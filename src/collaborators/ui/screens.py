"""Secondary screens: agent detail, task board, permission prompt, help."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from .. import accounts
from ..agents import Decision
from ..config import ConfigError, check_team_name
from ..events import Event, TaskRecord, TaskStatus
from ..orchestrator import fmt_duration
from .commands import CommandInput, CommandMenu
from .widgets import Transcript, short_model

LOGIN_KIND, TOKEN_KIND = "claude.ai", "token"  # what accounts.kind() returns


def describe_account(name: str) -> str:
    """How an account signs in, for the account lists."""
    try:
        how = accounts.kind(name)
    except ConfigError:
        return ""
    if how == accounts.LOGIN:
        return "Claude Code's own login"
    if how == LOGIN_KIND:
        return "claude.ai login" if accounts.is_logged_in(accounts.login_dir(name)) \
            else "claude.ai login (logged out: re-add it)"
    return "setup-token (no Remote Control)"


class AgentDetailScreen(Screen[None]):
    """Full transcript of one agent, with diffs and command output, and a direct line."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+x", "interrupt", "Interrupt"),
    ]

    def __init__(self, view: Any, agent: str, model: str, effort: str, workdir: Path,
                 history: list[Event]) -> None:
        super().__init__(classes=agent.rstrip("0123456789"))
        self.view = view  # the TeamView (tab) the agent belongs to
        self.agent = agent
        self.model = model
        self.effort = effort
        self.workdir = workdir
        self.history = history

    def compose(self) -> ComposeResult:
        head = Text(no_wrap=True)
        team = self.view.team_name
        who = self.agent if team == "main" else f"{team} · {self.agent}"
        head.append(f" ◆ {who} ", style="bold reverse")
        head.append(f"  {short_model(self.model)} · {self.effort}", style="dim")
        head.append("   full transcript", style="italic dim")
        yield Static(head, id="detail-header")
        yield Transcript(expanded=True, workdir=self.workdir, id="detail-log")
        if self.agent != "lead":
            yield CommandMenu(id="detail-input-menu")
            yield CommandInput(lambda: self.view.commands_for(self.agent),
                               menu_id="detail-input-menu",
                               placeholder=f"Message {self.agent} directly, or / for commands "
                                           "and skills…", id="detail-input")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one(Transcript)
        for event in self.history:
            log.feed(event)
        log.call_after_refresh(log.scroll_end, animate=False)

    def feed(self, event: Event) -> None:
        if event.agent == self.agent:
            self.query_one(Transcript).feed(event)

    def on_command_input_submitted(self, event: CommandInput.Submitted) -> None:
        text = event.value.strip()
        if text:
            event.input.value = ""
            self.view.send_direct(self.agent, text)

    def action_interrupt(self) -> None:
        self.view.interrupt_agent(self.agent)


class TaskBoardScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("d", "diff", "Diff"),
        Binding("u", "undo", "Undo task"),
    ]

    def __init__(self, tasks: dict[str, TaskRecord], view: Any = None) -> None:
        super().__init__()
        self.tasks = tasks
        self.view = view  # the TeamView: lets the board undo tasks
        self.current: str | None = None
        self.showing_diff = False

    def compose(self) -> ComposeResult:
        yield Static(Text(" ☰ Tasks ", style="bold reverse"), id="detail-header")
        with Horizontal(id="board"):
            yield DataTable(id="board-table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="board-report"):
                yield Static(id="board-report-body")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Task", "Agent", "Status", "Time", "Cost", "Files")
        self.refresh_table()
        self.set_interval(1, self.refresh_table)
        table.focus()

    def refresh_table(self) -> None:
        table = self.query_one(DataTable)
        keep = table.cursor_row
        table.clear()
        for task in sorted(self.tasks.values(), key=lambda t: int(t.id[1:]), reverse=True):
            status = "merge" if task.origin == "merge" else task.verdict or task.status.value
            if task.undone:
                status += " ↶"
            style = {"PASS": "bold green", "FAIL": "bold red", "running": "bold",
                     "failed": "red", "cancelled": "yellow", "merge": "bold cyan"}.get(
                         status.split()[0], "")
            files = task.changed_files or task.touched_files
            table.add_row(task.id, task.agent, Text(status, style=style),
                          fmt_duration(task.elapsed), f"${task.cost_usd:.2f}",
                          ", ".join(files) or "—", key=task.id)
        if table.row_count:
            table.move_cursor(row=min(keep, table.row_count - 1))
        else:
            self.query_one("#board-report-body", Static).update(
                Text("No tasks yet. Ask the Lead for something.", style="dim italic"))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if str(event.row_key.value) != self.current:
            self.current = str(event.row_key.value)
            self.showing_diff = False
        if not self.showing_diff:
            self.show_report()

    def show_report(self) -> None:
        task = self.tasks.get(self.current or "")
        if task is None:
            return
        body = f"### {task.id} · {task.agent} · {task.status.value}\n\n"
        body += f"**Task**\n\n{task.prompt}\n\n"
        if task.changed_files:
            state = (" — undone" if task.undone else
                     " — merged into the main directory" if task.merged else "")
            body += f"**Changed files**{state}\n\n" + "\n".join(
                f"- `{f}`" for f in task.changed_files) + "\n\n"
            if task.overlapped:
                body += ("_Other tasks changed the same directory at the same time: this "
                         "list may include their changes._\n\n")
            body += "_`d` shows the diff, `u` undoes the task._\n\n"
        if task.warnings:
            body += "**Warnings**\n\n" + "\n".join(f"- {w}" for w in task.warnings) + "\n\n"
        if task.status is not TaskStatus.RUNNING:
            body += f"**Report**\n\n{task.result or '_(empty)_'}"
        self.query_one("#board-report-body", Static).update(Markdown(body))

    def action_diff(self) -> None:
        task = self.tasks.get(self.current or "")
        if task is None:
            return
        if self.showing_diff:
            self.showing_diff = False
            self.show_report()
            return
        if not (task.snap_before and task.snap_after and task.changed_files):
            self.notify("No tracked changes for this task.", severity="warning")
            return

        async def load() -> None:
            from rich.syntax import Syntax

            from ..gitsnap import Tree
            try:
                patch = await Tree(Path(task.where)).diff(task.snap_before, task.snap_after,
                                                          task.changed_files)
            except Exception as exc:
                self.notify(str(exc), title="Diff", severity="error")
                return
            self.showing_diff = True
            self.query_one("#board-report-body", Static).update(
                Syntax(patch or "(no differences)", "diff", theme="ansi_dark",
                       background_color="default"))

        self.run_worker(load(), exit_on_error=False)

    def action_undo(self) -> None:
        task = self.tasks.get(self.current or "")
        team = getattr(self.view, "team", None)
        if task is None or team is None:
            return
        why = team.can_undo(task)
        if why:
            self.notify(f"{task.id} cannot be undone: {why}.", severity="warning")
            return
        from ..orchestrator import NeedsConfirmation

        async def undo(force: bool) -> None:
            try:
                files = await team.undo(task.id, force=force)
            except NeedsConfirmation as exc:
                self.app.push_screen(ConfirmScreen(
                    "These files changed again after the task:",
                    ", ".join(exc.files) + ". Undoing puts them back to how they were "
                    "before the task, losing those later changes too. Go ahead?"),
                    callback=lambda yes: yes and self.run_worker(undo(True)))
                return
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
                return
            self.notify(f"{task.id} undone: {', '.join(files)}. The Lead has been told.",
                        title="Undo")
            self.refresh_table()
            self.show_report()

        where = "" if not task.where else f" in {Path(task.where).name}"
        self.app.push_screen(ConfirmScreen(
            f"Undo {task.id} ({task.agent})?",
            f"Puts back {len(task.changed_files)} file(s){where} as they were before it: "
            + ", ".join(task.changed_files)),
            callback=lambda yes: yes and self.run_worker(undo(False)))


class ApprovalScreen(ModalScreen[Decision]):
    BINDINGS = [
        Binding("y", "decide('allow')", "Allow"),
        Binding("a", "decide('always')", "Always"),
        Binding("n", "decide('deny')", "Deny"),
        Binding("escape", "decide('deny')", "Deny", show=False),
    ]

    def __init__(self, agent: str, tool: str, args: dict[str, Any]) -> None:
        super().__init__()
        self.agent = agent
        self.tool = tool
        self.args = args

    def compose(self) -> ComposeResult:
        with Vertical(id="approval"):
            title = Text()
            title.append(f"{self.agent}", style="bold")
            title.append(" wants to use ")
            title.append(self.tool, style="bold")
            yield Static(title, id="approval-title")
            body = self.args.get("command") or self.args.get("file_path") or \
                json.dumps(self.args, indent=2, ensure_ascii=False, default=str)
            with VerticalScroll(id="approval-body"):
                yield Static(Text(str(body)))
            with Horizontal(id="approval-buttons"):
                yield Button("Allow  y", id="allow", variant="success")
                yield Button(f"Always allow {self.tool}  a", id="always", variant="primary")
                yield Button("Deny  n", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_decide(event.button.id or "deny")

    def action_decide(self, choice: str) -> None:
        self.dismiss(Decision(allow=choice in ("allow", "always"), always=choice == "always"))


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y,enter", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, question: str, detail: str = "") -> None:
        super().__init__()
        self.question = question
        self.detail = detail

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(Text(self.question, style="bold"), id="confirm-title")
            if self.detail:
                yield Static(Text(self.detail, style="dim"), id="confirm-detail")
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes  y", id="yes", variant="error")
                yield Button("No  n", id="no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_answer(event.button.id == "yes")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class NameScreen(ModalScreen[str | None]):
    """Ask for a short name (a new team's)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, question: str, detail: str, suggestion: str,
                 check: Any = None) -> None:
        super().__init__()
        self.question = question
        self.detail = detail
        self.suggestion = suggestion
        self.check = check  # name -> error message or None

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(Text(self.question, style="bold"), id="confirm-title")
            yield Static(Text(self.detail, style="dim"), id="confirm-detail")
            yield Input(value=self.suggestion, id="name-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        name = event.value.strip().lower()
        error = self.check(name) if self.check else None
        if error:
            self.notify(error, severity="warning")
            return
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TokenScreen(ModalScreen[str | None]):
    """Paste an account's token (hidden while typed)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.account = name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm", classes="account"):
            yield Static(Text(f"Token for the account {self.account!r}", style="bold"),
                         id="confirm-title")
            yield Static(Text("In another terminal run `claude setup-token`, sign in with "
                              "that Claude account, and paste the token it prints here. "
                              "It is kept in ~/.config/collaborators/accounts.json, readable "
                              "only by you.", style="dim"), id="confirm-detail")
            yield Input(password=True, placeholder="sk-ant-oat01-…", id="token-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        token = event.value.strip()
        if not token:
            self.notify("Paste the token first (Esc to cancel).", severity="warning")
            return
        self.dismiss(token)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AccountKindScreen(ModalScreen[str | None]):
    """How a new account signs in: a claude.ai login or a setup-token."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.account = name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm", classes="account"):
            yield Static(Text(f"How should {self.account!r} sign in?", style="bold"),
                         id="confirm-title")
            yield Static(Text("A claude.ai login gets its own Claude Code config dir; "
                              "conversations, settings and skills stay shared.",
                              style="dim"), id="confirm-detail")
            yield OptionList(
                Option("log in with claude.ai (supports Remote Control)", id=LOGIN_KIND),
                Option("paste a token from `claude setup-token` (no Remote Control)",
                       id=TOKEN_KIND),
                id="account-list")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AccountScreen(ModalScreen[str | None]):
    """The Claude accounts the agents can run under. Returns the one to switch to."""

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("a", "add", "Add account"),
        Binding("d,delete", "remove", "Remove"),
    ]

    def __init__(self, current: str, note: str = "", adding: bool = False) -> None:
        super().__init__()
        self.current = current
        self.note = note
        self.adding = adding  # open straight on "add an account" (`/switch add`)

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm", classes="account"):
            yield Static(Text("Claude account", style="bold"), id="confirm-title")
            yield Static(Text(self.note or "Every agent keeps its conversation; busy ones "
                              "switch when their turn ends.", style="dim"),
                         id="confirm-detail")
            yield OptionList(id="account-list")
            yield Static(Text("Enter switch · a add · d remove · Esc close", style="dim"),
                         id="account-keys")

    def on_mount(self) -> None:
        self._fill()
        self.query_one(OptionList).focus()
        if self.adding or accounts.names() == [accounts.LOGIN]:
            self.call_after_refresh(self.action_add)  # nothing to switch to yet

    def _fill(self, highlight: str | None = None) -> None:
        names = accounts.names()
        options = self.query_one(OptionList)
        options.clear_options()
        for name in names:
            line = Text()
            line.append("● " if name == self.current else "  ",
                        style="bold green" if name == self.current else "")
            line.append(name, style="bold")
            line.append(f"  {describe_account(name)}", style="dim")
            options.add_option(Option(line, id=name))
        target = highlight or self.current
        options.highlighted = names.index(target) if target in names else 0

    def _selected(self) -> str | None:
        options = self.query_one(OptionList)
        if options.highlighted is None:
            return None
        return options.get_option_at_index(options.highlighted).id

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_add(self) -> None:
        def check(name: str) -> str | None:
            try:
                name = check_team_name(name)
            except ConfigError as exc:
                return str(exc)
            if name == accounts.LOGIN:
                return f"{accounts.LOGIN!r} is Claude Code's own login; pick another name"
            if name == accounts.ADD:
                return f"{accounts.ADD!r} is reserved; pick another name"
            return None  # an existing name is re-added (e.g. a token turned into a login)

        def got_name(name: str | None) -> None:
            if name:
                self.app.push_screen(AccountKindScreen(name), lambda k: got_kind(name, k))

        def got_kind(name: str, how: str | None) -> None:
            if how == TOKEN_KIND:
                self.app.push_screen(TokenScreen(name), lambda tok: got_token(name, tok))
            elif how == LOGIN_KIND:
                self.app.login_account(name, added)  # type: ignore[attr-defined]

        def added(name: str) -> None:
            self._fill(highlight=name)
            self.notify(f"Account {name!r} added: press Enter to switch to it, or later "
                        f"type /switch {name}.")

        def got_token(name: str, token: str | None) -> None:
            if not token:
                return
            try:
                accounts.add(name, token)
            except ConfigError as exc:
                self.notify(str(exc), severity="error")
                return
            added(name)
            self.app.account_updated(name)  # type: ignore[attr-defined]

        self.app.push_screen(NameScreen("Name for the new account",
                                        "A short name, e.g. work or personal. Re-adding an "
                                        "existing one replaces how it signs in.", "", check),
                             got_name)

    def action_remove(self) -> None:
        name = self._selected()
        if not name or name == accounts.LOGIN:
            self.notify("Claude Code's own login cannot be removed.", severity="warning")
            return

        def answer(yes: bool | None) -> None:
            if not yes:
                return
            with contextlib.suppress(ConfigError):
                accounts.remove(name)
            if name == self.current:  # its token is gone: back to the login
                self.dismiss(accounts.LOGIN)
            else:
                self._fill()

        self.app.push_screen(ConfirmScreen(f"Remove the account {name!r}?",
                                           "Its token or claude.ai login is deleted from collab; "
                                           "conversations stay."), answer)


HELP = """\
## collaborators

You talk to the **Lead**. It breaks your request into tasks, dispatches them to
**workers**, has **testers** verify the result, and reports back. Every agent's
work streams live into its card.

| Key | Action |
|---|---|
| `Enter` | send your message to the Lead (or open the focused agent card) |
| `Ctrl+J` or `\\` `Enter` | new line in your message (also `Shift+Enter` in kitty, WezTerm, …) |
| `Tab` / `Shift+Tab` | move focus between the Lead and the agent cards |
| `Ctrl+O` | open the full transcript of the focused agent |
| `F2` / `F3` | add a worker / a tester — the Lead is told immediately |
| `Delete` | remove the focused worker or tester (its running task is cancelled) |
| `Ctrl+T` | task board: every task, its status, cost and report — `d` diff, `u` undo |
| `Ctrl+X` | interrupt the focused agent (the Lead when typing) |
| `Ctrl+P` | command palette (add or remove agents, themes, …) |
| `/` | slash commands and skills (yours and the project's): `↑`/`↓` choose, `Tab` completes |
| `/command ` | its values: `/switch` accounts, `/model` models… (`Enter` picks, sends) |
| `/switch` | switch Claude account (or `/switch <name>`); conversations carry on |
| `/switch add` | add an account: claude.ai login (Remote Control works) or a token |
| `/remote-control` | reach the Lead from claude.ai/code or the Claude app; not on token accounts |
| `↑` / `↓` | browse your previous messages (from the first / last line of the input) |
| `Ctrl+N` | open a new team in a new tab (its own Lead, workers and testers) |
| `Ctrl+G` | go to the next team's tab (or click the tab) |
| `F1` | this help |
| `Esc` | vim **normal mode** (see below); `i`, `a`, `o` or `Enter` to type again |
| `Ctrl+C` | clear the input; press twice to quit (also `Ctrl+Q`) — `collab --resume` picks up |

In an agent's full transcript you can message that agent directly; the Lead is
not told about those exchanges.

Teams in different tabs share the directory: a task that would touch files
another team is working on is refused, and the Leads can write to each other
(a few messages at a time, shown in both tabs). Close a tab from `Ctrl+P`.

### Vim mode

Press `Esc` in the Lead's input to get to **normal mode** (`-- NORMAL --` at the bottom):

| Keys | Action |
|---|---|
| `:` | command line (`↑`/`↓` history, `Esc` cancels) |
| `Ctrl+W s` / `Ctrl+W v` | add a worker / a tester |
| `Ctrl+W q` | close the selected agent (on the Lead: the tab) |
| `Ctrl+W w` / `W`, `h` `j` `k` `l` | move between the Lead and the cards |
| `j` / `k`, `Ctrl+D` / `Ctrl+U`, `gg` / `G` | scroll the selected log |
| `gt` / `gT` | next / previous tab |

| Command | Action |
|---|---|
| `:sp`, `:worker [n]` · `:vs`, `:tester [n]` | add workers · testers |
| `:q[!]` | close the selected agent; on the Lead the tab, or quit if it is the last |
| `:rm[!] worker2` | remove an agent |
| `:qa[!]` | quit collab |
| `:tabnew [name]`, `:tabc[!] [name]`, `:tabn`, `:tabp` | tabs (teams) |
| `:e worker1` | full transcript of an agent |
| `:stop [agent]`, `:tasks`, `:switch [account]`, `:remote [off]`, `:h` | as the keys above |

`!` skips the confirmation.
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,q", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield Static(Markdown(HELP))
