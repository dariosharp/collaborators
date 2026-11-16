"""An input with Claude Code style `/` completion for slash commands and skills."""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import events
from textual.widgets import Input, OptionList
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


class CommandInput(Input):
    """Typing `/` lists the agent's slash commands and skills. Up/Down choose,
    Tab or Enter (or a click) completes, Esc closes the list; Enter again sends."""

    def __init__(self, commands: Callable[[], list[Command]], *, menu_id: str,
                 **kwargs) -> None:
        super().__init__(**kwargs)
        self._commands = commands
        self.menu_id = menu_id

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
        if value.startswith("/") and " " not in value:
            self.menu.show(matching(self._commands(), value[1:]))
        else:
            self.menu.hide()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is self:
            self._update_menu()

    def on_blur(self) -> None:
        self.menu.hide()

    def _complete(self) -> bool:
        cmd = self.menu.current
        if cmd is None:
            return False
        self.value = f"/{cmd.name} "
        self.cursor_position = len(self.value)
        self.menu.hide()
        return True

    def pick(self, index: int) -> None:
        """Complete with the command at `index` in the list (clicked with the mouse)."""
        if self.menu_open and 0 <= index < len(self.menu.commands):
            self.menu.highlighted = index
            self._complete()
        self.focus()

    async def _on_key(self, event: events.Key) -> None:
        if self.menu_open:
            menu = self.menu
            handled = True
            if event.key == "down":
                menu.action_cursor_down()
            elif event.key == "up":
                menu.action_cursor_up()
            elif event.key == "tab":
                self._complete()
            elif event.key == "escape":
                menu.hide()
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
        await super().action_submit()
