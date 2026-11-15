"""The Textual application: wires the Team's events into the widgets."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input

from ..agents import Backend, Command, Decision
from ..config import TeamConfig
from ..events import (
    AgentAdded,
    AgentRemoved,
    AgentStatus,
    Event,
    Notice,
    StatusChanged,
    TaskRecord,
    TaskUpdate,
    TextDone,
    TurnResult,
)
from ..orchestrator import LEAD, Team
from .screens import (
    AgentDetailScreen,
    ApprovalScreen,
    ConfirmScreen,
    HelpScreen,
    TaskBoardScreen,
)
from .theme import THEMES
from .widgets import AgentCard, LeadPanel, StatsBar, TopBar

HISTORY_LIMIT = 3000
MODEL_SET = re.compile(r"Set model to `([^`]+)`")


class CollabApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "collaborators"
    BINDINGS = [
        Binding("f2", "add_agent('worker')", "+Worker"),
        Binding("f3", "add_agent('tester')", "+Tester"),
        Binding("ctrl+t", "tasks", "Tasks"),
        Binding("ctrl+o", "open_focused", "Transcript"),
        Binding("ctrl+x", "interrupt", "Interrupt"),
        Binding("f1", "help", "Help"),
        Binding("ctrl+c", "ctrl_c", "Quit", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True, show=False),
    ]

    def __init__(self, cfg: TeamConfig, backend: Backend, *, resume: bool = False,
                 initial_prompt: str = "", mode_label: str = "") -> None:
        super().__init__()
        self.cfg = cfg
        self.backend = backend
        self.resume = resume
        self.initial_prompt = initial_prompt
        self.mode_label = mode_label
        self.history: dict[str, list[Event]] = defaultdict(list)
        self.tasks: dict[str, TaskRecord] = {}
        self.team: Team | None = None
        self._restart_note = ""
        self._team_stopping = False
        self._last_ctrl_c = 0.0

    # ---- layout -----------------------------------------------------------------
    def _summary(self, names: list[str]) -> str:
        cfg = self.cfg
        workers = sum(cfg.role_of(n) == "worker" for n in names)
        testers = sum(cfg.role_of(n) == "tester" for n in names)
        bits = [f"{workers} worker{'s' * (workers != 1)}",
                f"{testers} tester{'s' * (testers != 1)}"]
        perm = "asks before acting" if cfg.ask else {
            "bypassPermissions": "full auto", "acceptEdits": "auto-edits",
        }.get(cfg.permission_mode, cfg.permission_mode)
        return " · ".join(bits + [perm] + ([self.mode_label] if self.mode_label else []))

    def compose(self) -> ComposeResult:
        cfg = self.cfg
        summary = self._summary(cfg.agent_names)
        yield TopBar(cfg.workdir, summary)
        lead = cfg.role_config(LEAD)
        with Horizontal(id="main"):
            yield LeadPanel(lead.model, lead.effort, cfg.workdir, self.initial_prompt,
                            commands=lambda: self.commands_for(LEAD))
            yield VerticalScroll(id="agents")
        yield StatsBar()
        yield Footer()

    async def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = "collaborators"
        self.team = Team(self.cfg, self.backend, self.on_team_event,
                         resume=self.resume, approver=self.approve)
        for name in self.team.agent_names[1:]:
            self._mount_card(name)
        self.query_one("#lead-input").focus()
        self.run_worker(self._start_team(), name="team-start", exit_on_error=False)

    async def _start_team(self) -> None:
        assert self.team is not None
        await self.team.start()
        lead = self.query_one(LeadPanel)
        if self.team.resumed:
            names = ", ".join(self.team.resumed)
            lead.feed(Notice(LEAD, f"Resumed the previous conversations of {names}."))
            self._restart_note = (
                "[team] The team app was restarted. Every agent kept its conversation, but "
                "tasks that were running before the restart were interrupted; check with "
                "team_status before relying on them.\n\n")
        elif self.resume:
            lead.feed(Notice(LEAD, "Nothing to resume here — started a fresh team.", "warning"))

    # ---- events from the team ------------------------------------------------------
    def on_team_event(self, event: Event) -> None:
        if self._team_stopping:
            return
        hist = self.history[event.agent]
        hist.append(event)
        if len(hist) > HISTORY_LIMIT:
            del hist[: len(hist) - HISTORY_LIMIT]

        if isinstance(event, AgentAdded):
            self._mount_card(event.agent)
            return
        if isinstance(event, AgentRemoved):
            self._unmount_card(event.agent)
            return
        if isinstance(event, TaskUpdate):
            self.tasks[event.task.id] = event.task
            self.query_one(StatsBar).set_task(event.task)
            card = self._card(event.task.agent)
            if card is not None:
                card.set_task(event.task)
            return
        if isinstance(event, TurnResult):
            self.query_one(StatsBar).add_result(event)
        if isinstance(event, TextDone):
            self._follow_model_switch(event)

        target: Any = self.query_one(LeadPanel) if event.agent == LEAD else \
            self._card(event.agent)
        if target is None:
            return
        if isinstance(event, StatusChanged):
            target.status = event.status
            if event.status is AgentStatus.ERROR and event.detail:
                self.notify(event.detail, title=f"{event.agent} error", severity="error")
        else:
            target.feed(event)
        screen = self.screen
        if isinstance(screen, AgentDetailScreen) and not isinstance(event, StatusChanged):
            screen.feed(event)

    def _follow_model_switch(self, event: TextDone) -> None:
        """`/model X` answers "Set model to `X` …": show the new model on the card."""
        m = MODEL_SET.match(event.text)
        if m is None or self.team is None or event.agent not in self.team.runners:
            return
        self.team.runners[event.agent].spec.model = m.group(1)
        target: Any = self.query_one(LeadPanel) if event.agent == LEAD else \
            self._card(event.agent)
        if target is not None:
            target.model = m.group(1)
            target._render_header()

    def _mount_card(self, name: str) -> None:
        if self._card(name) is not None:
            return
        rc = self.cfg.role_config(name)
        card = AgentCard(name, rc.model, rc.effort, self.cfg.workdir)
        agents = self.query_one("#agents", VerticalScroll)
        # Workers above testers, each group in number order.
        def key(n: str) -> tuple[bool, int]:
            return self.cfg.role_of(n) != "worker", int(n.lstrip("abcdefghijklmnopqrstuvwxyz"))
        later = [c for c in agents.query(AgentCard) if key(c.agent) > key(name)]
        if later:
            agents.mount(card, before=later[0])
        else:
            agents.mount(card)
        if self.team is not None:
            self.query_one(TopBar).set_summary(self._summary(self.team.agent_names))

    def _unmount_card(self, name: str) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, AgentDetailScreen) and screen.agent == name:
                while self.screen is not screen:
                    self.pop_screen()
                self.pop_screen()
                break
        card = self._card(name)
        if card is not None:
            card.remove()
        if self.team is not None:
            self.query_one(TopBar).set_summary(self._summary(self.team.agent_names))

    def _card(self, agent: str) -> AgentCard | None:
        try:
            return self.query_one(f"#card-{agent}", AgentCard)
        except Exception:
            return None

    # ---- user actions -------------------------------------------------------------
    def commands_for(self, agent: str) -> list[Command]:
        runner = self.team.runners.get(agent) if self.team is not None else None
        return runner.commands if runner is not None else []

    def on_lead_panel_submitted(self, message: LeadPanel.Submitted) -> None:
        if self.team is None:
            return
        self.query_one(LeadPanel).show_user(message.text)
        if message.text.startswith("/"):
            # A slash command goes to Claude Code exactly as typed.
            if self.check_command(LEAD, message.text):
                self.team.tell_lead(message.text)
            return
        self.team.tell_lead(self._restart_note + message.text)
        self._restart_note = ""

    def on_agent_card_open(self, message: AgentCard.Open) -> None:
        self.open_detail(message.agent)

    def open_detail(self, agent: str) -> None:
        rc = self.cfg.role_config(agent)
        self.push_screen(AgentDetailScreen(agent, rc.model, rc.effort, self.cfg.workdir,
                                           list(self.history[agent])))

    def check_command(self, agent: str, text: str) -> bool:
        """Catch a /command the agent's session doesn't have, instead of sending it
        into the void."""
        rest = text[1:]
        name = rest.split(maxsplit=1)[0] if rest and not rest[0].isspace() else ""
        if not name:
            self.notify("Write the command right after the slash, e.g. /model or "
                        "/code-review.", severity="warning")
            return False
        known = {c.name for c in self.commands_for(agent)}
        if known and name not in known:
            self.notify(f"/{name} isn't available here. Agents run as headless Claude Code "
                        "sessions, so commands that drive the interactive terminal (such as "
                        "/remote-control) don't exist. Type / to see what does.",
                        title="Unknown command", severity="warning", timeout=8)
            return False
        return True

    def send_direct(self, agent: str, text: str) -> None:
        if self.team is None:
            return
        if text.startswith("/") and not self.check_command(agent, text):
            return
        try:
            self.team.dispatch(agent, text, origin="user")
        except ValueError as exc:
            self.notify(str(exc), severity="warning")

    def interrupt_agent(self, agent: str) -> None:
        if self.team is not None:
            self.run_worker(self.team.interrupt(agent), exit_on_error=False)
            self.notify(f"Interrupting {agent}…")

    def _focused_agent(self) -> str:
        node = self.focused
        while node is not None:
            if isinstance(node, AgentCard):
                return node.agent
            node = node.parent  # type: ignore[assignment]
        return LEAD

    def action_open_focused(self) -> None:
        self.open_detail(self._focused_agent())

    def action_interrupt(self) -> None:
        self.interrupt_agent(self._focused_agent())

    def action_add_agent(self, role: str) -> None:
        if self.team is None:
            return

        async def add() -> None:
            assert self.team is not None
            try:
                name = await self.team.add_agent(role)
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
                return
            self.notify(f"{name} joined the team — the Lead has been told.", title="Team")

        self.run_worker(add(), exit_on_error=False)

    def on_agent_card_remove(self, message: AgentCard.Remove) -> None:
        self.action_remove_agent(message.agent)

    def action_remove_agent(self, name: str | None = None) -> None:
        if self.team is None:
            return
        name = name or self._focused_agent()
        if name == LEAD or name not in self.team.runners:
            self.notify("Focus a worker or tester card (Tab) to remove it.", severity="warning")
            return
        task = next((t for t in self.tasks.values()
                     if t.agent == name and not t.status.finished), None)
        detail = (f"It is working on {task.id}, which will be cancelled." if task
                  else "It is idle.") + " The Lead will be told."

        def answer(yes: bool | None) -> None:
            if yes:
                self.run_worker(remove(), exit_on_error=False)

        async def remove() -> None:
            assert self.team is not None
            try:
                await self.team.remove_agent(name)
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
                return
            self.notify(f"{name} left the team — the Lead has been told.", title="Team")

        self.push_screen(ConfirmScreen(f"Remove {name} from the team?", detail), callback=answer)

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand("Add a worker", "Start one more worker; the Lead is told at once",
                            lambda: self.action_add_agent("worker"))
        yield SystemCommand("Add a tester", "Start one more tester; the Lead is told at once",
                            lambda: self.action_add_agent("tester"))
        if self.team is not None:
            for name in self.team.agent_names:
                if name != LEAD:
                    yield SystemCommand(f"Remove {name}", "Take it off the team; the Lead is told",
                                        lambda n=name: self.action_remove_agent(n))
        yield SystemCommand("Task board", "Every task with status, cost and report",
                            self.action_tasks)

    def action_tasks(self) -> None:
        if not isinstance(self.screen, TaskBoardScreen):
            self.push_screen(TaskBoardScreen(self.tasks))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    async def action_ctrl_c(self) -> None:
        """Like Claude Code: Ctrl+C clears what you are typing; pressed twice, it quits."""
        focused = self.focused
        if isinstance(focused, Input) and focused.value:
            focused.value = ""
            return
        now = time.monotonic()
        if now - self._last_ctrl_c < 2.0:
            await self.action_quit()
            return
        self._last_ctrl_c = now
        self.notify("Press Ctrl+C again to quit.", timeout=2)

    async def action_quit(self) -> None:
        if self.team is not None:
            self.notify("Stopping agents…")
        await self._stop_team()
        self.exit()

    async def on_unmount(self) -> None:
        await self._stop_team()

    async def _stop_team(self) -> None:
        self._team_stopping = True
        team, self.team = self.team, None
        if team is not None:
            with contextlib.suppress(Exception):  # quitting must never hang or crash
                await asyncio.wait_for(team.shutdown(), timeout=10)

    # ---- permission prompts (--ask) -------------------------------------------------
    async def approve(self, agent: str, tool: str, args: dict[str, Any]) -> Decision:
        target: Any = self.query_one(LeadPanel) if agent == LEAD else self._card(agent)
        previous = target.status if target is not None else None
        if target is not None:
            target.status = AgentStatus.WAITING
        future: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()

        def done(decision: Decision | None) -> None:
            if not future.done():
                future.set_result(decision or Decision(allow=False))

        self.push_screen(ApprovalScreen(agent, tool, args), callback=done)
        self.bell()
        try:
            return await future
        finally:
            if target is not None and previous is not None:
                target.status = previous
