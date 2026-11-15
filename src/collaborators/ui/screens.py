"""Secondary screens: agent detail, task board, permission prompt, help."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Static

from ..agents import Decision
from ..events import Event, TaskRecord, TaskStatus
from ..orchestrator import fmt_duration
from .commands import CommandInput, CommandMenu
from .widgets import Transcript, short_model


class AgentDetailScreen(Screen[None]):
    """Full transcript of one agent, with diffs and command output, and a direct line."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+x", "interrupt", "Interrupt"),
    ]

    def __init__(self, agent: str, model: str, effort: str, workdir: Path,
                 history: list[Event]) -> None:
        super().__init__(classes=agent.rstrip("0123456789"))
        self.agent = agent
        self.model = model
        self.effort = effort
        self.workdir = workdir
        self.history = history

    def compose(self) -> ComposeResult:
        head = Text(no_wrap=True)
        head.append(f" ◆ {self.agent} ", style="bold reverse")
        head.append(f"  {short_model(self.model)} · {self.effort}", style="dim")
        head.append("   full transcript", style="italic dim")
        yield Static(head, id="detail-header")
        yield Transcript(expanded=True, workdir=self.workdir, id="detail-log")
        if self.agent != "lead":
            app: Any = self.app
            yield CommandMenu(id="detail-input-menu")
            yield CommandInput(lambda: app.commands_for(self.agent), menu_id="detail-input-menu",
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if text:
            event.input.value = ""
            self.app.send_direct(self.agent, text)  # type: ignore[attr-defined]

    def action_interrupt(self) -> None:
        self.app.interrupt_agent(self.agent)  # type: ignore[attr-defined]


class TaskBoardScreen(Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, tasks: dict[str, TaskRecord]) -> None:
        super().__init__()
        self.tasks = tasks

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
            status = task.verdict or task.status.value
            style = {"PASS": "bold green", "FAIL": "bold red", "running": "bold",
                     "failed": "red", "cancelled": "yellow"}.get(status, "")
            table.add_row(task.id, task.agent, Text(status, style=style),
                          fmt_duration(task.elapsed), f"${task.cost_usd:.2f}",
                          ", ".join(task.touched_files) or "—", key=task.id)
        if table.row_count:
            table.move_cursor(row=min(keep, table.row_count - 1))
        else:
            self.query_one("#board-report-body", Static).update(
                Text("No tasks yet. Ask the Lead for something.", style="dim italic"))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        task = self.tasks.get(str(event.row_key.value))
        if task is None:
            return
        body = f"### {task.id} · {task.agent} · {task.status.value}\n\n"
        body += f"**Task**\n\n{task.prompt}\n\n"
        if task.warnings:
            body += "**Warnings**\n\n" + "\n".join(f"- {w}" for w in task.warnings) + "\n\n"
        if task.status is not TaskStatus.RUNNING:
            body += f"**Report**\n\n{task.result or '_(empty)_'}"
        self.query_one("#board-report-body", Static).update(Markdown(body))


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


HELP = """\
## collaborators

You talk to the **Lead**. It breaks your request into tasks, dispatches them to
**workers**, has **testers** verify the result, and reports back. Every agent's
work streams live into its card.

| Key | Action |
|---|---|
| `Enter` | send your message to the Lead (or open the focused agent card) |
| `Tab` / `Shift+Tab` | move focus between the Lead and the agent cards |
| `Ctrl+O` | open the full transcript of the focused agent |
| `F2` / `F3` | add a worker / a tester — the Lead is told immediately |
| `Delete` | remove the focused worker or tester (its running task is cancelled) |
| `Ctrl+T` | task board: every task, its status, cost and report |
| `Ctrl+X` | interrupt the focused agent (the Lead when typing) |
| `Ctrl+P` | command palette (add or remove agents, themes, …) |
| `/` | slash commands and skills (yours and the project's): `↑`/`↓` choose, `Tab` completes |
| `/remote-control` | continue with the Lead from claude.ai/code or the Claude app (`off` to stop) |
| `↑` / `↓` | browse your previous messages in the Lead input |
| `F1` | this help |
| `Ctrl+C` | clear the input; press twice to quit (also `Ctrl+Q`) — `collab --resume` picks up |

In an agent's full transcript you can message that agent directly; the Lead is
not told about those exchanges.
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,q", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield Static(Markdown(HELP))
