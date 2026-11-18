"""Typed events that flow from the agents/orchestrator to the UI.

Backends (the real Claude Agent SDK one, or the fake one used by tests and the
demo) translate whatever they receive into these events, so everything above
the backend layer — the orchestrator, the UI, the tests — speaks one language.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AgentStatus(StrEnum):
    STARTING = "starting"
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"  # waiting on a permission decision
    ERROR = "error"


class TaskStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self is not TaskStatus.RUNNING


# Tools whose input names a file the agent writes to.
WRITE_TOOLS = {"Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path",
               "NotebookEdit": "notebook_path"}


@dataclass(frozen=True)
class Event:
    agent: str


@dataclass(frozen=True)
class StatusChanged(Event):
    status: AgentStatus
    task_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class AgentAdded(Event):
    """A worker or tester joined the running team."""
    role: str
    model: str
    effort: str


@dataclass(frozen=True)
class AgentRemoved(Event):
    """A worker or tester left the running team."""


@dataclass(frozen=True)
class UserPrompt(Event):
    """A prompt delivered to an agent (by the user or by the Lead)."""
    text: str
    task_id: str | None = None


@dataclass(frozen=True)
class TextDelta(Event):
    """A streamed chunk of assistant text."""
    text: str


@dataclass(frozen=True)
class TextDone(Event):
    """An assistant text block is complete. `text` is the whole block."""
    text: str


@dataclass(frozen=True)
class Thinking(Event):
    text: str


@dataclass(frozen=True)
class ToolCall(Event):
    tool_id: str
    name: str
    input: dict[str, Any]

    @property
    def written_file(self) -> str | None:
        key = WRITE_TOOLS.get(self.name)
        value = self.input.get(key) if key else None
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class ToolResult(Event):
    tool_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class TurnResult(Event):
    """Emitted once per prompt, when the agent has fully finished it."""
    text: str
    is_error: bool = False
    subtype: str = "success"
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str | None = None
    limited: bool = False  # stopped because the account hit its usage limit


@dataclass(frozen=True)
class LimitReached(Event):
    """The account the agent runs under hit its usage limit."""
    resets_at: int | None = None  # unix time
    kind: str = ""  # five_hour, seven_day, …


@dataclass(frozen=True)
class Notice(Event):
    """An orchestration line for the Lead's log (dispatches, waits, warnings)."""
    text: str
    level: str = "info"  # info | warning | error


@dataclass
class TaskRecord:
    id: str
    agent: str
    prompt: str
    origin: str = "lead"  # "lead", or "user" for a message typed into an agent's view
    status: TaskStatus = TaskStatus.RUNNING
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    planned_files: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)
    result: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    warnings: list[str] = field(default_factory=list)
    # change tracking (git): the working tree it ran in, snapshots around it, and
    # the files that differ between them
    where: str = ""
    snap_before: str = ""
    snap_after: str = ""
    changed_files: list[str] = field(default_factory=list)
    overlapped: bool = False  # other tasks changed the same tree at the same time
    undone: bool = False
    merged: bool = False  # a worker task whose worktree changes reached the main directory
    limited: bool = False  # stopped because the account hit its usage limit

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    @property
    def verdict(self) -> str | None:
        """PASS/FAIL for tester tasks that follow the VERDICT convention."""
        for line in self.result.splitlines()[:5]:
            line = line.strip().strip("*").strip()
            if line.upper().startswith("VERDICT:"):
                word = line.split(":", 1)[1].strip().upper()
                if word.startswith("PASS"):
                    return "PASS"
                if word.startswith("FAIL"):
                    return "FAIL"
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "agent": self.agent, "prompt": self.prompt,
            "origin": self.origin, "status": self.status.value, "started_at": self.started_at,
            "finished_at": self.finished_at, "planned_files": self.planned_files,
            "touched_files": self.touched_files, "result": self.result,
            "cost_usd": self.cost_usd, "num_turns": self.num_turns,
            "warnings": self.warnings, "where": self.where,
            "snap_before": self.snap_before, "snap_after": self.snap_after,
            "changed_files": self.changed_files, "overlapped": self.overlapped,
            "undone": self.undone, "merged": self.merged, "limited": self.limited,
        }


@dataclass(frozen=True)
class TaskUpdate(Event):
    task: TaskRecord
