"""Vim style keys on the main screen.

There is no mode state: the mode follows the focus. Typing in the Lead's input is
INSERT; Esc leaves it for the Lead's log, and anywhere else on the main screen is
NORMAL. In NORMAL, `:` opens a command line (`:split`, `:q`, `:tabnew`…), Ctrl+W
works on the "windows" (the Lead and the agent cards), and j/k, gg/G, Ctrl+D/U,
gt/gT move around. Every existing shortcut keeps working as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import events
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Input, Static, TextArea

from ..config import ConfigError, check_team_name
from ..orchestrator import LEAD
from .screens import ConfirmScreen
from .widgets import AgentCard

if TYPE_CHECKING:
    from .app import CollabApp
    from .team_view import TeamView

HINT = ":commands  i type  ^W s/v +worker/+tester  ^W q close  gt next tab"


def agent_of(node: Any) -> str:
    """The agent a widget belongs to: a card's agent, or the Lead."""
    while node is not None:
        if isinstance(node, AgentCard):
            return node.agent
        node = node.parent
    return LEAD


def abbrev(cmd: str, full: str, least: int) -> bool:
    """Vim abbreviations: `cmd` is `full`, or a prefix of it at least `least` long."""
    return len(cmd) >= least and full.startswith(cmd)


# ---- normal-mode keys ------------------------------------------------------------

class VimKeys:
    def __init__(self, app: CollabApp) -> None:
        self.app = app
        self.pending = ""  # "g" or "ctrl+w" while a two-key command is half typed

    @property
    def showcmd(self) -> str:
        return {"ctrl+w": "^W", "g": "g"}.get(self.pending, "")

    def feed(self, event: events.Key) -> bool:
        """Handle a key typed in NORMAL mode. Returns True if it was used."""
        app, view = self.app, self.app.view
        if view is None:
            return False
        key, char = event.key, event.character or ""
        pending, self.pending = self.pending, ""
        if pending == "ctrl+w":
            self._window(view, char if char.isprintable() and char else key)
            return True
        if pending == "g":
            if char == "g":
                self._scroll(view, "home")
            elif char == "t":
                app.keep_normal()
                app.action_next_team()
            elif char == "T":
                app.keep_normal()
                app.action_prev_team()
            return True
        if key == "ctrl+w":
            self.pending = "ctrl+w"
            return True
        if key == "ctrl+d":
            self._scroll(view, "half-down")
            return True
        if key == "ctrl+u":
            self._scroll(view, "half-up")
            return True
        if key == "enter":
            if isinstance(app.focused, AgentCard):
                return False  # the card's own Enter opens its transcript
            view.focus_input()
            return True
        if char == "g":
            self.pending = "g"
            return True
        if char == ":":
            app.mode_line.open()
            return True
        if char in ("i", "a", "o"):
            view.focus_input()
            return True
        if char in ("j", "k", "G"):
            self._scroll(view, {"j": "down", "k": "up", "G": "end"}[char])
            return True
        return False

    # ---- scrolling ---------------------------------------------------------------
    def _log(self, view: TeamView) -> Widget | None:
        """The transcript of the selected pane: the focused card's, or the Lead's."""
        node = self.app.focused
        if node is not None and node.has_class("transcript"):
            return node
        if isinstance(node, AgentCard):
            logs = node.query(".transcript")
            return logs.first() if logs else None
        logs = view.query("#lead-log")
        return logs.first() if logs else None

    def _scroll(self, view: TeamView, how: str) -> None:
        log = self._log(view)
        if log is None:
            return
        half = max(1, log.size.height // 2)
        if how == "home":
            log.scroll_home(animate=False)
        elif how == "end":
            log.scroll_end(animate=False)
        else:
            step = {"down": 1, "up": -1, "half-down": half, "half-up": -half}[how]
            log.scroll_relative(y=step, animate=False)

    # ---- windows (Ctrl+W …) --------------------------------------------------------
    def _panes(self, view: TeamView) -> list[Widget]:
        lead = list(view.query("#lead-log"))
        return lead + list(view.query(AgentCard))

    def _window(self, view: TeamView, key: str) -> None:
        app = self.app
        if key == "s":
            app.action_add_agent("worker")
            return
        if key == "v":
            app.action_add_agent("tester")
            return
        if key in ("q", "c"):
            run_ex(app, "q", app.focused)
            return
        panes = self._panes(view)
        if not panes:
            return
        here = agent_of(app.focused)
        idx = next((i for i, p in enumerate(panes)
                    if isinstance(p, AgentCard) and p.agent == here), 0)
        target = {
            "w": (idx + 1) % len(panes), "W": (idx - 1) % len(panes),
            "h": 0, "l": min(1, len(panes) - 1),
            "j": min(idx + 1, len(panes) - 1), "k": max(idx - 1, 0),
        }.get(key)
        if target is not None:
            panes[target].focus()


# ---- ex commands -----------------------------------------------------------------

def run_ex(app: CollabApp, text: str, focus: Any = None) -> None:
    """Run a `:` command. `focus` is what had the focus before the command line
    opened: `:q` and friends act on that pane."""
    text = text.strip().lstrip(":").strip()
    if not text:
        return
    head, _, arg = text.partition(" ")
    arg = arg.strip()
    bang = head.endswith("!")
    cmd = head.rstrip("!")
    view = app.view
    if view is None or view.team is None:
        return
    here = agent_of(focus)

    def count() -> int:
        try:
            return max(1, min(int(arg), 8)) if arg else 1
        except ValueError:
            return 1

    if cmd == "worker" or abbrev(cmd, "split", 2):
        for _ in range(count()):
            app.action_add_agent("worker")
    elif cmd == "tester" or abbrev(cmd, "vsplit", 2):
        for _ in range(count()):
            app.action_add_agent("tester")
    elif abbrev(cmd, "qall", 2) or abbrev(cmd, "wqall", 3) or abbrev(cmd, "xall", 2):
        quit_all(app, bang)
    elif abbrev(cmd, "quit", 1) or abbrev(cmd, "close", 3):
        if here != LEAD:
            view.remove_agent(here, confirm=not bang)
        elif len(app.views) > 1:
            app.action_close_team(view.team_name, confirm=not bang)
        else:
            quit_all(app, bang)
    elif cmd in ("rm", "bd") or abbrev(cmd, "bdelete", 2):
        view.remove_agent(arg or here, confirm=not bang)
    elif cmd == "tabnew" or abbrev(cmd, "tabedit", 4):
        new_tab(app, arg)
    elif abbrev(cmd, "tabclose", 4):
        name = arg or view.team_name
        if app.view_of(name) is None:
            app.notify(f"No tab called {name!r}.", severity="warning")
        else:
            app.action_close_team(name, confirm=not bang)
    elif abbrev(cmd, "tabnext", 4):
        app.keep_normal()
        app.action_next_team()
    elif abbrev(cmd, "tabprevious", 4) or abbrev(cmd, "tabNext", 4):
        app.keep_normal()
        app.action_prev_team()
    elif abbrev(cmd, "edit", 1) or cmd == "open":
        name = arg or here
        try:
            name = name if name.strip().lower() == LEAD else view.team.resolve_agent(name)
        except ValueError as exc:
            app.notify(str(exc), severity="warning")
            return
        view.open_detail(name.strip().lower())
    elif cmd == "stop" or abbrev(cmd, "interrupt", 3):
        view.interrupt_agent(arg.strip().lower() or here)
    elif cmd == "tasks":
        app.action_tasks()
    elif cmd == "switch":
        app.action_switch_account(arg or None)
    elif cmd == "remote":
        view.set_remote_control(arg != "off")
    elif abbrev(cmd, "help", 1):
        app.action_help()
    else:
        app.notify(f"E492: Not an editor command: {text}", severity="error")


def changes_tab(app: Any, text: str, focus: Any) -> bool:
    """Whether the `:` command `text` opens, closes or switches tabs."""
    cmd = text.strip().lstrip(":").split(" ", 1)[0].rstrip("!")
    if cmd.startswith("tab"):
        return True
    quits = abbrev(cmd, "quit", 1) or abbrev(cmd, "close", 3)
    return quits and agent_of(focus) == LEAD and len(app.views) > 1


def quit_all(app: CollabApp, bang: bool) -> None:
    if bang or not any(v.busy for v in app.views):
        app.run_worker(app.action_quit(), exit_on_error=False)
        return

    def answer(yes: bool | None) -> None:
        if yes:
            app.run_worker(app.action_quit(), exit_on_error=False)

    app.push_screen(ConfirmScreen("Quit collab?", "Agents are still working and will be "
                                  "stopped. `collab --resume` brings every conversation back. "
                                  "(:qa! quits without asking.)"), answer)


def new_tab(app: CollabApp, name: str) -> None:
    if not name:
        app.action_new_team()
        return
    error = app._check_new_name(name)
    if error:
        app.notify(error, severity="warning")
        return
    try:
        name = check_team_name(name)
    except ConfigError as exc:  # already checked; keeps the type checker honest
        app.notify(str(exc), severity="warning")
        return
    app.run_worker(app._new_team(name), exit_on_error=False)


# ---- the bottom line: mode, pending keys, and the `:` command line ------------------

class ModeLine(Horizontal):
    def __init__(self) -> None:
        super().__init__(id="modeline")
        self.history: list[str] = []
        self._hpos = 0
        self._prev: Widget | None = None
        self._shown: Text | None = None

    def compose(self):
        yield Static(id="mode")
        yield Input(id="cmdline")

    def on_mount(self) -> None:
        self.query_one("#cmdline", Input).display = False
        self.set_interval(0.1, self.refresh_mode)
        self.refresh_mode()

    @property
    def is_open(self) -> bool:
        return self.query_one("#cmdline", Input).display

    def open(self) -> None:
        self._prev = self.app.focused
        inp = self.query_one("#cmdline", Input)
        inp.value = ""
        inp.display = True
        inp.focus()
        self._hpos = len(self.history)
        self.refresh_mode()

    def close(self, restore: bool = True) -> None:
        """Hide the command line. The focus goes back where it was, unless the
        command moves to another tab: focusing a widget of the old tab would pull
        the view back there, so the tab switch places the focus instead."""
        inp = self.query_one("#cmdline", Input)
        inp.display = False
        inp.value = ""
        prev, self._prev = self._prev, None
        app: Any = self.app
        view = app.view
        if not restore:
            app.screen.set_focus(None)
            self.set_timer(0.5, self._ensure_focus)
        elif prev is not None and prev.is_attached and view is not None \
                and view in prev.ancestors_with_self:
            prev.focus()
        else:
            logs = view.query("#lead-log") if view is not None else []
            if logs:
                logs.first().focus()
        self.refresh_mode()

    def _ensure_focus(self) -> None:
        """After a tab command that changed nothing (a typo), focus the log again."""
        app: Any = self.app
        if app.focused is None and app.screen is app.screen_stack[0] and app.view:
            logs = app.view.query("#lead-log")
            if logs:
                logs.first().focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        prev = self._prev
        self.close(restore=not changes_tab(self.app, text, prev))
        if text:
            if not self.history or self.history[-1] != text:
                self.history.append(text)
            run_ex(self.app, text, prev)  # type: ignore[arg-type]

    def on_key(self, event: events.Key) -> None:
        if not self.is_open:
            return
        inp = self.query_one("#cmdline", Input)
        if event.key == "escape":
            self.close()
        elif event.key in ("up", "down") and self.history:
            step = -1 if event.key == "up" else 1
            self._hpos = max(0, min(len(self.history), self._hpos + step))
            inp.value = self.history[self._hpos] if self._hpos < len(self.history) else ""
            inp.cursor_position = len(inp.value)
        else:
            return
        event.stop()
        event.prevent_default()

    def refresh_mode(self) -> None:
        app: Any = self.app
        if not app.screen_stack or app.screen is not app.screen_stack[0]:
            return
        t = Text(no_wrap=True, overflow="ellipsis")
        if self.is_open:
            t.append(":", style="bold")
        elif isinstance(app.focused, (TextArea, Input)):
            t.append("-- INSERT --", style="bold")
            t.append("   Esc normal mode (vim keys, :commands)", style="dim")
        else:
            t.append("-- NORMAL --", style="bold reverse")
            t.append(f"   {HINT}", style="dim")
            showcmd = app.vim.showcmd
            if showcmd:
                t.append(f"   {showcmd}", style="bold")
        if t != self._shown:
            self._shown = t
            self.query_one("#mode", Static).update(t)
