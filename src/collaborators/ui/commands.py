"""A multi-line prompt box with Claude Code style `/` completion for slash commands
and skills."""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import OptionList, TextArea
from textual.widgets.option_list import Option

from ..agents import Command

MAX_SHOWN = 8


def matching(commands: list[Command], typed: str) -> list[Command]:
    """Commands for what follows the `/`: prefix matches first, then substring ones."""
    typed = typed.lower()
    first = [c for c in commands if c.name.lower().startswith(typed)]
    rest = [c for c in commands if typed in c.name.lower() and c not in first]
    return first + rest


class CommandMenu(OptionList):
    """The popup list shown above a CommandInput while a `/command` is being typed."""

    can_focus = False

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id, classes="command-menu")
        self.commands: list[Command] = []

    def show(self, commands: list[Command]) -> None:
        self.commands = commands[:MAX_SHOWN]
        self.clear_options()
        for c in self.commands:
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append(f"/{c.name}", style="bold")
            if c.hint:
                line.append(f" {c.hint}", style="dim")
            if c.description:
                line.append("  " + c.description.split(". ")[0].strip(), style="dim italic")
            self.add_option(Option(line))
        self.highlighted = 0 if self.commands else None
        self.display = bool(self.commands)

    def hide(self) -> None:
        self.display = False

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Only a mouse click gets here: while typing, the keys go to the input.
        event.stop()
        for inp in self.parent.query(CommandInput) if self.parent else []:
            if inp.menu_id == self.id:
                inp.pick(event.option_index)

    @property
    def current(self) -> Command | None:
        if not self.display or self.highlighted is None or not self.commands:
            return None
        return self.commands[self.highlighted]


class CommandInput(TextArea):
    """The prompt box: it grows with what you type, up to a few lines.

    Enter sends. Shift+Enter, Ctrl+J, or a `\\` right before Enter start a new line
    instead (Shift+Enter only where the terminal reports it: kitty, WezTerm,
    Ghostty, foot, iTerm2; elsewhere it arrives as a plain Enter).

    Typing `/` lists the agent's slash commands and skills. Up/Down choose,
    Tab or Enter (or a click) completes, Esc closes the list; Enter again sends.
    Up on the first line and Down on the last one walk through what you sent.
    """

    # Ctrl+X interrupts and Ctrl+C clears/quits in the app: TextArea's cut/copy
    # bindings on those keys would swallow them.
    BINDINGS = [b for b in TextArea.BINDINGS
                if not {"ctrl+x", "ctrl+c"} & set(b.key.split(","))]
    inherit_bindings = False

    NEWLINE_KEYS = ("shift+enter", "ctrl+j", "shift+\r")

    class Submitted(Message):
        def __init__(self, input: CommandInput, value: str) -> None:
            super().__init__()
            self.input = input
            self.value = value

        @property
        def control(self) -> CommandInput:
            return self.input

    def __init__(self, commands: Callable[[], list[Command]], *, menu_id: str,
                 value: str = "", placeholder: str = "", escape_to_normal: bool = False,
                 **kwargs) -> None:
        super().__init__(value, placeholder=placeholder, soft_wrap=True,
                         highlight_cursor_line=False, classes="command-input", **kwargs)
        self._commands = commands
        self.menu_id = menu_id
        # Vim style: Esc (with no menu open) leaves the input for the log next to it.
        self.escape_to_normal = escape_to_normal
        self.sent: list[str] = []  # messages sent from here, for Up/Down
        self._hpos = 0
        self._draft = ""

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.text = value
        self.move_cursor(self.document.end)
        self._update_menu()

    @property
    def menu(self) -> CommandMenu:
        # The menu is a sibling: several tabs may each have one with the same id.
        assert self.parent is not None
        return self.parent.query_one(f"#{self.menu_id}", CommandMenu)

    @property
    def menu_open(self) -> bool:
        return self.menu.display

    def _update_menu(self) -> None:
        value = self.value
        if value.startswith("/") and not any(c.isspace() for c in value):
            self.menu.show(matching(self._commands(), value[1:]))
        else:
            self.menu.hide()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is self:
            self._update_menu()

    def on_blur(self) -> None:
        self.menu.hide()

    def _complete(self) -> bool:
        cmd = self.menu.current
        if cmd is None:
            return False
        self.value = f"/{cmd.name} "
        self.menu.hide()
        return True

    def pick(self, index: int) -> None:
        """Complete with the command at `index` in the list (clicked with the mouse)."""
        if self.menu_open and 0 <= index < len(self.menu.commands):
            self.menu.highlighted = index
            self._complete()
        self.focus()

    def _newline(self) -> None:
        start, end = self.selection
        self.replace("\n", start, end)

    def _browse_history(self, step: int) -> bool:
        """Up/Down through what was sent. Returns False when there is nowhere to go."""
        pos = self._hpos + step
        if not self.sent or pos < 0 or pos > len(self.sent) or pos == self._hpos:
            return False
        if self._hpos == len(self.sent):
            self._draft = self.value  # keep what was being typed
        self._hpos = pos
        self.value = self.sent[pos] if pos < len(self.sent) else self._draft
        return True

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        handled = True
        row, col = self.cursor_location
        if self.menu_open and key in ("down", "up", "tab", "escape"):
            menu = self.menu
            if key == "down":
                menu.action_cursor_down()
            elif key == "up":
                menu.action_cursor_up()
            elif key == "tab":
                self._complete()
            else:
                menu.hide()
        elif key == "escape" and self.escape_to_normal and self.parent is not None:
            log = self.parent.query(".transcript")
            if log:
                log.first().focus()
            else:
                self.screen.set_focus(None)
        elif key in self.NEWLINE_KEYS:
            self._newline()
        elif key == "enter":
            line = self.document.get_line(row)
            if (not self.menu_open and self.selection.is_empty and col > 0
                    and line[col - 1] == "\\"):
                # `\` + Enter: a new line, in any terminal (as in Claude Code).
                self.replace("\n", (row, col - 1), (row, col))
            else:
                await self.action_submit()
        elif key == "up" and row == 0:
            handled = self._browse_history(-1)
        elif key == "down" and row == self.document.line_count - 1:
            handled = self._browse_history(+1)
        else:
            handled = False
        if handled:
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    async def action_submit(self) -> None:
        # Enter on an unfinished `/comm` picks the highlighted command first.
        cmd = self.menu.current if self.menu_open else None
        if cmd is not None and self.value.strip() != f"/{cmd.name}":
            self._complete()
            return
        self.menu.hide()
        text = self.value
        if text.strip():
            self.sent.append(text.strip())
        self._hpos = len(self.sent)
        self._draft = ""
        self.post_message(self.Submitted(self, text))
