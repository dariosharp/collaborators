"""Agent abstraction: one long-lived conversation per agent.

`AgentSession` is the backend boundary. The real implementation (backend_sdk)
drives a Claude Agent SDK client; the fake one (backend_fake) scripts events
for tests and the demo. `AgentRunner` sits on top of either, serialises
prompts, tracks status and the files the agent writes, and forwards every
event to the UI.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .events import (
    AgentStatus,
    Event,
    Notice,
    StatusChanged,
    ToolCall,
    TurnResult,
    UserPrompt,
)

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


@dataclass(frozen=True)
class Command:
    """A slash command or skill the agent's Claude Code session offers."""
    name: str
    description: str = ""
    hint: str = ""


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
        if hasattr(session, "on_remote"):
            session.on_remote = self._on_remote  # type: ignore[attr-defined]

    def _on_remote(self, event: Event) -> None:
        """Events of a turn someone started from claude.ai (Remote Control)."""
        if self.status is not AgentStatus.RUNNING:
            self._set_status(AgentStatus.RUNNING)
        self._emit(event)
        if isinstance(event, TurnResult):
            self.total_cost += event.cost_usd
            if not self.busy:
                self._set_status(AgentStatus.IDLE)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def commands(self) -> list[Command]:
        return list(getattr(self.session, "commands", None) or [])

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
            self.total_cost += result.cost_usd
            return result

    async def interrupt(self) -> None:
        if self.busy or getattr(self.session, "remote_turn", False):
            await self.session.interrupt()

    async def close(self) -> None:
        await self.session.close()
