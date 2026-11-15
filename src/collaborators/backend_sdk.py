"""The real backend: one Claude Agent SDK client (a headless Claude Code) per agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    StreamEvent,
    ToolPermissionContext,
)

from .agents import AgentSpec, Command, ToolSpec
from .events import (
    Event,
    Notice,
    TextDelta,
    TextDone,
    Thinking,
    ToolCall,
    ToolResult,
    TurnResult,
)
from .prompts import TOOL_PREFIX


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, dict) and item.get("type") == "image":
                parts.append("[image]")
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False, default=str)


def _injected(msg: Any) -> bool:
    """True for a message the CLI attributes to something other than a person or an
    SDK prompt (task notifications, peers, channels, …). Our own prompts carry none."""
    origin = getattr(msg, "origin", None)
    return isinstance(origin, dict) and origin.get("kind") not in (None, "human")


def _sdk_tool(spec: ToolSpec):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        out = await spec.handler(args)
        result: dict[str, Any] = {"content": [{"type": "text", "text": out}]}
        if out.startswith("ERROR"):
            result["is_error"] = True
        return result

    return tool(spec.name, spec.description, spec.schema)(handler)


class SdkSession:
    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec
        self.session_id = spec.session_id
        self._client: ClaudeSDKClient | None = None
        self._cost_so_far = 0.0  # ResultMessage cost is cumulative per client
        self._always_allowed: set[str] = set()
        self.commands: list[Command] = []
        self.remote_control_available = False
        self.remote_url: str | None = None
        self.on_remote: Callable[[Event], None] | None = None  # events of claude.ai turns
        self._pump_task: asyncio.Task[None] | None = None
        self._turn_queue: asyncio.Queue[Event | BaseException | None] | None = None
        self._local_pending = 0   # our queries whose turn hasn't finished
        self._in_turn = False
        self._turn_is_remote = False
        self._idle = asyncio.Event()
        self._idle.set()

    def _options(self) -> ClaudeAgentOptions:
        spec = self.spec
        kwargs: dict[str, Any] = dict(
            model=spec.model,
            effort=spec.effort,
            cwd=str(spec.cwd),
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": spec.system_append},
            include_partial_messages=True,
            # Show a readable summary of the reasoning in the UI (it is hidden by default).
            thinking={"type": "adaptive", "display": "summarized"},
            disallowed_tools=list(spec.disallowed_tools),
            # Load the user's and the project's settings (CLAUDE.md, permissions, MCP).
            setting_sources=["user", "project", "local"],
            # The user's and the project's skills, usable by the agent and as /commands.
            skills="all",
        )
        if spec.resume:
            kwargs["resume"] = spec.session_id
        else:
            kwargs["session_id"] = spec.session_id
        if spec.tools:
            kwargs["mcp_servers"] = {
                "team": create_sdk_mcp_server("team", tools=[_sdk_tool(t) for t in spec.tools])
            }
            kwargs["allowed_tools"] = [TOOL_PREFIX + t.name for t in spec.tools]
        if spec.approver is not None:
            kwargs["permission_mode"] = "default"
            kwargs["can_use_tool"] = self._can_use_tool
        else:
            kwargs["permission_mode"] = spec.permission_mode
        return ClaudeAgentOptions(**kwargs)

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any],
                            context: ToolPermissionContext):
        if tool_name in self._always_allowed or tool_name.startswith(TOOL_PREFIX):
            return PermissionResultAllow()
        assert self.spec.approver is not None
        decision = await self.spec.approver(self.spec.name, tool_name, tool_input)
        if decision.allow:
            if decision.always:
                self._always_allowed.add(tool_name)
            return PermissionResultAllow()
        return PermissionResultDeny(message=decision.message or "Denied by the user.")

    async def connect(self) -> None:
        self._client = ClaudeSDKClient(self._options())
        await self._client.connect()
        try:
            info = await self._client.get_server_info() or {}
        except Exception:
            info = {}
        self.commands = [
            Command(str(c.get("name", "")), str(c.get("description", "") or ""),
                    str(c.get("argumentHint", "") or ""))
            for c in info.get("commands", []) or []
            if isinstance(c, dict) and c.get("name") and not str(c["name"]).startswith("_")
        ]
        self.remote_control_available = bool(info.get("remote_control_available"))
        # Read the session's output all the time, not only during our own turns: with
        # Remote Control a turn can also start from claude.ai, and unread messages
        # would otherwise pile up and stall the session.
        self._pump_task = asyncio.create_task(self._pump(), name=f"{self.spec.name}-pump")

    async def close(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            with contextlib.suppress(BaseException):
                await self._pump_task
            self._pump_task = None
        if self._client is not None:
            client, self._client = self._client, None
            await client.disconnect()

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def set_remote_control(self, enabled: bool, name: str | None = None) -> str | None:
        """Turn Remote Control on (returns the claude.ai session URL) or off."""
        if self._client is None or self._client._query is None:
            raise RuntimeError(f"{self.spec.name} is not connected")
        request: dict[str, Any] = {"subtype": "remote_control", "enabled": enabled}
        if name:
            request["name"] = name
        # Not wrapped by the Python SDK yet; it is the same control request the
        # TypeScript SDK and the CLI use.
        response = await self._client._query._send_control_request(request, timeout=60)
        self.remote_url = (response or {}).get("session_url") if enabled else None
        return self.remote_url

    # ---- the reader -----------------------------------------------------------------
    @property
    def remote_turn(self) -> bool:
        """A turn started from claude.ai is in progress."""
        return self._in_turn and self._turn_is_remote

    async def run(self, prompt: str) -> AsyncIterator[Event]:
        if self._client is None:
            raise RuntimeError(f"{self.spec.name} is not connected")
        await self._idle.wait()  # never interleave with a turn started from claude.ai
        queue: asyncio.Queue[Event | BaseException | None] = asyncio.Queue()
        self._turn_queue = queue
        self._local_pending += 1
        try:
            await self._client.query(prompt)
            while True:
                item = await queue.get()
                if item is None:
                    raise RuntimeError(f"{self.spec.name}: the session ended")
                if isinstance(item, BaseException):
                    raise item
                yield item
                if isinstance(item, TurnResult):
                    return
        finally:
            if self._turn_queue is queue:
                self._turn_queue = None

    def _deliver(self, event: Event | BaseException | None) -> None:
        if self._turn_is_remote:
            if isinstance(event, Event) and self.on_remote is not None:
                self.on_remote(event)
        elif self._turn_queue is not None:
            self._turn_queue.put_nowait(event)

    async def _pump(self) -> None:
        assert self._client is not None
        name = self.spec.name
        last_text = ""
        try:
            async for msg in self._client.receive_messages():
                if not self._in_turn:
                    starts = isinstance(msg, (StreamEvent, AssistantMessage, UserMessage)) or (
                        isinstance(msg, SystemMessage) and msg.subtype == "init")
                    if not starts:
                        continue  # stray status messages between turns
                    self._in_turn = True
                    self._turn_is_remote = self._local_pending == 0 or _injected(msg)
                    self._idle.clear()
                    last_text = ""
                if isinstance(msg, StreamEvent):
                    if msg.parent_tool_use_id:  # a subagent's stream; its final blocks show
                        continue
                    ev = msg.event
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            self._deliver(TextDelta(name, delta["text"]))
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            last_text = block.text
                            self._deliver(TextDone(name, block.text))
                        elif isinstance(block, ThinkingBlock):
                            if block.thinking.strip():
                                self._deliver(Thinking(name, block.thinking))
                        elif isinstance(block, ToolUseBlock):
                            self._deliver(ToolCall(name, block.id, block.name,
                                                   dict(block.input or {})))
                elif isinstance(msg, UserMessage):
                    if isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock):
                                self._deliver(ToolResult(name, block.tool_use_id,
                                                         _text_of(block.content),
                                                         bool(block.is_error)))
                            elif isinstance(block, TextBlock) and self._turn_is_remote:
                                self._deliver(Notice(name, "claude.ai › " + block.text))
                    elif isinstance(msg.content, str) and self._turn_is_remote:
                        self._deliver(Notice(name, "claude.ai › " + msg.content))
                elif isinstance(msg, ResultMessage):
                    if _injected(msg):
                        # A turn the session started itself (a background task finishing,
                        # a scheduled prompt, …) — not the answer to our prompt.
                        self._turn_is_remote = True
                    self._deliver(self._turn_result(msg, last_text))
                    if not self._turn_is_remote:
                        self._local_pending = max(0, self._local_pending - 1)
                    self._in_turn = False
                    self._turn_is_remote = False
                    self._idle.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._deliver(exc)
        finally:
            self._turn_is_remote = False
            self._deliver(None)
            self._in_turn = False
            self._idle.set()

    def _turn_result(self, msg: ResultMessage, last_text: str) -> TurnResult:
        total = msg.total_cost_usd or 0.0
        cost = max(0.0, total - self._cost_so_far)
        self._cost_so_far = max(self._cost_so_far, total)
        usage = msg.usage or {}
        if msg.session_id:
            self.session_id = msg.session_id
        errors = getattr(msg, "errors", None)
        text = msg.result or last_text
        if msg.is_error and not text:
            text = f"{msg.subtype}: {errors}" if errors else msg.subtype
        return TurnResult(
            self.spec.name, text, is_error=msg.is_error, subtype=msg.subtype,
            cost_usd=cost, duration_ms=msg.duration_ms, num_turns=msg.num_turns,
            input_tokens=int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            session_id=msg.session_id,
        )


def sdk_backend(spec: AgentSpec) -> SdkSession:
    return SdkSession(spec)
