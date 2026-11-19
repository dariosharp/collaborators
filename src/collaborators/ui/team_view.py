"""One team (a Lead with its workers and testers) as the content of one tab."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import defaultdict
from dataclasses import replace
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message

from .. import accounts
from ..agents import Backend, Command, Decision
from ..config import DEFAULT_MODEL, DEFAULT_TEAM, TeamConfig, is_default_model, is_model_id
from ..events import (
    AgentAdded,
    AgentRemoved,
    AgentStatus,
    Event,
    LimitReached,
    Notice,
    StatusChanged,
    TaskRecord,
    TaskUpdate,
    TextDone,
    TurnResult,
    UsageChanged,
)
from ..orchestrator import LEAD, Hub, Team
from .screens import AgentDetailScreen, ApprovalScreen, ConfirmScreen, describe_account
from .widgets import BUSY_STATUSES, AgentCard, LeadPanel, StatsBar

HISTORY_LIMIT = 3000
# A Lead turn at least this long ends with a desktop notification: long enough that
# you have probably switched to something else, short replies don't ping you.
NOTIFY_AFTER_S = 30.0
REMOTE_CMD = Command("remote-control", "Control the Lead from claude.ai/code or the Claude "
                     "app (collab)", "[off]",
                     options=(("off", "stop Remote Control"),))
SWITCH_CMD = Command("switch", "Switch Claude account; every conversation carries on "
                     "(collab)", "[account]")
MODEL_SET = re.compile(r"Set model to `([^`]+)`")


def model_from_switch(typed: str | None, shown: str) -> str | None:
    """The model to keep after `/model`: what Claude Code accepts, not the display name
    it answers with ("Set model to `Opus 5.5 (default)`"). `typed` is the argument the
    user gave /model, if any. None: nothing usable, keep the current model."""
    if (typed is not None and is_default_model(typed)) or "(default)" in shown.lower():
        return DEFAULT_MODEL
    if typed and is_model_id(typed):
        return typed
    return shown if is_model_id(shown) else None


class TeamView(Vertical):
    """Owns one Team: routes its events into the Lead panel and the agent cards."""

    class Changed(Message):
        """Something shown outside the tab (top bar, tab label) may have changed."""

        def __init__(self, view: TeamView) -> None:
            super().__init__()
            self.view = view

    def __init__(self, cfg: TeamConfig, backend: Backend, hub: Hub, *, resume: bool = False,
                 initial_prompt: str = "", remote_control: bool = False) -> None:
        super().__init__(classes="team-view")
        self.cfg = cfg
        self.team_name = cfg.team
        self.backend = backend
        self.hub = hub
        self.resume = resume
        self.initial_prompt = initial_prompt
        self.remote_control_at_start = remote_control
        self.history: dict[str, list[Event]] = defaultdict(list)
        self.tasks: dict[str, TaskRecord] = {}
        self._model_typed: dict[str, str | None] = {}  # the last `/model X` per agent
        self.team: Team | None = None
        self._restart_note = ""
        self._stopping = False
        self._was_busy = False

    # ---- layout -----------------------------------------------------------------
    def compose(self) -> ComposeResult:
        lead = self.cfg.role_config(LEAD)
        with Horizontal(id="main"):
            yield LeadPanel(lead.model, lead.effort, self.cfg.workdir, self.initial_prompt,
                            commands=lambda: self.commands_for(LEAD))
            yield VerticalScroll(id="agents")
        yield StatsBar()

    def on_mount(self) -> None:
        self.team = Team(self.cfg, self.backend, self.on_team_event, resume=self.resume,
                         approver=self.approve, hub=self.hub,
                         account=getattr(self.app, "account", "login"),
                         env=getattr(self.app, "account_env", {}))
        for name in self.team.agent_names[1:]:
            self._mount_card(name)
        self.set_interval(0.1, self._tick)
        self.run_worker(self._start_team(), name=f"team-start-{self.team_name}",
                        exit_on_error=False)

    def focus_input(self) -> None:
        # Called after a refresh: the tab may have been closed (or not composed) by then.
        found = self.query("#lead-input")
        if self.is_attached and found:
            found.first().focus()

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
        if self.remote_control_at_start:
            self.set_remote_control(True)

    async def stop(self) -> None:
        self._stopping = True
        team, self.team = self.team, None
        if team is not None:
            # stopping must never hang or crash
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(team.shutdown(), timeout=10)

    # ---- how the tab looks from outside -------------------------------------------
    @property
    def busy(self) -> bool:
        leads = self.query(LeadPanel)
        lead_busy = bool(leads) and leads.first().status in BUSY_STATUSES
        return lead_busy or any(not t.status.finished for t in self.tasks.values())

    def tab_label(self) -> str:
        # A fixed mark, not a spinner: changing a tab's label re-animates the tab bar.
        return f"{'●' if self.busy else '○'} {self.team_name}"

    def _tick(self) -> None:
        busy = self.busy
        if busy != self._was_busy:
            self._was_busy = busy
            self.post_message(self.Changed(self))

    def summary(self) -> str:
        cfg = self.cfg
        names = self.team.agent_names if self.team else cfg.agent_names
        workers = sum(cfg.role_of(n) == "worker" for n in names)
        testers = sum(cfg.role_of(n) == "tester" for n in names)
        bits = [f"{workers} worker{'s' * (workers != 1)}",
                f"{testers} tester{'s' * (testers != 1)}"]
        perm = "asks before acting" if cfg.ask else {
            "bypassPermissions": "full auto", "acceptEdits": "auto-edits",
        }.get(cfg.permission_mode, cfg.permission_mode)
        team = [] if self.team_name == DEFAULT_TEAM and len(self.hub.teams) < 2 else \
            [f"team {self.team_name}"]
        return " · ".join(team + bits + [perm])

    # ---- events from the team ------------------------------------------------------
    def on_team_event(self, event: Event) -> None:
        if self._stopping:
            return
        if not isinstance(event, UsageChanged):  # transcript events only
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
            self.post_message(self.Changed(self))
            return
        if isinstance(event, TurnResult):
            self.query_one(StatsBar).add_result(event)
            if event.agent == LEAD:
                self._notify_done(event)
        if isinstance(event, TextDone):
            self._follow_model_switch(event)
        if isinstance(event, LimitReached):
            limit = getattr(self.app, "limit_reached", None)
            if limit is not None:
                limit(event)

        target: Any = self.query_one(LeadPanel) if event.agent == LEAD else \
            self._card(event.agent)
        if target is None:
            return
        if isinstance(event, StatusChanged):
            target.status = event.status
            if event.agent == LEAD:
                self.post_message(self.Changed(self))
            if event.status is AgentStatus.ERROR and event.detail:
                self.notify(event.detail, title=f"{self._who(event.agent)} error",
                            severity="error")
        elif isinstance(event, UsageChanged):
            target.set_usage(event.usage)
            return
        else:
            target.feed(event)
        screen = self.app.screen
        if isinstance(screen, AgentDetailScreen) and screen.view is self \
                and not isinstance(event, StatusChanged):
            screen.feed(event)

    def _notify_done(self, event: TurnResult) -> None:
        if not self.cfg.notify or event.duration_ms < NOTIFY_AFTER_S * 1000:
            return
        lines = [ln.strip(" #*") for ln in event.text.splitlines() if ln.strip(" #*")]
        body = "error: " + event.subtype if event.is_error else \
            (lines[0] if lines else "done")
        title = "collab" if self.team_name == DEFAULT_TEAM else f"collab · {self.team_name}"
        notify = getattr(self.app, "notify_desktop", None)
        if notify:
            notify(title, body)

    def _who(self, agent: str) -> str:
        return agent if self.team_name == DEFAULT_TEAM else f"{self.team_name}/{agent}"

    def _follow_model_switch(self, event: TextDone) -> None:
        """`/model X` answers "Set model to `X` …": show the new model on the card."""
        m = MODEL_SET.match(event.text)
        if m is None or self.team is None or event.agent not in self.team.runners:
            return
        model = model_from_switch(self._model_typed.pop(event.agent, None), m.group(1))
        if model is not None:
            self.team.model_chosen(event.agent, model)
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

        def key(n: str) -> tuple[bool, int]:  # workers above testers, in number order
            return self.cfg.role_of(n) != "worker", int(n.lstrip("abcdefghijklmnopqrstuvwxyz"))

        later = [c for c in agents.query(AgentCard) if key(c.agent) > key(name)]
        if later:
            agents.mount(card, before=later[0])
        else:
            agents.mount(card)
        self.post_message(self.Changed(self))

    def _unmount_card(self, name: str) -> None:
        for screen in self.app.screen_stack:
            if isinstance(screen, AgentDetailScreen) and screen.view is self \
                    and screen.agent == name:
                while self.app.screen is not screen:
                    self.app.pop_screen()
                self.app.pop_screen()
                break
        card = self._card(name)
        if card is not None:
            card.remove()
        self.post_message(self.Changed(self))

    def _card(self, agent: str) -> AgentCard | None:
        try:
            return self.query_one(f"#card-{agent}", AgentCard)
        except Exception:
            return None

    # ---- user actions -------------------------------------------------------------
    def commands_for(self, agent: str) -> list[Command]:
        runner = self.team.runners.get(agent) if self.team is not None else None
        if runner is None:
            return []
        extra = [REMOTE_CMD] if agent == LEAD and self.team.remote_control_available else []
        if agent == LEAD:
            extra.append(replace(SWITCH_CMD, options=self._account_options()))
        ours = {c.name for c in extra}
        models = tuple(runner.models)
        return extra + [replace(c, options=models) if c.name == "model" and models else c
                        for c in runner.commands if c.name not in ours]

    def _account_options(self) -> tuple[tuple[str, str], ...]:
        """/switch completion: the other accounts first, the one in use last."""
        current = getattr(self.app, "account", accounts.LOGIN)
        names = accounts.names()
        def describe(name: str) -> str:
            return describe_account(name) + (" · in use" if name == current else "")
        listed = [n for n in names if n != current] + [n for n in names if n == current]
        return (*((n, describe(n)) for n in listed),
                (accounts.ADD, "add an account: claude.ai login or a setup-token"))

    @property
    def remote_url(self) -> str | None:
        return self.query_one(LeadPanel).remote_url

    def set_remote_control(self, enabled: bool) -> None:
        """Make the Lead reachable from claude.ai/code and the Claude apps (or stop)."""
        if self.team is None:
            return
        lead = self.query_one(LeadPanel)

        async def switch() -> None:
            assert self.team is not None
            try:
                url = await self.team.set_remote_control(enabled)
            except Exception as exc:
                lead.feed(Notice(LEAD, f"Remote Control failed: {exc}", "error"))
                self.notify(str(exc), title="Remote Control", severity="error")
                return
            lead.remote_url = url
            lead._render_header()
            if enabled:
                lead.feed(Notice(LEAD, f"Remote Control on — continue from {url} or from "
                                       "the Claude app. Turn it off with /remote-control off."))
                self.notify(f"{url}", title="Remote Control on", timeout=10)
            else:
                lead.feed(Notice(LEAD, "Remote Control off."))

        self.run_worker(switch(), exit_on_error=False)

    async def switch_account(self, account: str, env: dict[str, str]) -> None:
        if self.team is None:
            return
        await self.team.switch_account(account, env)
        lead = self.query_one(LeadPanel)
        lead.remote_url = getattr(self.team.runners[LEAD].session, "remote_url", None)
        lead._render_header()

    def on_lead_panel_submitted(self, message: LeadPanel.Submitted) -> None:
        message.stop()
        if self.team is None:
            return
        self.query_one(LeadPanel).show_user(message.text)
        if message.text.startswith("/"):
            # A slash command goes to Claude Code exactly as typed.
            parts = message.text[1:].split()
            if parts and parts[0] == REMOTE_CMD.name and self.team.remote_control_available:
                self.set_remote_control(not (len(parts) > 1 and parts[1] == "off"))
            elif parts and parts[0] == SWITCH_CMD.name:
                arg = parts[1].lower() if len(parts) > 1 else None
                if arg == accounts.ADD:
                    self.app.action_switch_account(adding=True)
                else:
                    self.app.action_switch_account(arg)
            elif self.check_command(LEAD, message.text):
                self.team.tell_lead(message.text, chat=False)
            return
        self.team.tell_lead(self._restart_note + message.text)
        self._restart_note = ""

    def on_agent_card_open(self, message: AgentCard.Open) -> None:
        message.stop()
        self.open_detail(message.agent)

    def on_agent_card_remove(self, message: AgentCard.Remove) -> None:
        message.stop()
        self.remove_agent(message.agent)

    def open_detail(self, agent: str) -> None:
        rc = self.cfg.role_config(agent)
        model = rc.model
        if self.team is not None and agent in self.team.runners:
            model = self.team.runners[agent].spec.model
        self.app.push_screen(AgentDetailScreen(self, agent, model, rc.effort, self.cfg.workdir,
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
        if name == "model":  # what the user typed is what Claude Code accepts
            args = rest.split()[1:]
            self._model_typed[agent] = args[0] if args else None
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
            self.notify(f"Interrupting {self._who(agent)}…")

    def focused_agent(self) -> str:
        node = self.app.focused
        while node is not None:
            if isinstance(node, AgentCard):
                return node.agent
            node = node.parent  # type: ignore[assignment]
        return LEAD

    def add_agent(self, role: str) -> None:
        if self.team is None:
            return

        async def add() -> None:
            assert self.team is not None
            try:
                name = await self.team.add_agent(role)
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
                return
            self.notify(f"{self._who(name)} joined the team — the Lead has been told.",
                        title="Team")

        self.run_worker(add(), exit_on_error=False)

    def remove_agent(self, name: str | None = None, confirm: bool = True) -> None:
        if self.team is None:
            return
        name = name or self.focused_agent()
        if name and self.team is not None and name != LEAD:
            with contextlib.suppress(ValueError):
                name = self.team.resolve_agent(name)
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
            self.notify(f"{self._who(name)} left the team — the Lead has been told.",
                        title="Team")

        if not confirm:
            answer(True)
            return
        self.app.push_screen(ConfirmScreen(f"Remove {self._who(name)} from the team?", detail),
                             callback=answer)

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

        self.app.push_screen(ApprovalScreen(self._who(agent), tool, args), callback=done)
        notify = getattr(self.app, "notify_desktop", None)
        if notify and self.cfg.notify:
            notify("collab", f"{self._who(agent)} asks to use {tool}")
        else:
            self.app.bell()
        try:
            return await future
        finally:
            if target is not None and previous is not None:
                target.status = previous
