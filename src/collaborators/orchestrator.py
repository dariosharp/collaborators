"""The Team: owns every agent, the task registry, and the Lead's team tools."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agents import AgentRunner, AgentSpec, Approver, Backend, Emit, ToolSpec
from .config import TeamConfig
from .events import (
    AgentAdded,
    AgentRemoved,
    Event,
    Notice,
    TaskRecord,
    TaskStatus,
    TaskUpdate,
    ToolCall,
)
from .prompts import prompt_for
from .state import StateStore, new_session_id

log = logging.getLogger(__name__)

LEAD = "lead"
# The Lead orchestrates; it must not change the code itself.
LEAD_DISALLOWED = ["Edit", "MultiEdit", "Write", "NotebookEdit"]
MAX_AGENTS = 16


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class Team:
    def __init__(
        self,
        cfg: TeamConfig,
        backend: Backend,
        emit: Emit,
        store: StateStore | None = None,
        *,
        resume: bool = False,
        approver: Approver | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store or StateStore(cfg.state_dir)
        def safe_emit(event: Event) -> None:
            # A display problem must never break the orchestration itself.
            try:
                emit(event)
            except Exception:
                log.exception("UI failed to handle %r", event)

        self._ui_emit = safe_emit
        self.tasks: dict[str, TaskRecord] = {}
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._waiting: dict[str, int] = {}   # task id -> number of active waiters
        self._reported: set[str] = set()     # tasks whose report reached the Lead
        self._counter = self.store.last_task_number()
        self._lead_inbox: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._lead_loop: asyncio.Task[None] | None = None
        self.started_at = time.time()

        self._backend = backend
        self._approver = approver
        self._announcements: list[str] = []  # news the Lead hasn't seen yet
        self._news = asyncio.Event()           # wakes a blocked wait_tasks early
        previous = self.store.load_sessions() if resume else {}
        # On --resume, agents added at runtime last time come back too.
        names = list(cfg.agent_names)
        for role in ("worker", "tester"):
            names += sorted((n for n in previous if cfg.role_of(n) == role and n not in names),
                            key=lambda n: int(n[len(role):] or 0))
        self.resumed = sorted(n for n in names if n in previous)
        self._previous = previous
        self.runners: dict[str, AgentRunner] = {}
        self._initial_names = names
        for name in names:
            self._make_runner(name)

    def _make_runner(self, name: str) -> AgentRunner:
        role = self.cfg.role_of(name)
        rc = self.cfg.role_config(name)
        spec = AgentSpec(
            name=name, role=role, model=rc.model, effort=rc.effort, cwd=self.cfg.workdir,
            system_append=prompt_for(self.cfg, name, self._initial_names),
            session_id=self._previous.get(name) or new_session_id(),
            resume=name in self._previous,
            permission_mode=self.cfg.permission_mode,
            disallowed_tools=list(LEAD_DISALLOWED) if role == "lead" else [],
            tools=self._lead_tools() if role == "lead" else [],
            approver=self._approver if self.cfg.ask else None,
        )
        runner = AgentRunner(spec, self._backend(spec), self._on_event)
        self.runners[name] = runner
        return runner

    def _save_sessions(self) -> None:
        self.store.save_sessions(
            {n: r.spec.session_id for n, r in self.runners.items()},
            {n: {"model": r.spec.model, "effort": r.spec.effort} for n, r in self.runners.items()},
        )

    @property
    def agent_names(self) -> list[str]:
        return list(self.runners)

    async def add_agent(self, role: str) -> str:
        """Add a worker or tester while the team is running, and tell the Lead."""
        if role not in ("worker", "tester"):
            raise ValueError("only workers and testers can be added")
        taken = [int(n[len(role):]) for n in self.runners if self.cfg.role_of(n) == role]
        if len(self.runners) >= MAX_AGENTS:
            raise ValueError(f"the team is full ({MAX_AGENTS} agents)")
        name = f"{role}{max(taken, default=0) + 1}"
        runner = self._make_runner(name)
        self._save_sessions()
        self._ui_emit(AgentAdded(name, role, runner.spec.model, runner.spec.effort))
        try:
            await runner.start()
        except Exception:
            return name  # the card shows the error; the Lead isn't told about a dead agent
        self._announce(f"{name} ({role}, {runner.spec.model}, effort {runner.spec.effort}) "
                       "joined the team and is idle, ready for tasks.")
        return name

    async def remove_agent(self, name: str) -> str:
        """Remove a worker or tester from the running team, and tell the Lead.
        A task it is running is cancelled first."""
        name = self.resolve_agent(name)
        if name == LEAD:
            raise ValueError("the Lead cannot be removed")
        # Gone from the roster first, so nothing new can be dispatched to it meanwhile.
        runner = self.runners.pop(name)
        self._previous.pop(name, None)  # re-adding the same name starts a fresh session
        self._save_sessions()
        task = next((t for t in self._running_tasks() if t.agent == name), None)
        if task is not None:
            task.status = TaskStatus.CANCELLED
            self._reported.add(task.id)  # the announcement below covers it
            with contextlib.suppress(Exception):
                await runner.interrupt()
            job = self._jobs.get(task.id)
            if job is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(job), timeout=15)
                except (TimeoutError, asyncio.CancelledError, Exception):
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner.close(), timeout=10)
        self._ui_emit(AgentRemoved(name))
        note = f" Its task {task.id} was cancelled." if task else ""
        self._announce(f"{name} was removed from the team by the user and can no longer "
                       f"receive tasks.{note}")
        return name

    def _announce(self, text: str) -> None:
        """Make sure the Lead learns `text`: in its next team-tool result if it is
        working, otherwise as a message as soon as it is idle."""
        self._announcements.append(text)
        self._news.set()
        self._lead_inbox.put_nowait(("announce", ""))
        self._ui_emit(Notice(LEAD, text))

    def _take_announcements(self) -> str:
        if not self._announcements:
            return ""
        text = "\n".join(f"[team] {a}" for a in self._announcements)
        self._announcements.clear()
        self._news.clear()
        return text + "\nCurrent team:\n" + self.status_report()

    # ---- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        self.store.ensure()
        self._save_sessions()
        await asyncio.gather(*(r.start() for r in self.runners.values()),
                             return_exceptions=True)
        self._lead_loop = asyncio.create_task(self._run_lead_inbox(), name="lead-inbox")

    async def shutdown(self) -> None:
        if self._lead_loop:
            self._lead_loop.cancel()
        for job in self._jobs.values():
            job.cancel()
        await asyncio.gather(*self._jobs.values(), return_exceptions=True)
        if self._lead_loop:
            with contextlib.suppress(asyncio.CancelledError):
                await self._lead_loop
        await asyncio.gather(*(r.close() for r in self.runners.values()),
                             return_exceptions=True)

    # ---- events ---------------------------------------------------------
    def _on_event(self, event: Event) -> None:
        if isinstance(event, ToolCall) and event.written_file:
            self._track_write(event.agent, event.written_file)
        self._ui_emit(event)

    def _norm(self, path: str) -> str:
        p = Path(path)
        if not p.is_absolute():
            p = self.cfg.workdir / p
        try:
            return str(p.resolve().relative_to(self.cfg.workdir))
        except ValueError:
            return str(p.resolve())

    def _running_tasks(self) -> list[TaskRecord]:
        return [t for t in self.tasks.values() if t.status is TaskStatus.RUNNING]

    def _track_write(self, agent: str, path: str) -> None:
        task = next((t for t in self._running_tasks() if t.agent == agent), None)
        if task is None:
            return
        rel = self._norm(path)
        if rel not in task.touched_files:
            task.touched_files.append(rel)
            self._ui_emit(TaskUpdate(task.agent, task))
        for other in self._running_tasks():
            if other is task:
                continue
            if rel in other.touched_files or rel in other.planned_files:
                msg = (f"{task.id} ({task.agent}) wrote {rel}, which {other.id} "
                       f"({other.agent}) is also working on")
                for t in (task, other):
                    if msg not in t.warnings:
                        t.warnings.append(msg)
                self._ui_emit(Notice(LEAD, msg, "warning"))

    # ---- the user talks to the Lead --------------------------------------
    def tell_lead(self, text: str) -> None:
        self._lead_inbox.put_nowait(("user", text))

    async def _run_lead_inbox(self) -> None:
        lead = self.runners[LEAD]
        while True:
            kind, payload = await self._lead_inbox.get()
            if kind == "announce":
                payload = self._take_announcements()
                if not payload:
                    continue  # already delivered inside a tool result
            elif kind == "task_done":
                # Only report tasks the Lead hasn't collected through wait_tasks meanwhile.
                if payload in self._reported:
                    continue
                self._reported.add(payload)
                payload = "[team] A task finished while you were not waiting for it.\n\n" + \
                    self.task_report(self.tasks[payload])
            try:
                await lead.run(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the inbox alive
                self._ui_emit(Notice(LEAD, f"Lead error: {exc}", "error"))

    # ---- tasks ------------------------------------------------------------
    def resolve_agent(self, name: str) -> str:
        n = name.strip().lower()
        if n in ("worker", "tester"):
            n += "1"
        if n not in self.runners:
            raise ValueError(f"unknown agent {name!r}; available: "
                             + ", ".join(a for a in self.runners if a != LEAD))
        return n

    def dispatch(self, agent: str, prompt: str, files: list[str] | None = None,
                 origin: str = "lead") -> TaskRecord:
        agent = self.resolve_agent(agent)
        if agent == LEAD:
            raise ValueError("the Lead cannot dispatch tasks to itself")
        runner = self.runners[agent]
        busy = next((t for t in self._running_tasks() if t.agent == agent), None)
        if busy or runner.busy:
            raise ValueError(f"{agent} is busy" + (f" with {busy.id}" if busy else "")
                             + "; wait for it or pick another agent")
        planned = [self._norm(f) for f in files or []]
        for other in self._running_tasks():
            clash = sorted(set(planned) & set(other.planned_files + other.touched_files))
            if clash:
                raise ValueError(f"{other.id} ({other.agent}) is already working on "
                                 f"{', '.join(clash)}; run this after it finishes")
        self._counter += 1
        task = TaskRecord(id=f"T{self._counter}", agent=agent, prompt=prompt,
                          origin=origin, planned_files=planned)
        self.tasks[task.id] = task
        self._done[task.id] = asyncio.Event()
        self._ui_emit(TaskUpdate(agent, task))
        self._jobs[task.id] = asyncio.create_task(self._run_task(task), name=task.id)
        return task

    async def _run_task(self, task: TaskRecord) -> None:
        runner = self.runners[task.agent]
        try:
            result = await runner.run(task.prompt, task.id)
            task.result = result.text
            task.cost_usd = result.cost_usd
            task.num_turns = result.num_turns
            if task.status is not TaskStatus.CANCELLED:
                task.status = TaskStatus.FAILED if result.is_error else TaskStatus.DONE
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            raise
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.result = f"{type(exc).__name__}: {exc}"
        finally:
            task.finished_at = time.time()
            self._done[task.id].set()
            self._ui_emit(TaskUpdate(task.agent, task))
            with contextlib.suppress(Exception):
                self.store.append_task(task)
            if task.origin == "lead" and not self._waiting.get(task.id):
                self._lead_inbox.put_nowait(("task_done", task.id))

    async def wait(self, task_ids: list[str], mode: str = "all",
                   timeout: float | None = None, wake_on_news: bool = False) -> list[TaskRecord]:
        ids = [t.strip().upper() for t in task_ids] or [t.id for t in self._running_tasks()]
        unknown = [t for t in ids if t not in self.tasks]
        if unknown:
            raise ValueError(f"unknown task id(s): {', '.join(unknown)}")
        if not ids:
            return []
        for t in ids:
            self._waiting[t] = self._waiting.get(t, 0) + 1
        try:
            waiters = [asyncio.create_task(self._done[t].wait()) for t in ids]
            when = asyncio.FIRST_COMPLETED if mode == "any" else asyncio.ALL_COMPLETED
            done_waiting = asyncio.ensure_future(asyncio.wait(waiters, timeout=timeout,
                                                              return_when=when))
            news = None
            if wake_on_news:
                news = asyncio.ensure_future(self._news.wait())
            await asyncio.wait([done_waiting] + ([news] if news else []),
                               return_when=asyncio.FIRST_COMPLETED)
            for fut in [done_waiting, news, *waiters]:
                if fut is not None and not fut.done():
                    fut.cancel()
        finally:
            for t in ids:
                self._waiting[t] -= 1
        records = [self.tasks[t] for t in ids]
        self._reported.update(r.id for r in records if r.status.finished)
        return records

    async def cancel(self, task_id: str) -> TaskRecord:
        task = self.tasks.get(task_id.strip().upper())
        if task is None:
            raise ValueError(f"unknown task id {task_id!r}")
        if task.status is TaskStatus.RUNNING:
            task.status = TaskStatus.CANCELLED
            await self.runners[task.agent].interrupt()
            self._ui_emit(Notice(LEAD, f"{task.id} cancelled", "warning"))
        return task

    async def interrupt(self, agent: str) -> None:
        """Interrupt whatever `agent` is doing (UI ctrl+c)."""
        task = next((t for t in self._running_tasks() if t.agent == agent), None)
        if task:
            await self.cancel(task.id)
        else:
            await self.runners[agent].interrupt()

    # ---- reports ------------------------------------------------------------
    def task_report(self, task: TaskRecord) -> str:
        head = f"{task.id} · {task.agent} · {task.status.value}"
        if task.status.finished:
            head += f" in {fmt_duration(task.elapsed)} · ${task.cost_usd:.2f}"
        else:
            head += f" for {fmt_duration(task.elapsed)} so far"
        lines = [head]
        if task.verdict:
            lines.append(f"verdict: {task.verdict}")
        if task.touched_files:
            lines.append("files modified: " + ", ".join(task.touched_files))
        lines += [f"WARNING: {w}" for w in task.warnings]
        if task.status.finished:
            lines += ["--- report ---", task.result.strip() or "(no text)"]
        return "\n".join(lines)

    def status_report(self) -> str:
        lines = []
        for name, runner in self.runners.items():
            if name == LEAD:
                continue
            task = next((t for t in self._running_tasks() if t.agent == name), None)
            state = (f"busy with {task.id} ({fmt_duration(task.elapsed)})" if task
                     else runner.status.value)
            lines.append(f"{name} [{runner.spec.model}, effort {runner.spec.effort}]: {state}")
        finished = [t for t in self.tasks.values() if t.status.finished]
        if finished:
            lines.append("finished tasks: " + ", ".join(
                f"{t.id}={t.status.value}{'/' + t.verdict if t.verdict else ''}"
                for t in finished))
        return "\n".join(lines)

    # ---- the Lead's tools ------------------------------------------------
    def _lead_tools(self) -> list[ToolSpec]:
        def guarded(fn: Callable[[dict[str, Any]], Any]):
            async def handler(args: dict[str, Any]) -> str:
                try:
                    out = await fn(args)
                except ValueError as exc:
                    out = f"ERROR: {exc}"
                news = self._take_announcements()
                return f"{out}\n\n{news}" if news else out
            return handler

        async def dispatch_task(args: dict[str, Any]) -> str:
            task = self.dispatch(str(args.get("agent", "")), str(args.get("task", "")),
                                 [str(f) for f in args.get("files") or []])
            return (f"{task.id} started on {task.agent}. Use wait_tasks(['{task.id}']) "
                    "to collect its report.")

        async def wait_tasks(args: dict[str, Any]) -> str:
            ids = [str(t) for t in args.get("task_ids") or []]
            mode = "any" if args.get("mode") == "any" else "all"
            timeout = float(args.get("timeout_s") or 900)
            records = await self.wait(ids, mode, timeout, wake_on_news=True)
            if not records:
                return "No tasks are running."
            return "\n\n".join(self.task_report(r) for r in records)

        async def team_status(args: dict[str, Any]) -> str:
            return self.status_report()

        async def cancel_task(args: dict[str, Any]) -> str:
            task = await self.cancel(str(args.get("task_id", "")))
            return f"{task.id} is {task.status.value}"

        return [
            ToolSpec(
                "dispatch_task",
                "Start a task on an idle worker or tester. Returns a task id immediately; "
                "the agent works in the background.",
                {"type": "object", "properties": {
                    "agent": {"type": "string",
                              "description": "worker1, worker2, …, tester1, … (see team_status)"},
                    "task": {"type": "string", "description":
                             "Self-contained instructions with acceptance criteria."},
                    "files": {"type": "array", "items": {"type": "string"}, "description":
                              "Files the agent is expected to modify (for conflict checks)."},
                }, "required": ["agent", "task"]},
                guarded(dispatch_task),
            ),
            ToolSpec(
                "wait_tasks",
                "Wait for tasks to finish and return their reports. mode='any' returns "
                "as soon as one finishes. Empty task_ids waits on all running tasks. "
                "Returns early (tasks still running) if an agent joins the team.",
                {"type": "object", "properties": {
                    "task_ids": {"type": "array", "items": {"type": "string"}},
                    "mode": {"type": "string", "enum": ["all", "any"]},
                    "timeout_s": {"type": "number", "description": "Default 900."},
                }, "required": ["task_ids"]},
                guarded(wait_tasks),
            ),
            ToolSpec("team_status", "Show which agents are idle or busy, and finished tasks.",
                     {"type": "object", "properties": {}}, guarded(team_status)),
            ToolSpec(
                "cancel_task", "Interrupt a running task.",
                {"type": "object", "properties": {"task_id": {"type": "string"}},
                 "required": ["task_id"]},
                guarded(cancel_task),
            ),
        ]
