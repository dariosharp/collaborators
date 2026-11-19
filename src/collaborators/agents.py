"""Agent abstraction: one long-lived conversation per agent.

`AgentSession` is the backend boundary. The real implementation (backend_sdk)
drives a Claude Agent SDK client; the fake one (backend_fake) scripts events
for tests and the demo. `AgentRunner` sits on top of either, serialises
prompts, tracks status and the files the agent writes, and forwards every
event to the UI.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from .events import (
    AgentStatus,
    Event,
    Notice,
    StatusChanged,
    ToolCall,
    TurnResult,
    UsageChanged,
    UserPrompt,
)
from .usage import AgentUsage, context_from_transcript, fmt_tokens, transcript_path

COMPACT_TIMEOUT_S = 600

Emit = Callable[[Event], None]


@dataclass(frozen=True)
class ToolSpec:
    """An in-process tool offered to an agent (used for the Lead's team tools)."""
    name: str
    description: str
    schema: dict[str, Any]  # JSON schema of the input object
    handler: Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class Decision:
    allow: bool
    always: bool = False
    message: str = ""


Approver = Callable[[str, str, dict[str, Any]], Awaitable[Decision]]


@dataclass
class AgentSpec:
    name: str
    role: str
    model: str
    effort: str
    cwd: Path
    system_append: str
    session_id: str
    resume: bool = False
    permission_mode: str = "bypassPermissions"
    disallowed_tools: list[str] = field(default_factory=list)
    tools: list[ToolSpec] = field(default_factory=list)
    approver: Approver | None = None
    team: str = "main"
    env: dict[str, str] = field(default_factory=dict)  # e.g. the account's token


@dataclass(frozen=True)
class Command:
    """A slash command or skill the agent's Claude Code session offers."""
    name: str
    description: str = ""
    hint: str = ""
    # Values its first argument can take, as (value, description), for completion.
    # Empty: guessed from the hint when it lists them, e.g. "[low|medium|high]".
    options: tuple[tuple[str, str], ...] = ()


class AgentSession(Protocol):
    session_id: str

    async def connect(self) -> None: ...

    @property
    def commands(self) -> list[Command]:
        """Slash commands and skills available once connected."""
        ...

    def run(self, prompt: str) -> AsyncIterator[Event]:
        """Send `prompt` and yield events until (and including) a TurnResult."""
        ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...


Backend = Callable[[AgentSpec], AgentSession]


class AgentRunner:
    def __init__(self, spec: AgentSpec, session: AgentSession, emit: Emit) -> None:
        self.spec = spec
        self.session = session
        self._emit = emit
        self._lock = asyncio.Lock()
        self.status = AgentStatus.STARTING
        self.task_id: str | None = None
        self.last_touched: list[str] = []
        self.total_cost = 0.0
        self.usage = AgentUsage()  # context size and token totals since collab started
        self.compacting = False    # a /compact is running or about to (holds the lock)
        self._estimating: asyncio.Task[None] | None = None
        self.last_limited = False  # its last turn stopped at the account's usage limit
        self._ran = spec.resume    # its conversation exists, so it can be resumed
        self._attach(session)

    def _attach(self, session: AgentSession) -> None:
        if hasattr(session, "on_remote"):
            session.on_remote = self._on_remote  # type: ignore[attr-defined]

    def _on_remote(self, event: Event) -> None:
        """Events of a turn someone started from claude.ai (Remote Control)."""
        if self.status is not AgentStatus.RUNNING:
            self._set_status(AgentStatus.RUNNING)
        self._emit(event)
        if isinstance(event, TurnResult):
            self._ran = True
            self.total_cost += event.cost_usd
            self._account(event)
            if not self.busy:
                self._set_status(AgentStatus.IDLE)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def commands(self) -> list[Command]:
        return list(getattr(self.session, "commands", None) or [])

    @property
    def models(self) -> list[tuple[str, str]]:
        """The models its session offers for /model, as (value, description)."""
        return list(getattr(self.session, "models", None) or [])

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def _set_status(self, status: AgentStatus, detail: str = "") -> None:
        self.status = status
        self._emit(StatusChanged(self.name, status, self.task_id, detail))

    async def start(self) -> None:
        self._set_status(AgentStatus.STARTING)
        try:
            await self.session.connect()
        except Exception as exc:  # surfaced in the UI, the rest of the team keeps going
            self._set_status(AgentStatus.ERROR, str(exc))
            self._emit(Notice(self.name, f"{self.name} failed to start: {exc}", "error"))
            raise
        self._set_status(AgentStatus.IDLE)
        if self.spec.resume and self.usage.context_tokens is None and self._estimating is None:
            self._estimating = asyncio.create_task(self._estimate_context(),
                                                   name=f"{self.name}-context")

    def _account(self, result: TurnResult) -> None:
        """Add a finished turn (ours or one started from claude.ai) to the usage."""
        u = self.usage
        if result.usage is not None:
            u.totals.add(result.usage)
        else:
            u.totals.cost_usd += result.cost_usd
            u.totals.output += result.output_tokens
        u.turns += 1
        if result.context_tokens:
            u.context_tokens, u.estimated = result.context_tokens, False
        self._emit(UsageChanged(self.name, u.copy()))

    async def _estimate_context(self) -> None:
        """A resumed conversation's context size, until its first turn says: from the
        tail of Claude Code's transcript on disk, off the event loop."""
        def read() -> int | None:
            path = transcript_path(self.spec.cwd, self.session.session_id, self.spec.env)
            return context_from_transcript(path) if path else None
        try:
            tokens = await asyncio.to_thread(read)
        except Exception:
            return
        if tokens and self.usage.context_tokens is None:
            self.usage.context_tokens, self.usage.estimated = tokens, True
            self._emit(UsageChanged(self.name, self.usage.copy()))

    async def run(self, prompt: str, task_id: str | None = None) -> TurnResult:
        async with self._lock:
            self.task_id = task_id
            self._set_status(AgentStatus.RUNNING)
            self._emit(UserPrompt(self.name, prompt, task_id))
            touched: list[str] = []
            result: TurnResult | None = None
            try:
                async for event in self.session.run(prompt):
                    if isinstance(event, ToolCall):
                        path = event.written_file
                        if path and path not in touched:
                            touched.append(path)
                    elif isinstance(event, TurnResult):
                        result = event
                    self._emit(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = TurnResult(self.name, f"{type(exc).__name__}: {exc}",
                                    is_error=True, subtype="exception")
                self._emit(result)
            finally:
                self.last_touched = touched
                self.task_id = None
                failed = result is None or (result.is_error and result.subtype == "exception")
                self._set_status(AgentStatus.ERROR if failed else AgentStatus.IDLE,
                                 result.text if failed and result else "")
            if result is None:
                result = TurnResult(self.name, "the agent stopped without a result",
                                    is_error=True, subtype="no_result")
            self._ran = True
            self.last_limited = result.limited
            self.total_cost += result.cost_usd
            self._account(result)
            return result

    async def compact(self, instructions: str,
                      still_needed: Callable[[], bool] | None = None) -> bool:
        """`/compact` the conversation, keeping what `instructions` say. Meant for right
        after a turn, while the prompt cache is warm. Holds the agent meanwhile: work
        sent to it waits. `still_needed` is checked once the agent is ours (work sent
        just before may have run first): False skips it. Failures are shown and
        swallowed. True if it compacted."""
        self.compacting = True
        try:
            async with self._lock:
                if still_needed is not None and not still_needed():
                    return False
                return await self._compact(instructions)
        finally:
            self.compacting = False

    async def _compact(self, instructions: str) -> bool:
        before = self.usage.context_tokens
        self._set_status(AgentStatus.COMPACTING)
        result: TurnResult | None = None
        error = ""
        try:
            async with asyncio.timeout(COMPACT_TIMEOUT_S):
                async for event in self.session.run(f"/compact {instructions}"):
                    if isinstance(event, TurnResult):
                        result = event  # the summary itself stays out of the transcript
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        if result is None or result.is_error:
            error = error or (result.text if result else "no result")
        if result is not None:
            # Its tokens count; its context is the one it summarised, not the new one.
            self._account(replace(result, context_tokens=None))
        after = None
        if not error:
            size = getattr(self.session, "context_size", None)
            if size is not None:
                with contextlib.suppress(Exception):
                    after = await size()
            self.usage.context_tokens, self.usage.estimated = after, False
            self._emit(UsageChanged(self.name, self.usage.copy()))
        self._set_status(AgentStatus.IDLE)
        was = fmt_tokens(before) if before else "?"
        if error:
            self._emit(Notice(self.name, f"Compaction failed ({error}); carrying on with "
                                         f"the full conversation ({was}).", "warning"))
            return False
        self._emit(Notice(self.name, f"Compacted: {was} → "
                                     f"{fmt_tokens(after) if after else '?'}"))
        return True

    async def start_fresh(self, backend: Backend, session_id: str, note: str) -> None:
        """Begin a new conversation (a new session id, nothing resumed): e.g. a tester,
        which needs no memory of earlier tasks. `note` marks the break in the UI."""
        await self.reconnect(backend, session_id=session_id, resume=False)
        self._ran = False
        self.usage.context_tokens, self.usage.estimated = None, False
        self._emit(UsageChanged(self.name, self.usage.copy()))
        self._emit(Notice(self.name, note))

    async def reconnect(self, backend: Backend, **changes: Any) -> None:
        """Start a new session for the same conversation with `changes` to the spec
        (e.g. another account's environment). Waits for the turn in progress to end."""
        if self.busy:
            self._emit(Notice(self.name, f"{self.name} switches account when this turn ends"))
        async with self._lock:
            fields: dict[str, Any] = {"session_id": self.session.session_id,
                                      "resume": self._ran, **changes}
            self.spec = replace(self.spec, **fields)
            old, self.session = self.session, backend(self.spec)
            self._attach(self.session)
            with contextlib.suppress(Exception):
                await old.close()
            self.last_limited = False
            await self.start()

    async def interrupt(self) -> None:
        if self.busy or getattr(self.session, "remote_turn", False):
            await self.session.interrupt()

    async def close(self) -> None:
        if self._estimating is not None:
            self._estimating.cancel()
        await self.session.close()
