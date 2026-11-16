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
from .gitsnap import Tree
from .prompts import prompt_for
from .state import StateStore, new_session_id
from .workspace import Workspace

log = logging.getLogger(__name__)

LEAD = "lead"
# The Lead orchestrates; it must not change the code itself.
LEAD_DISALLOWED = ["Edit", "MultiEdit", "Write", "NotebookEdit"]
MAX_AGENTS = 16
# Lead-to-Lead messages a Lead may send before the user talks to it again:
# enough to agree on an API, not enough for two models to chat away the quota.
PEER_MESSAGE_BUDGET = 4


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class NeedsConfirmation(ValueError):
    """Undoing would also overwrite later changes to these files."""

    def __init__(self, files: list[str]) -> None:
        super().__init__("changed again since: " + ", ".join(files))
        self.files = files


class Hub:
    """The teams working in one directory (one per tab). Lets them see each
    other's running tasks for file conflicts, and lets their Leads talk."""

    def __init__(self) -> None:
        self.teams: dict[str, Team] = {}

    def others(self, team: Team) -> list[Team]:
        return [t for t in self.teams.values() if t is not team]

    def send(self, sender: Team, to: str, text: str) -> str:
        to = to.strip().lower()
        if not text.strip():
            raise ValueError("the message is empty")
        target = self.teams.get(to)
        if target is None or target is sender:
            names = ", ".join(t.name for t in self.others(sender)) or "none open right now"
            raise ValueError(f"no other team called {to!r}; other teams: {names}")
        if sender.peer_budget <= 0:
            raise ValueError("message limit reached: wait until the user talks to you "
                             "before writing to other Leads again")
        sender.peer_budget -= 1
        target.receive_from_lead(sender.name, text.strip())
        return (f"Delivered to the Lead of team {to}. It does not block you; a reply, "
                f"if any, will come as a [from the Lead of team {to}] message.")


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
        hub: Hub | None = None,
    ) -> None:
        self.cfg = cfg
        self.name = cfg.team
        self.hub = hub or Hub()
        if self.name in self.hub.teams:
            raise ValueError(f"a team called {self.name!r} is already open")
        self.hub.teams[self.name] = self
        self.peer_budget = PEER_MESSAGE_BUDGET
        self.store = store or StateStore(cfg.state_dir)
        self.ws = Workspace(cfg)

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
        self._lead_waits = 0                   # wait_tasks calls the Lead is blocked in
        self._notes: list[tuple[str, str]] = []  # (from, text) that interrupted a wait
        self._note_news = asyncio.Event()
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
            name=name, role=role, model=rc.model, effort=rc.effort, cwd=self.ws.cwd_for(name),
            system_append=prompt_for(self.cfg, name, self._initial_names, self.ws.isolated),
            session_id=self._previous.get(name) or new_session_id(),
            resume=name in self._previous,
            permission_mode=self.cfg.permission_mode,
            disallowed_tools=list(LEAD_DISALLOWED) if role == "lead" else [],
            tools=self._lead_tools() if role == "lead" else [],
            approver=self._approver if self.cfg.ask else None,
            team=self.name,
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
            if self.ws.isolates(name):
                await self.ws.ensure_worktree(name)
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
        kept = True
        with contextlib.suppress(Exception):
            kept = await self.ws.drop_worktree(name)
        if not kept:
            note += (f" Its unmerged changes are kept in {self.ws.worktree_dir(name)}; "
                     f"adding a {self.cfg.role_of(name)} back brings them back.")
        self._announce(f"{name} was removed from the team by the user and can no longer "
                       f"receive tasks.{note}")
        return name

    @property
    def remote_control_available(self) -> bool:
        return hasattr(self.runners[LEAD].session, "set_remote_control")

    async def set_remote_control(self, enabled: bool) -> str | None:
        """Make the Lead controllable from claude.ai/code and the Claude apps."""
        session: Any = self.runners[LEAD].session
        if not hasattr(session, "set_remote_control"):
            raise ValueError("this backend has no Remote Control")
        name = f"collab · {self.cfg.workdir.name}"
        if self.name != "main":
            name += f" · {self.name}"
        return await session.set_remote_control(enabled, name if enabled else None)

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
        if self.cfg.worktrees and not self.ws.isolated:
            self._ui_emit(Notice(LEAD, "Worktrees need a git repository: the workers share "
                                       "the directory instead.", "warning"))
        for name in self.runners:
            if self.ws.isolates(name):
                try:
                    await self.ws.ensure_worktree(name)
                except Exception as exc:
                    self._ui_emit(Notice(LEAD, f"Could not create {name}'s worktree: {exc}",
                                         "error"))
        await asyncio.gather(*(r.start() for r in self.runners.values()),
                             return_exceptions=True)
        self._lead_loop = asyncio.create_task(self._run_lead_inbox(), name="lead-inbox")

    async def shutdown(self) -> None:
        if self.hub.teams.get(self.name) is self:
            del self.hub.teams[self.name]
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

    def _norm(self, path: str, agent: str | None = None) -> str:
        """A path relative to the project directory (or the agent's own worktree)."""
        root = self.ws.cwd_for(agent) if agent else self.cfg.workdir
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        for base in (root, self.cfg.workdir):
            try:
                return str(p.resolve().relative_to(base.resolve()))
            except ValueError:
                continue
        return str(p.resolve())

    def _running_tasks(self) -> list[TaskRecord]:
        return [t for t in self.tasks.values() if t.status is TaskStatus.RUNNING]

    def _running_everywhere(self) -> list[tuple[Team, TaskRecord]]:
        """Running tasks of this team and of every other team in the directory."""
        return [(team, t) for team in [self, *self.hub.others(self)]
                for t in team._running_tasks()]

    def _label(self, team: Team, task: TaskRecord) -> str:
        where = "" if team is self else f"team {team.name}'s "
        return f"{where}{task.id} ({task.agent})"

    def _track_write(self, agent: str, path: str) -> None:
        task = next((t for t in self._running_tasks() if t.agent == agent), None)
        if task is None:
            return
        rel = self._norm(path, agent)
        if rel not in task.touched_files:
            task.touched_files.append(rel)
            self._ui_emit(TaskUpdate(task.agent, task))
        for team, other in self._running_everywhere():
            if other is task or other.where != task.where:
                continue  # a worker in its own worktree cannot clash
            if rel in other.touched_files or rel in other.planned_files:
                msg = (f"{task.id} ({task.agent}) wrote {rel}, which "
                       f"{self._label(team, other)} is also working on")
                for t in (task, other):
                    if msg not in t.warnings:
                        t.warnings.append(msg)
                self._ui_emit(Notice(LEAD, msg, "warning"))
                if team is not self:
                    team._ui_emit(Notice(LEAD, f"team {self.name}'s {task.id} ({task.agent}) "
                                               f"wrote {rel}, which {other.id} ({other.agent}) "
                                               "is also working on", "warning"))

    # ---- the user talks to the Lead --------------------------------------
    def tell_lead(self, text: str, *, chat: bool = True) -> None:
        """Send the user's message to the Lead. If the Lead is blocked waiting for
        tasks, the message ends the wait so they can talk while the agents work;
        otherwise (or for a /command) it is queued for the Lead's next turn."""
        if chat:
            self.peer_budget = PEER_MESSAGE_BUDGET  # the user is in the loop again
        self._deliver("user", text, chat)

    def receive_from_lead(self, sender: str, text: str) -> None:
        """A message from the Lead of another team in the same directory."""
        self._ui_emit(Notice(LEAD, f"✉ from team {sender}: {text}"))
        self._deliver(sender, text, True)

    def _deliver(self, sender: str, text: str, chat: bool) -> None:
        if chat and self._lead_waits:
            self._notes.append((sender, text))
            self._note_news.set()
        elif sender == "user":
            self._lead_inbox.put_nowait(("user", text))
        else:
            self._lead_inbox.put_nowait(("user", self._peer_header(sender) + text))

    @staticmethod
    def _peer_header(sender: str) -> str:
        return (f"[from the Lead of team {sender}] (another team in this directory; "
                f"answer with message_lead('{sender}', …) only if it needs an answer)\n\n")

    def _take_notes(self) -> str:
        if not self._notes:
            return ""
        parts = [("[user] The user wrote to you:\n\n" if who == "user"
                  else self._peer_header(who)) + text for who, text in self._notes]
        self._notes.clear()
        self._note_news.clear()
        return ("While you were waiting (the tasks above keep running in the background):"
                "\n\n" + "\n\n".join(parts) + "\n\n"
                "Answer now; it does not have to be about the tasks. Then either call "
                "wait_tasks again, or end your turn: every task that finishes will be "
                "reported to you as a [team] message.")

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
        where = self.ws.where(agent)
        same_tree = [(team, t) for team, t in self._running_everywhere() if t.where == where]
        for team, other in same_tree:
            clash = sorted(set(planned) & set(other.planned_files + other.touched_files))
            if clash:
                hint = "" if team is self else \
                    f" (agree with its Lead via message_lead('{team.name}', …))"
                raise ValueError(f"{self._label(team, other)} is already working on "
                                 f"{', '.join(clash)}; run this after it finishes{hint}")
        self._counter += 1
        task = TaskRecord(id=f"T{self._counter}", agent=agent, prompt=prompt,
                          origin=origin, planned_files=planned, where=where)
        # Two tasks changing one tree at once: its snapshots will include both.
        for _, other in same_tree:
            other.overlapped = task.overlapped = True
        self.tasks[task.id] = task
        self._done[task.id] = asyncio.Event()
        self._ui_emit(TaskUpdate(agent, task))
        self._jobs[task.id] = asyncio.create_task(self._run_task(task), name=task.id)
        return task

    async def _run_task(self, task: TaskRecord) -> None:
        runner = self.runners[task.agent]
        tree = self.ws.tree_for(task.agent)
        cancelled = False
        if tree is not None:
            try:
                if self.ws.isolates(task.agent):
                    await self.ws.prepare(task.agent)
                task.snap_before = await tree.snapshot(self.ws.ref(task.id, "before"))
            except Exception as exc:
                self._ui_emit(Notice(LEAD, f"{task.id}: changes not tracked ({exc})",
                                     "warning"))
        try:
            result = await runner.run(task.prompt, task.id)
            task.result = result.text
            task.cost_usd = result.cost_usd
            task.num_turns = result.num_turns
            if task.status is not TaskStatus.CANCELLED:
                task.status = TaskStatus.FAILED if result.is_error else TaskStatus.DONE
        except asyncio.CancelledError:
            task.status = TaskStatus.CANCELLED
            cancelled = True
            raise
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.result = f"{type(exc).__name__}: {exc}"
        finally:
            task.finished_at = time.time()
            if tree is not None and task.snap_before and not cancelled:
                try:
                    task.snap_after = await tree.snapshot(self.ws.ref(task.id, "after"))
                    task.changed_files = await tree.changed(task.snap_before,
                                                            task.snap_after)
                except Exception as exc:
                    self._ui_emit(Notice(LEAD, f"{task.id}: changes not tracked ({exc})",
                                         "warning"))
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
            news = []
            if wake_on_news:
                news = [asyncio.ensure_future(self._news.wait()),
                        asyncio.ensure_future(self._note_news.wait())]
            await asyncio.wait([done_waiting, *news], return_when=asyncio.FIRST_COMPLETED)
            for fut in [done_waiting, *news, *waiters]:
                if not fut.done():
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

    # ---- changes: undo, merge, discard -------------------------------------
    def can_undo(self, task: TaskRecord) -> str | None:
        """Why `task` cannot be undone, or None if it can."""
        if not task.snap_after:
            return "its changes were not tracked (is this a git repository?)"
        if not task.status.finished:
            return "it is still running"
        if task.undone:
            return "it was already undone"
        if not task.changed_files:
            return "it changed no files"
        if task.merged:
            merge = next((t for t in self.tasks.values() if t.origin == "merge"
                          and t.agent == task.agent and t.finished_at
                          and t.finished_at >= (task.finished_at or 0)), None)
            return ("its changes were merged into the main directory: undo the merge"
                    + (f" ({merge.id})" if merge else "") + " instead")
        return None

    async def undo(self, task_id: str, *, force: bool = False) -> list[str]:
        """Put the files `task` changed back the way they were before it ran.
        Raises NeedsConfirmation if some of them changed again since (unless force)."""
        task = self.tasks.get(task_id.strip().upper())
        if task is None:
            raise ValueError(f"unknown task id {task_id!r}")
        why = self.can_undo(task)
        if why:
            raise ValueError(f"{task.id} cannot be undone: {why}")
        tree = Tree(Path(task.where))
        async with self.ws.lock:
            now = await tree.snapshot()
            later = sorted(set(await tree.changed(task.snap_after, now))
                           & set(task.changed_files))
            if later and not force:
                raise NeedsConfirmation(later)
            await tree.put(task.snap_before, task.changed_files)
        task.undone = True
        self._ui_emit(TaskUpdate(task.agent, task))
        with contextlib.suppress(Exception):
            self.store.append_task(task)
        place = "" if task.where == self.ws.where(LEAD) else f" in {task.agent}'s worktree"
        self._announce(f"The user undid {task.id} ({task.agent}){place}: these files are back "
                       f"to how they were before it: {', '.join(task.changed_files)}.")
        return task.changed_files

    async def merge_work(self, agent: str) -> TaskRecord | str:
        """Bring a worker's verified work from its worktree into the main directory."""
        agent = self.resolve_agent(agent)
        if not self.ws.isolates(agent):
            raise ValueError(f"{agent} works in the main directory: nothing to merge")
        if any(t.agent == agent for t in self._running_tasks()):
            raise ValueError(f"{agent} is still working; wait for its task first")
        outcome = await self.ws.merge(agent)
        if outcome.conflicts:
            return (f"CONFLICT: the main directory changed these files since {agent} "
                    f"started: {', '.join(outcome.conflicts)}. They now contain conflict "
                    f"markers in {agent}'s worktree. Dispatch {agent} to resolve them "
                    "(keep both intents), then call merge_work again.")
        if not outcome.files:
            return f"{agent} has no changes waiting to be merged."
        self._counter += 1
        now = time.time()
        merge = TaskRecord(
            id=f"T{self._counter}", agent=agent, origin="merge",
            prompt=f"Merge {agent}'s work into the main directory",
            status=TaskStatus.DONE, started_at=now, finished_at=now,
            where=self.ws.where(LEAD), snap_before=outcome.before, snap_after=outcome.after,
            changed_files=outcome.files,
            result=f"Merged {len(outcome.files)} file(s): {', '.join(outcome.files)}")
        self.tasks[merge.id] = merge
        self._done[merge.id] = asyncio.Event()
        self._done[merge.id].set()
        self._reported.add(merge.id)
        for t in self.tasks.values():
            if t.agent == agent and t.where != merge.where and not t.undone:
                t.merged = True
        self._ui_emit(TaskUpdate(agent, merge))
        with contextlib.suppress(Exception):
            self.store.append_task(merge)
        return merge

    async def discard_work(self, agent: str) -> list[str]:
        agent = self.resolve_agent(agent)
        if not self.ws.isolates(agent):
            raise ValueError(f"{agent} works in the main directory: nothing to discard")
        if any(t.agent == agent for t in self._running_tasks()):
            raise ValueError(f"{agent} is still working; cancel its task first")
        files = await self.ws.discard(agent)
        for t in self.tasks.values():
            if t.agent == agent and t.where != self.ws.where(LEAD) and not t.merged \
                    and not t.undone:
                t.undone = True
                self._ui_emit(TaskUpdate(agent, t))
        return files

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
        files = task.changed_files or task.touched_files
        if files:
            lines.append("files modified: " + ", ".join(files))
            if self.ws.isolates(task.agent) and task.status.finished and not task.merged:
                lines.append(f"(in {task.agent}'s worktree {task.where}; not in the main "
                             f"directory until you call merge_work('{task.agent}'))")
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
        others = self.hub.others(self)
        if others:
            lines.append("other teams in this directory (reach their Lead with message_lead): "
                         + "; ".join(t.brief() for t in others))
        return "\n".join(lines)

    def brief(self) -> str:
        """One line about this team, for the other teams' Leads."""
        lead = self.runners[LEAD].status.value
        running = []
        for t in self._running_tasks():
            files = list(dict.fromkeys(t.planned_files + t.touched_files))
            running.append(f"{t.id} on {t.agent}" + (f" ({', '.join(files)})" if files else ""))
        return f"{self.name} (Lead {lead}" + (", running " + ", ".join(running)
                                              if running else "") + ")"

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
            prompt = str(args.get("task", ""))
            source = str(args.get("worktree_of") or "").strip()
            if source:
                owner = self.resolve_agent(source)
                if not self.ws.isolates(owner):
                    raise ValueError(f"{owner} has no worktree")
                prompt = (f"The change to work on is in {owner}'s git worktree at "
                          f"{self.ws.cwd_for(owner)} — run everything there (cd into it), "
                          "not in your own directory, and do not modify files outside "
                          f"it.\n\n{prompt}")
            task = self.dispatch(str(args.get("agent", "")), prompt,
                                 [str(f) for f in args.get("files") or []])
            return (f"{task.id} started on {task.agent}. Use wait_tasks(['{task.id}']) "
                    "to collect its report.")

        async def wait_tasks(args: dict[str, Any]) -> str:
            ids = [str(t) for t in args.get("task_ids") or []]
            mode = "any" if args.get("mode") == "any" else "all"
            timeout = float(args.get("timeout_s") or 900)
            self._lead_waits += 1
            try:
                records = await self.wait(ids, mode, timeout, wake_on_news=True)
            finally:
                self._lead_waits -= 1
            out = "\n\n".join(self.task_report(r) for r in records) or \
                "No tasks are running."
            # No await from here on: a message sent from now on goes to the inbox instead.
            notes = self._take_notes()
            return f"{out}\n\n{notes}" if notes else out

        async def team_status(args: dict[str, Any]) -> str:
            return self.status_report()

        async def cancel_task(args: dict[str, Any]) -> str:
            task = await self.cancel(str(args.get("task_id", "")))
            return f"{task.id} is {task.status.value}"

        async def merge_work(args: dict[str, Any]) -> str:
            out = await self.merge_work(str(args.get("agent", "")))
            if isinstance(out, str):
                return out
            return (f"{out.id}: merged {len(out.changed_files)} file(s) from {out.agent} "
                    f"into the main directory: {', '.join(out.changed_files)}")

        async def discard_work(args: dict[str, Any]) -> str:
            files = await self.discard_work(str(args.get("agent", "")))
            return ("Discarded: " + ", ".join(files)) if files else "Nothing to discard."

        async def message_lead(args: dict[str, Any]) -> str:
            return self.hub.send(self, str(args.get("team", "")),
                                 str(args.get("message", "")))

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
                    **({"worktree_of": {"type": "string", "description":
                                        "A worker whose worktree the agent should work in "
                                        "(e.g. a tester verifying worker1's change)."}}
                       if self.ws.isolated else {}),
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
            *([ToolSpec(
                "merge_work",
                "Bring a worker's changes from its worktree into the main directory, after "
                "they have been verified. Reports conflicts if the main directory changed "
                "the same files meanwhile.",
                {"type": "object", "properties": {"agent": {"type": "string"}},
                 "required": ["agent"]},
                guarded(merge_work),
            ), ToolSpec(
                "discard_work",
                "Throw away a worker's changes that were not merged (its worktree goes back "
                "to the main directory's state).",
                {"type": "object", "properties": {"agent": {"type": "string"}},
                 "required": ["agent"]},
                guarded(discard_work),
            )] if self.ws.isolated else []),
            ToolSpec(
                "message_lead",
                "Send a one-way message to the Lead of another team working in this "
                "directory (team_status lists them). Use it only for what that team needs "
                "to know or decide: an API/contract change, a shared file, a question. "
                f"At most {PEER_MESSAGE_BUDGET} messages until the user talks to you again.",
                {"type": "object", "properties": {
                    "team": {"type": "string", "description": "the other team's name"},
                    "message": {"type": "string", "description":
                                "Self-contained: the other Lead does not see your conversation."},
                }, "required": ["team", "message"]},
                guarded(message_lead),
            ),
        ]
