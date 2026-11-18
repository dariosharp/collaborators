"""The Textual application: one tab per team, all in the same directory."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any

from textual import events
from textual.app import App, ComposeResult, SuspendNotSupported, SystemCommand
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Input, TabbedContent, TabPane, TextArea

from .. import accounts
from ..agents import Backend, Command
from ..config import DEFAULT_TEAM, ConfigError, TeamConfig, check_team_name
from ..events import Event, LimitReached, TaskRecord
from ..orchestrator import Hub, Team
from ..state import has_saved_team, load_open_teams, save_open_teams
from .commands import CommandInput
from .screens import AccountScreen, ConfirmScreen, HelpScreen, NameScreen, TaskBoardScreen
from .team_view import REMOTE_CMD, TeamView
from .theme import THEMES
from .vim import ModeLine, VimKeys
from .widgets import TopBar

__all__ = ["CollabApp", "REMOTE_CMD"]

MAX_TEAMS = 6
SUGGESTED_NAMES = ("frontend", "backend", "docs", "infra", "api", "web")


class TeamPane(TabPane):
    """A tab. Unlike a plain TabPane, focus moving into it never switches tabs (as
    when a dialog closes and gives the focus back): only the user switches tabs, and
    switching moves the focus — the other way round, the two would chase each other."""

    def _on_descendant_focus(self, event: events.DescendantFocus) -> None:
        pass


class CollabApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "collaborators"
    BINDINGS = [
        Binding("f2", "add_agent('worker')", "+Worker"),
        Binding("f3", "add_agent('tester')", "+Tester"),
        Binding("ctrl+n", "new_team", "New team"),
        Binding("ctrl+g", "next_team", "Next team", show=False),
        Binding("ctrl+t", "tasks", "Tasks"),
        Binding("ctrl+o", "open_focused", "Transcript"),
        Binding("ctrl+x", "interrupt", "Interrupt"),
        Binding("f1", "help", "Help"),
        Binding("ctrl+c", "ctrl_c", "Quit", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True, show=False),
    ]

    def __init__(self, cfg: TeamConfig, backend: Backend, *, resume: bool = False,
                 initial_prompt: str = "", mode_label: str = "",
                 remote_control: bool = False, account: str | None = None) -> None:
        super().__init__()
        # The Claude account every agent runs under (accounts.py); /switch changes it.
        self.account = account or accounts.current()
        try:
            self.account_env = accounts.env_for(self.account)
        except ConfigError:
            self.account, self.account_env = accounts.LOGIN, {}
        self._limit_asked: set[str] = set()  # accounts whose usage limit was reported
        self.vim = VimKeys(self)  # NORMAL-mode keys (Esc out of the input)
        self._normal_until = 0.0  # tab switches until then keep NORMAL mode (gt, :tabn)
        self.cfg = cfg
        self.backend = backend
        self.resume = resume
        self.initial_prompt = initial_prompt
        self.mode_label = mode_label
        self.remote_control_at_start = remote_control
        self.hub = Hub()
        self._views: list[TeamView] = []  # in tab order
        self._quitting = False
        self._last_ctrl_c = 0.0
        self.sent_notifications: list[str] = []

    # ---- layout -----------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield TopBar(self.cfg.workdir, "", self._account_label())
        yield TabbedContent(id="teams")
        yield ModeLine()  # no Footer: the keys are in F1 help and the mode line

    async def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = "collaborators"
        names = (load_open_teams(self.cfg.project_dir) if self.resume else []) or [DEFAULT_TEAM]
        for i, name in enumerate(names):
            await self.open_team(name, resume=self.resume, first=i == 0)
        await self._show(names[0])
        if self.account != accounts.LOGIN and os.environ.get("ANTHROPIC_API_KEY"):
            self.notify("ANTHROPIC_API_KEY is set: Claude Code uses it before any account "
                        "token, so /switch has no effect until you unset it.",
                        title="Accounts", severity="warning", timeout=12)

    @property
    def mode_line(self) -> ModeLine:
        return self.query_one(ModeLine)

    def on_key(self, event: events.Key) -> None:
        """Vim NORMAL mode: keys that reach the app on the main screen while nobody is
        typing (Esc leaves the Lead's input)."""
        if not self.screen_stack or self.screen is not self.screen_stack[0]:
            return
        if isinstance(self.focused, (Input, TextArea)):
            return
        if self.vim.feed(event):
            event.stop()
            event.prevent_default()

    @property
    def views(self) -> list[TeamView]:
        return list(self._views)

    @property
    def view(self) -> TeamView | None:
        """The team in the active tab."""
        try:
            active = self.query_one(TabbedContent).active
        except Exception:  # shutting down
            return self._views[0] if self._views else None
        return next((v for v in self._views if f"tab-{v.team_name}" == active), None)

    def view_of(self, name: str) -> TeamView | None:
        return next((v for v in self.views if v.team_name == name), None)

    # For callers (and tests) that think in terms of "the" team: the active one.
    @property
    def team(self) -> Team | None:
        view = self.view
        return view.team if view else None

    @property
    def tasks(self) -> dict[str, TaskRecord]:
        view = self.view
        return view.tasks if view else {}

    @property
    def history(self) -> dict[str, list[Event]]:
        view = self.view
        return view.history if view else {}

    def commands_for(self, agent: str) -> list[Command]:
        view = self.view
        return view.commands_for(agent) if view else []

    def open_detail(self, agent: str) -> None:
        if self.view:
            self.view.open_detail(agent)

    # ---- tabs ---------------------------------------------------------------------
    async def open_team(self, name: str, *, resume: bool, first: bool = False) -> TeamView:
        cfg = replace(self.cfg, team=name)
        view = TeamView(cfg, self.backend, self.hub, resume=resume,
                        initial_prompt=self.initial_prompt if first else "",
                        remote_control=self.remote_control_at_start and first)
        tabs = self.query_one(TabbedContent)
        self._views.append(view)
        await tabs.add_pane(TeamPane(name, view, id=f"tab-{name}"))
        self._save_tabs()
        return view

    async def _show(self, name: str) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = f"tab-{name}"
        view = self.view_of(name)
        if view is not None:
            self._refresh_chrome()
            view.call_after_refresh(self._focus_if_active, view)

    def _save_tabs(self) -> None:
        with contextlib.suppress(OSError):
            save_open_teams(self.cfg.project_dir, [v.team_name for v in self.views])

    def _refresh_chrome(self) -> None:
        try:
            tabs = self.query_one(TabbedContent)
        except Exception:  # shutting down
            return
        for view in self.views:
            with contextlib.suppress(Exception):
                tab = tabs.get_tab(f"tab-{view.team_name}")
                if str(tab.label) != view.tab_label():  # a new label re-animates the bar
                    tab.label = view.tab_label()
        view = self.view
        if view is not None:
            summary = view.summary()
            if self.mode_label:
                summary += f" · {self.mode_label}"
            self.query_one(TopBar).set_summary(summary)
        # One tab needs no tab bar.
        tabs.set_class(len(self.views) < 2, "-single")

    def on_team_view_changed(self, message: TeamView.Changed) -> None:
        if self._quitting:
            return
        if message.view is self.view:
            self._refresh_chrome()
            return
        with contextlib.suppress(Exception):
            tab = self.query_one(TabbedContent).get_tab(f"tab-{message.view.team_name}")
            if str(tab.label) != message.view.tab_label():
                tab.label = message.view.tab_label()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if self._quitting:
            return
        self._refresh_chrome()
        view = next((v for v in self._views if f"tab-{v.team_name}" == event.pane.id), None)
        if view is not None:
            view.call_after_refresh(self._focus_if_active, view)

    def keep_normal(self) -> None:
        """The tab switch about to happen (gt, :tabn) keeps vim's NORMAL mode."""
        self._normal_until = time.monotonic() + 1.0

    def _focus_if_active(self, view: TeamView) -> None:
        if view is self.view:
            stay = time.monotonic() < self._normal_until
            logs = view.query("#lead-log") if stay else None
            if logs:
                logs.first().focus()
            else:
                view.focus_input()

    def _check_new_name(self, name: str) -> str | None:
        try:
            name = check_team_name(name)
        except ConfigError as exc:
            return str(exc)
        if self.view_of(name) is not None:
            return f"team {name} is already open"
        return None

    def action_new_team(self) -> None:
        if len(self.views) >= MAX_TEAMS:
            self.notify(f"At most {MAX_TEAMS} teams at once.", severity="warning")
            return
        taken = {v.team_name for v in self.views}
        suggestion = next((n for n in SUGGESTED_NAMES if n not in taken), "")

        def answer(name: str | None) -> None:
            if name:
                self.run_worker(self._new_team(name), exit_on_error=False)
            elif self.view is not None:
                self.view.focus_input()

        # Closing the dialog gives the focus back to what had it, and focusing a widget
        # in another tab switches to that tab: so drop the focus first, or the new tab
        # could lose to the old one.
        self.screen.set_focus(None)
        self.push_screen(NameScreen(
            "New team", "A new tab with its own Lead, workers and testers, working in the same "
            "directory. Use a saved name to pick that team up where it left off.",
            suggestion, self._check_new_name), callback=answer)

    async def _new_team(self, name: str) -> None:
        resume = has_saved_team(replace(self.cfg, team=name).state_dir)
        await self.open_team(name, resume=resume)
        await self._show(name)
        self.notify(f"Team {name} {'resumed' if resume else 'started'} in a new tab.",
                    title="Teams")

    def action_next_team(self) -> None:
        views = self.views
        if len(views) < 2 or self.view is None:
            return
        nxt = views[(views.index(self.view) + 1) % len(views)]
        self.run_worker(self._show(nxt.team_name), exit_on_error=False)

    def action_prev_team(self) -> None:
        views = self.views
        if len(views) < 2 or self.view is None:
            return
        prv = views[(views.index(self.view) - 1) % len(views)]
        self.run_worker(self._show(prv.team_name), exit_on_error=False)

    def action_close_team(self, name: str, confirm: bool = True) -> None:
        view = self.view_of(name)
        if view is None:
            return
        if len(self.views) < 2:
            self.notify("This is the only team; quit with Ctrl+C instead.", severity="warning")
            return
        running = sum(not t.status.finished for t in view.tasks.values())
        detail = (f"{running} running task{'s' * (running != 1)} will be stopped. "
                  if running else "") + \
            f"Its conversations are kept: open a team called {name} again to resume it."

        def answer(yes: bool | None) -> None:
            if yes:
                self.run_worker(self._close_team(view), exit_on_error=False)

        if not confirm:
            answer(True)
            return
        self.push_screen(ConfirmScreen(f"Close team {name}?", detail), callback=answer)

    async def _close_team(self, view: TeamView) -> None:
        for screen in list(self.screen_stack[1:]):
            if getattr(screen, "view", None) is view:
                while self.screen is not screen:
                    self.pop_screen()
                self.pop_screen()
                break
        await view.stop()
        self._views.remove(view)
        await self.query_one(TabbedContent).remove_pane(f"tab-{view.team_name}")
        self._save_tabs()
        self._refresh_chrome()
        if self.view is not None:
            self.view.call_after_refresh(self.view.focus_input)

    # ---- actions on the active team -------------------------------------------------
    def action_add_agent(self, role: str) -> None:
        if self.view:
            self.view.add_agent(role)

    def action_remove_agent(self, name: str | None = None) -> None:
        if self.view:
            self.view.remove_agent(name)

    def action_open_focused(self) -> None:
        if self.view:
            self.view.open_detail(self.view.focused_agent())

    def action_interrupt(self) -> None:
        if self.view:
            self.view.interrupt_agent(self.view.focused_agent())

    def set_remote_control(self, enabled: bool) -> None:
        if self.view:
            self.view.set_remote_control(enabled)

    def action_tasks(self) -> None:
        if self.view and not isinstance(self.screen, TaskBoardScreen):
            self.push_screen(TaskBoardScreen(self.view.tasks, self.view))

    # ---- Claude accounts -------------------------------------------------------------
    def _account_label(self) -> str:
        if self.account == accounts.LOGIN and accounts.names() == [accounts.LOGIN]:
            return ""  # only Claude Code's own login: nothing worth showing
        return self.account

    def action_switch_account(self, name: str | None = None, note: str = "",
                              adding: bool = False) -> None:
        """/switch: pick an account (or go straight to `name`); every agent of every
        team carries on with its conversation under it. `adding`: start by adding one."""
        if name is None:
            def chosen(choice: str | None) -> None:
                if choice:
                    self.action_switch_account(choice)
                else:
                    self.query_one(TopBar).set_account(self._account_label())
            self.push_screen(AccountScreen(self.account, note, adding=adding), chosen)
            return
        name = name.strip().lower()
        if name == self.account:
            self.notify(f"Already on the account {name!r}.")
            return
        try:
            env = accounts.env_for(name)
        except ConfigError as exc:
            self.notify(f"{exc}. Type /switch to add it.", title="Accounts",
                        severity="error")
            return
        self.run_worker(self._switch_to(name, env), exit_on_error=False)

    async def _switch_to(self, name: str, env: dict[str, str]) -> None:
        with contextlib.suppress(ConfigError, OSError):
            accounts.set_current(name)
        self.account, self.account_env = name, env
        self._limit_asked.discard(name)
        self.query_one(TopBar).set_account(self._account_label())
        busy = any(v.busy for v in self.views)
        self.notify(f"Switching to the account {name!r}"
                    + (": busy agents switch when their turn ends." if busy else "…"),
                    title="Accounts")
        await asyncio.gather(*(v.switch_account(name, env) for v in self.views),
                             return_exceptions=True)

    def account_updated(self, name: str) -> None:
        """The account `name` was re-added: if the team runs under it, restart the
        agents with its new environment (each keeps its conversation)."""
        if name != self.account:
            return
        try:
            env = accounts.env_for(name)
        except ConfigError as exc:
            self.notify(str(exc), title="Accounts", severity="error")
            return
        self.run_worker(self._switch_to(name, env), exit_on_error=False)

    def login_account(self, name: str, done: Callable[[str], None]) -> None:
        """Add `name` as a claude.ai login account: prepare its config dir, run
        `claude auth login --claudeai` in the terminal (collab steps aside meanwhile),
        check it and register it; then `done(name)`. Where collab cannot step aside,
        it shows the command to run in another terminal instead."""
        try:
            path = accounts.prepare_login_dir(accounts.login_dir(name))
        except OSError as exc:
            self.notify(f"Cannot prepare the account's config dir: {exc}",
                        title="Accounts", severity="error")
            return

        async def verify() -> None:
            try:
                who = await asyncio.to_thread(accounts.check_login, name)
            except ConfigError as exc:
                self.notify(str(exc), title="Accounts", severity="error", timeout=20)
                return
            self.notify(f"{name!r} is logged in with claude.ai ({who}).", title="Accounts")
            done(name)
            self.account_updated(name)

        try:
            with self.suspend():
                print(f"\nLogging the collab account {name!r} in with claude.ai "
                      f"(config dir {path}).\n", flush=True)
                try:
                    subprocess.run(accounts.login_command(), env=accounts.claude_env(path),
                                   check=False)
                except OSError as exc:
                    print(f"collab: cannot run claude: {exc}", flush=True)
        except SuspendNotSupported:
            def answer(yes: bool | None) -> None:
                if yes:
                    self.run_worker(verify(), exit_on_error=False)

            self.push_screen(ConfirmScreen(
                f"Log {name!r} in from another terminal",
                f"Run this, sign in with that Claude account, then press y here:\n\n"
                f"  {accounts.login_hint(path)}"), answer)
            return
        self.run_worker(verify(), exit_on_error=False)

    def limit_reached(self, event: LimitReached) -> None:
        """An agent hit the account's usage limit: offer another account, once."""
        account = self.account
        if account in self._limit_asked:
            return
        self._limit_asked.add(account)
        when = ""
        if event.resets_at:
            when = f" (it resets at {time.strftime('%H:%M', time.localtime(event.resets_at))})"
        self.notify_desktop("collab", f"the Claude account {account} hit its usage limit")
        others = [n for n in accounts.names() if n != account]
        if not others:
            self.notify(f"The Claude account {account!r} hit its usage limit{when}. Add "
                        "another account with /switch to keep going.", title="Usage limit",
                        severity="warning", timeout=15)
            return
        if len(others) > 1:
            self.action_switch_account(note=f"{account!r} hit its usage limit{when}. Pick "
                                            "another account to carry on.")
            return
        target = others[0]

        def answer(yes: bool | None) -> None:
            if yes:
                self.action_switch_account(target)

        self.push_screen(ConfirmScreen(
            f"The account {account!r} hit its usage limit{when}. Switch to {target!r}?",
            "Every agent keeps its conversation; busy ones switch when their turn ends, "
            "and the Lead is told which tasks to pick up again."), answer)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        view = self.view
        yield SystemCommand("New team", "Open another team (Lead, workers, testers) in a new "
                            "tab, in the same directory", self.action_new_team)
        for other in self.views:
            if other is not view:
                yield SystemCommand(f"Go to team {other.team_name}", "Switch tab",
                                    lambda n=other.team_name: self.run_worker(self._show(n)))
        if len(self.views) > 1:
            for v in self.views:
                yield SystemCommand(f"Close team {v.team_name}",
                                    "Stop its agents and close the tab (it can be resumed)",
                                    lambda n=v.team_name: self.action_close_team(n))
        yield SystemCommand("Add a worker", "Start one more worker; the Lead is told at once",
                            lambda: self.action_add_agent("worker"))
        yield SystemCommand("Add a tester", "Start one more tester; the Lead is told at once",
                            lambda: self.action_add_agent("tester"))
        if view is not None and view.team is not None:
            for name in view.team.agent_names[1:]:
                yield SystemCommand(f"Remove {name}", "Take it off the team; the Lead is told",
                                    lambda n=name: self.action_remove_agent(n))
            if view.team.remote_control_available:
                on = bool(view.remote_url)
                yield SystemCommand("Remote Control: " + ("turn off" if on else "turn on"),
                                    "Continue with the Lead from claude.ai/code or the Claude "
                                    "app", lambda: self.set_remote_control(not on))
        yield SystemCommand("Switch account", f"Run the agents under another Claude account "
                            f"(now: {self.account})", lambda: self.action_switch_account())
        yield SystemCommand("Task board", "Every task with status, cost and report",
                            self.action_tasks)

    # ---- desktop notifications ----------------------------------------------------
    def notify_desktop(self, title: str, body: str) -> None:
        """Ring the bell and post a desktop notification (OSC 9: iTerm2, WezTerm, kitty,
        Windows Terminal…). Inside tmux it needs `set -g allow-passthrough on`;
        the bell gets through regardless, and iTerm2 can notify on it too."""
        text = "".join(ch for ch in f"{title}: {body}" if ch.isprintable())[:180]
        seq = f"\x1b]9;{text}\x07"
        if os.environ.get("TMUX"):
            seq = "\x1bPtmux;" + seq.replace("\x1b", "\x1b\x1b") + "\x1b\\"
        self.sent_notifications.append(text)
        driver = getattr(self, "_driver", None)
        if driver is not None and not self.is_headless:
            with contextlib.suppress(Exception):
                driver.write(seq)
                driver.flush()
        self.bell()
        view = self.view
        if view is None or title != ("collab" if view.team_name == DEFAULT_TEAM
                                     else f"collab · {view.team_name}"):
            self.notify(body, title=title, timeout=8)  # from a tab you are not looking at

    # ---- quitting -----------------------------------------------------------------
    async def action_ctrl_c(self) -> None:
        """Like Claude Code: Ctrl+C clears what you are typing; pressed twice, it quits."""
        focused = self.focused
        if isinstance(focused, (Input, CommandInput)) and focused.value:
            focused.value = ""
            return
        now = time.monotonic()
        if now - self._last_ctrl_c < 2.0:
            await self.action_quit()
            return
        self._last_ctrl_c = now
        self.notify("Press Ctrl+C again to quit.", timeout=2)

    async def action_quit(self) -> None:
        if any(v.team is not None for v in self.views):
            self.notify("Stopping agents…")
        await self._stop_teams()
        self.exit()

    async def on_unmount(self) -> None:
        await self._stop_teams()

    async def _stop_teams(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        views = self.views
        with contextlib.suppress(Exception):  # quitting must never hang or crash
            await asyncio.gather(*(v.stop() for v in views), return_exceptions=True)
