"""The real backend: one Claude Agent SDK client (a headless Claude Code) per agent."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
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
from .events import Event, TextDelta, TextDone, Thinking, ToolCall, ToolResult, TurnResult
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

    async def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await client.disconnect()

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def run(self, prompt: str) -> AsyncIterator[Event]:
        if self._client is None:
            raise RuntimeError(f"{self.spec.name} is not connected")
        name = self.spec.name
        await self._client.query(prompt)
        last_text = ""
        async for msg in self._client.receive_response():
            if isinstance(msg, StreamEvent):
                if msg.parent_tool_use_id:  # a subagent's stream; its final blocks still show
                    continue
                ev = msg.event
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield TextDelta(name, delta["text"])
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        last_text = block.text
                        yield TextDone(name, block.text)
                    elif isinstance(block, ThinkingBlock):
                        if block.thinking.strip():
                            yield Thinking(name, block.thinking)
                    elif isinstance(block, ToolUseBlock):
                        yield ToolCall(name, block.id, block.name, dict(block.input or {}))
            elif isinstance(msg, UserMessage):
                if isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock):
                            yield ToolResult(name, block.tool_use_id, _text_of(block.content),
                                             bool(block.is_error))
            elif isinstance(msg, ResultMessage):
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
                yield TurnResult(
                    name, text, is_error=msg.is_error, subtype=msg.subtype,
                    cost_usd=cost, duration_ms=msg.duration_ms, num_turns=msg.num_turns,
                    input_tokens=int(usage.get("input_tokens", 0) or 0)
                    + int(usage.get("cache_read_input_tokens", 0) or 0)
                    + int(usage.get("cache_creation_input_tokens", 0) or 0),
                    output_tokens=int(usage.get("output_tokens", 0) or 0),
                    session_id=msg.session_id,
                )


def sdk_backend(spec: AgentSpec) -> SdkSession:
    return SdkSession(spec)
