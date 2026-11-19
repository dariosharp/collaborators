"""Widgets: the live transcript, agent cards, the Lead panel and the bars."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import Static

from ..agents import Command
from ..config import is_default_model
from ..events import (
    AgentStatus,
    Event,
    LimitReached,
    Notice,
    TaskRecord,
    TaskStatus,
    TextDelta,
    TextDone,
    Thinking,
    ToolCall,
    ToolResult,
    TurnResult,
    UserPrompt,
)
from ..orchestrator import fmt_duration
from ..usage import CONTEXT_STYLE, AgentUsage, context_level, fmt_context, fmt_tokens
from .commands import CommandInput, CommandMenu
from .render import TEAM, describe_call, describe_result, detail

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
HOURGLASS = "⧗⧓⧖⧓"  # full, on its side, flipped over, on its side: an hourglass turning


# Statuses with a spinner: the agent is doing something.
BUSY_STATUSES = (AgentStatus.RUNNING, AgentStatus.STARTING, AgentStatus.COMPACTING)


def usage_text(usage: AgentUsage | None) -> Text:
    """` · ctx 137k · 2.4M tok` for a header: the context coloured by its size (dim,
    yellow from CTX_WARN_TOKENS, red from CTX_HIGH_TOKENS), then the session's tokens.
    Nothing until something is known."""
    t = Text()
    if usage is None:
        return t
    if usage.context_tokens is not None:
        t.append(" · ", style="dim")
        t.append(fmt_context(usage), style=CONTEXT_STYLE[context_level(usage.context_tokens)])
    if usage.totals.total:
        t.append(f" · {fmt_tokens(usage.totals.total)} tok", style="dim")
    return t


def short_model(model: str) -> str:
    if is_default_model(model):
        return "default model"  # Claude Code's own choice, until /model says otherwise
    return model.removeprefix("claude-")


# ---- transcript entries ------------------------------------------------------

class Entry(Static):
    pass


class TextEntry(Entry):
    def __init__(self, text: str = "", markdown: bool = True) -> None:
        super().__init__()
        self.buffer = text
        self.markdown = markdown
        self._shown = 0

    def on_mount(self) -> None:
        self._refresh_view()

    def append(self, chunk: str) -> None:
        self.buffer += chunk
        # Re-rendering markdown on every token is wasteful; batch small chunks.
        if len(self.buffer) - self._shown >= 24 or "\n" in chunk:
            self._refresh_view()

    def finish(self, text: str) -> None:
        self.buffer = text
        self._refresh_view()

    def _refresh_view(self) -> None:
        self._shown = len(self.buffer)
        self.update(Markdown(self.buffer) if self.markdown else Text(self.buffer))


class ToolEntry(Entry):
    def __init__(self, call: ToolCall, workdir: Path | None, expanded: bool) -> None:
        super().__init__()
        self.call = call
        self.workdir = workdir
        self.expanded = expanded
        self.result: ToolResult | None = None
        self.started = time.monotonic()
        self._frame = 0
        self._timer: Timer | None = None

    @property
    def is_wait(self) -> bool:
        return self.call.name == TEAM + "wait_tasks"

    def on_mount(self) -> None:
        if self.is_wait and self.result is None:
            self._timer = self.set_interval(0.3, self._tick)
        self._refresh_view()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(HOURGLASS)
        self._refresh_view()

    def set_result(self, result: ToolResult) -> None:
        self.result = result
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if result.is_error:
            self.add_class("error")
        self._refresh_view()

    def _refresh_view(self) -> None:
        line = describe_call(self.call.name, self.call.input, self.workdir)
        head = Text()
        icon = HOURGLASS[self._frame] if self._timer is not None else line.icon
        head.append(f"{icon} ", style="bold")
        if line.verb:
            head.append(line.verb + " ", style="bold")
        head.append(line.target)
        if self.result is None:
            waited = int(time.monotonic() - self.started)
            head.append(f"  {waited}s" if self.is_wait and waited else "  …", style="dim")
        else:
            summary = describe_result(self.call.name, self.call.input,
                                      self.result.content, self.result.is_error)
            if summary:
                head.append("\n  └ ", style="dim")
                head.append(summary, style="red" if self.result.is_error else "dim")
        body = None
        if self.expanded:
            body = detail(self.call.name, self.call.input,
                          self.result.content if self.result else None,
                          bool(self.result and self.result.is_error))
        self.update(Group(head, body) if body is not None else head)


class Transcript(VerticalScroll):
    """Live log of one agent: prompts, streamed text, thinking, tool calls."""

    MAX_ENTRIES = 400

    def __init__(self, *, expanded: bool, workdir: Path | None, show_prompts: bool = True,
                 markdown: bool | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.add_class("transcript")
        self.expanded = expanded
        self.markdown = expanded if markdown is None else markdown
        self.workdir = workdir
        self.show_prompts = show_prompts
        self._stream: TextEntry | None = None
        self._tools: dict[str, ToolEntry] = {}

    def _add(self, entry: Entry, *classes: str) -> Entry:
        at_end = self.scroll_y >= self.max_scroll_y - 2
        entry.add_class(*classes)
        self.mount(entry)
        excess = len(self.children) - self.MAX_ENTRIES
        if excess > 0:
            for old in list(self.children)[:excess]:
                old.remove()
        if at_end:
            self.call_after_refresh(self.scroll_end, animate=False)
        return entry

    def feed(self, event: Event) -> None:
        if isinstance(event, UserPrompt):
            self._stream = None
            if self.show_prompts:
                self._prompt(event)
        elif isinstance(event, TextDelta):
            if self._stream is None:
                self._stream = self._add(TextEntry(markdown=self.markdown), "text")  # type: ignore[assignment]
            self._stream.append(event.text)
            if self.scroll_y >= self.max_scroll_y - 2:
                self.call_after_refresh(self.scroll_end, animate=False)
        elif isinstance(event, TextDone):
            if self._stream is not None:
                self._stream.finish(event.text)
                self._stream = None
            elif event.text.strip():
                self._add(TextEntry(event.text, markdown=self.markdown), "text")
        elif isinstance(event, Thinking):
            self._stream = None
            text = event.text.strip()
            if not self.expanded and len(text) > 160:
                text = text[:159] + "…"
            self._add(Entry(Text("∴ " + text, style="italic")), "thinking")
        elif isinstance(event, ToolCall):
            self._stream = None
            self._tools[event.tool_id] = self._add(  # type: ignore[assignment]
                ToolEntry(event, self.workdir, self.expanded), "tool")
        elif isinstance(event, ToolResult):
            entry = self._tools.pop(event.tool_id, None)
            if entry is not None:
                entry.set_result(event)
        elif isinstance(event, TurnResult):
            self._stream = None
            self._result(event)
        elif isinstance(event, LimitReached):
            self._stream = None
            self._add(Entry(Text("▸ the Claude account hit its usage limit — /switch to "
                                 "another one")), "notice", "warning")
        elif isinstance(event, Notice):
            style = {"warning": "warning", "error": "error"}.get(event.level, "")
            self._add(Entry(Text("▸ " + event.text)), "notice", *([style] if style else []))

    def _prompt(self, event: UserPrompt) -> None:
        text = Text()
        if event.task_id:
            text.append(f" {event.task_id} ", style="bold reverse")
            text.append("  ")
        body = event.text.strip()
        if not self.expanded and len(body) > 220:
            body = body[:219] + "…"
        text.append(body)
        self._add(Entry(text), "prompt")

    def _result(self, event: TurnResult) -> None:
        if event.is_error:
            msg = Text("✗ usage limit" if event.limited else f"✗ {event.subtype}",
                       style="bold")
            if event.text and event.subtype in ("exception", "no_result"):
                msg.append(f"  {event.text}")
            self._add(Entry(msg), "result", "error")
            return
        bits = []
        if event.duration_ms:
            bits.append(fmt_duration(event.duration_ms / 1000))
        if event.num_turns:
            bits.append(f"{event.num_turns} turns")
        bits.append(f"${event.cost_usd:.2f}")
        self._add(Entry(Text("✓ done · " + " · ".join(bits))), "result")


# ---- agent card --------------------------------------------------------------

class AgentCard(Vertical, can_focus=True):
    """One worker or tester: a header with live status, and its recent activity."""

    class Open(Message):
        def __init__(self, agent: str) -> None:
            super().__init__()
            self.agent = agent

    class Remove(Message):
        def __init__(self, agent: str) -> None:
            super().__init__()
            self.agent = agent

    status: reactive[AgentStatus] = reactive(AgentStatus.STARTING)

    BINDINGS = [("enter", "open", "Open"), ("delete", "remove", "Remove")]

    def __init__(self, agent: str, model: str, effort: str, workdir: Path) -> None:
        super().__init__(id=f"card-{agent}", classes=agent.rstrip("0123456789"))
        self.agent = agent
        self.model = model
        self.effort = effort
        self.workdir = workdir
        self.current: TaskRecord | None = None
        self.last_task: TaskRecord | None = None
        self.cost = 0.0
        self.usage: AgentUsage | None = None
        self._frame = 0

    def compose(self) -> ComposeResult:
        yield Static(id="card-header", classes="card-header")
        with Transcript(expanded=False, workdir=self.workdir, classes="card-log"):
            yield Static("Waiting for a task from the Lead.  Enter: full transcript",
                         classes="hint")

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)
        self._render_header()

    def _tick(self) -> None:
        if self.status in BUSY_STATUSES:
            self._frame = (self._frame + 1) % len(SPINNER)
            self._render_header()

    def watch_status(self, status: AgentStatus) -> None:
        for s in AgentStatus:
            self.set_class(s is status, f"-{s.value}")
        self._render_header()

    def feed(self, event: Event) -> None:
        if isinstance(event, TurnResult):
            self.cost += event.cost_usd
        for hint in self.query(".hint"):
            hint.remove()
        self.query_one(Transcript).feed(event)
        self._render_header()

    def set_usage(self, usage: AgentUsage) -> None:
        self.usage = usage
        self._render_header()

    def set_task(self, task: TaskRecord) -> None:
        if task.status is TaskStatus.RUNNING:
            self.current = task
        else:
            self.current = None
            self.last_task = task
        self._render_header()

    def _render_header(self) -> None:
        if not self.is_mounted:
            return
        t = Text(no_wrap=True, overflow="ellipsis")
        icon = {
            AgentStatus.STARTING: SPINNER[self._frame],
            AgentStatus.RUNNING: SPINNER[self._frame],
            AgentStatus.COMPACTING: SPINNER[self._frame],
            AgentStatus.IDLE: "○",
            AgentStatus.WAITING: "◉",
            AgentStatus.ERROR: "✗",
        }[self.status]
        t.append(f" {icon} ", style="bold")
        t.append(self.agent, style="bold")
        t.append(f"  {short_model(self.model)} · {self.effort}", style="dim")
        t.append_text(usage_text(self.usage))
        right = Text()
        if self.current is not None:
            right.append(f"  {self.current.id} ", style="bold")
            right.append(fmt_duration(self.current.elapsed))
        elif self.status is AgentStatus.WAITING:
            right.append("  needs approval", style="bold")
        elif self.status is AgentStatus.COMPACTING:
            right.append("  compacting…", style="italic")
        elif self.status is AgentStatus.ERROR:
            right.append("  error", style="bold")
        elif self.last_task is not None:
            verdict = self.last_task.verdict
            if verdict:
                right.append("  ")
                right.append(f" {verdict} ", style="bold reverse")
            else:
                right.append(f"  {self.last_task.id} {self.last_task.status.value}", style="dim")
        if self.cost:
            right.append(f"  ${self.cost:.2f}", style="dim")
        t.append_text(right)
        self.query_one("#card-header", Static).update(t)

    def action_remove(self) -> None:
        self.post_message(self.Remove(self.agent))

    def action_open(self) -> None:
        self.post_message(self.Open(self.agent))

    def on_click(self, event: Any) -> None:
        if getattr(event, "chain", 1) >= 2:
            self.action_open()


# ---- the Lead ----------------------------------------------------------------

class LeadPanel(Vertical):
    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    status: reactive[AgentStatus] = reactive(AgentStatus.STARTING)

    def __init__(self, model: str, effort: str, workdir: Path, placeholder: str = "",
                 commands: Callable[[], list[Command]] = list) -> None:
        super().__init__(id="lead", classes="lead")
        self.commands = commands
        self.remote_url: str | None = None
        self.model = model
        self.effort = effort
        self.workdir = workdir
        self.placeholder = placeholder
        self.cost = 0.0
        self.usage: AgentUsage | None = None
        self._frame = 0

    def compose(self) -> ComposeResult:
        yield Static(id="lead-header", classes="card-header")
        yield Transcript(expanded=False, markdown=True, workdir=self.workdir,
                         show_prompts=False, id="lead-log")
        yield CommandMenu(id="lead-input-menu")
        yield CommandInput(self.commands, menu_id="lead-input-menu", escape_to_normal=True,
                           placeholder="Describe what you want built, or / for commands and "
                                       "skills…", id="lead-input", value=self.placeholder)

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)
        self._render_header()

    def _tick(self) -> None:
        if self.status in BUSY_STATUSES:
            self._frame = (self._frame + 1) % len(SPINNER)
            self._render_header()

    def watch_status(self, status: AgentStatus) -> None:
        for s in AgentStatus:
            self.set_class(s is status, f"-{s.value}")
        self._render_header()

    def _render_header(self) -> None:
        if not self.is_mounted:
            return
        busy = self.status in BUSY_STATUSES
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(f" {SPINNER[self._frame] if busy else '◆'} ", style="bold")
        t.append("Lead", style="bold")
        t.append(f"  {short_model(self.model)} · {self.effort}", style="dim")
        t.append_text(usage_text(self.usage))
        state = {AgentStatus.STARTING: "starting…", AgentStatus.RUNNING: "working…",
                 AgentStatus.COMPACTING: "compacting…",
                 AgentStatus.IDLE: "ready", AgentStatus.WAITING: "needs approval",
                 AgentStatus.ERROR: "error"}[self.status]
        t.append(f"  {state}", style="italic")
        if self.remote_url:
            t.append("  ● remote", style="bold green")
        if self.cost:
            t.append(f"  ${self.cost:.2f}", style="dim")
        self.query_one("#lead-header", Static).update(t)

    def set_usage(self, usage: AgentUsage) -> None:
        self.usage = usage
        self._render_header()

    def feed(self, event: Event) -> None:
        if isinstance(event, TurnResult):
            self.cost += event.cost_usd
            self._render_header()
            if not event.is_error:
                return  # the cost is in the header; no need for a line per reply
        if isinstance(event, UserPrompt) and event.text.startswith("[team]"):
            first = event.text.split("\n\n", 1)[-1].splitlines()[0]
            self.query_one(Transcript).feed(Notice("lead", "report: " + first))
            return
        self.query_one(Transcript).feed(event)

    def show_user(self, text: str) -> None:
        log = self.query_one(Transcript)
        msg = Text()
        msg.append("you › ", style="bold")
        msg.append(text)
        log._add(Entry(msg), "user")
        log.call_after_refresh(log.scroll_end, animate=False)

    def on_command_input_submitted(self, event: CommandInput.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.post_message(self.Submitted(text))


# ---- bars ----------------------------------------------------------------------

class TopBar(Static):
    def __init__(self, workdir: Path, summary: str, account: str = "") -> None:
        super().__init__(id="topbar")
        self.workdir = workdir
        self.summary = summary
        self.account = account  # shown when there is more than Claude Code's own login

    def set_account(self, account: str) -> None:
        self.account = account
        self.on_mount()

    def set_summary(self, summary: str) -> None:
        self.summary = summary
        self.on_mount()

    def on_mount(self) -> None:
        home = str(Path.home())
        where = str(self.workdir)
        where = "~" + where[len(home):] if where.startswith(home) else where
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(" ◆ collaborators ", style="bold reverse")
        t.append(f"  {where}", style="bold")
        if self.account:
            t.append(f"   ● {self.account}", style="bold green")
        t.append(f"   {self.summary}", style="dim")
        self.update(t)


class StatsBar(Static):
    def __init__(self) -> None:
        super().__init__(id="statsbar")
        self.started = time.time()
        self.cost = 0.0
        self.tokens = 0
        self.tasks: dict[str, TaskRecord] = {}

    def on_mount(self) -> None:
        self.set_interval(1, self.refresh_stats)
        self.refresh_stats()

    def add_result(self, result: TurnResult) -> None:
        self.cost += result.cost_usd
        self.tokens += result.input_tokens + result.output_tokens
        self.refresh_stats()

    def set_task(self, task: TaskRecord) -> None:
        self.tasks[task.id] = task
        self.refresh_stats()

    def refresh_stats(self) -> None:
        running = sum(t.status is TaskStatus.RUNNING for t in self.tasks.values())
        done = sum(t.status is TaskStatus.DONE for t in self.tasks.values())
        failed = sum(t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED)
                     for t in self.tasks.values())
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(f" ● {running} running", style="bold" if running else "dim")
        t.append(f"   ✓ {done} done")
        if failed:
            t.append(f"   ✗ {failed} failed", style="bold")
        tok = f"{self.tokens / 1000:.1f}k" if self.tokens < 1_000_000 else \
            f"{self.tokens / 1_000_000:.2f}M"
        t.append(f"   {tok} tokens   ${self.cost:.2f}   "
                 f"{fmt_duration(time.time() - self.started)} elapsed", style="dim")
        self.update(t)
