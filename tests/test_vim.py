"""Vim style keys on the main screen, driven through the real Textual app."""

from collaborators.backend_fake import fake_backend
from collaborators.config import load_config
from collaborators.ui.app import CollabApp
from collaborators.ui.commands import CommandInput
from collaborators.ui.screens import ConfirmScreen
from collaborators.ui.widgets import AgentCard


async def wait_for(pilot, cond, timeout=10.0, state=None):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met" + (f": {state()}" if state else ""))


def cards(app):
    return [c.agent for c in app.view.query(AgentCard)]


def mode(app) -> str:
    return str(app.query_one("#mode").render())


async def ex(pilot, command: str) -> None:
    await pilot.press(":", *command, "enter")
    await pilot.pause(0.05)


async def test_normal_mode_commands_and_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = load_config(tmp_path / ".", {"workers": 1, "testers": 1})
    app = CollabApp(cfg, fake_backend(speed=20))
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(pilot, lambda: app.team and app.team._lead_loop is not None)
        inp = app.query_one("#lead-input", CommandInput)
        inp.focus()
        await pilot.pause(0.2)
        assert "INSERT" in mode(app)

        # Esc leaves the input; i comes back; Esc again.
        await pilot.press("escape")
        assert app.focused.id == "lead-log"
        await wait_for(pilot, lambda: "NORMAL" in mode(app))
        await pilot.press("i")
        assert app.focused is inp
        await pilot.press("escape")

        # :split / Ctrl+W v / :worker 2 add agents.
        await ex(pilot, "sp")
        await wait_for(pilot, lambda: cards(app) == ["worker1", "worker2", "tester1"])
        await pilot.press("ctrl+w", "v")
        await wait_for(pilot, lambda: "tester2" in cards(app))
        await ex(pilot, "worker 2")
        await wait_for(pilot, lambda: {"worker3", "worker4"} <= set(cards(app)))
        assert app.focused.id == "lead-log"  # the command line gives the focus back

        # Ctrl+W j selects the first card; :q! closes it without asking.
        await pilot.press("ctrl+w", "j")
        assert isinstance(app.focused, AgentCard) and app.focused.agent == "worker1"
        await pilot.press("ctrl+w", "j")
        assert app.focused.agent == "worker2"
        await ex(pilot, "q!")
        await wait_for(pilot, lambda: "worker2" not in cards(app))
        # :rm (without !) asks first.
        await ex(pilot, "rm worker3")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await wait_for(pilot, lambda: "worker3" not in cards(app))
        await pilot.press("ctrl+w", "h")
        assert app.focused.id == "lead-log"

        # Unknown commands are refused, like vim.
        await ex(pilot, "frobnicate")
        assert any("E492" in str(n.message) for n in app._notifications)

        # Tabs: :tabnew api, gT / gt, :tabc! closes it.
        await ex(pilot, "tabnew api")
        await wait_for(pilot, lambda: app.view is not None and app.view.team_name == "api"
                       and app.view.team is not None,
                       state=lambda: ([v.team_name for v in app.views],
                                      [str(n.message) for n in app._notifications]))
        await pilot.pause(0.3)
        await pilot.press("escape")
        await pilot.press("g", "T")
        await wait_for(pilot, lambda: app.view.team_name == "main")
        await pilot.press("g", "t")
        await wait_for(pilot, lambda: app.view.team_name == "api")
        await pilot.pause(0.2)
        await pilot.press("escape")
        await ex(pilot, "tabc!")
        await wait_for(pilot, lambda: [v.team_name for v in app.views] == ["main"])

        # :qa! quits.
        await pilot.pause(0.2)
        await pilot.press("escape")
        await ex(pilot, "qa!")
        await wait_for(pilot, lambda: not app.is_running)


async def test_scrolling_and_history(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = load_config(tmp_path / ".", {"workers": 1, "testers": 0})
    app = CollabApp(cfg, fake_backend(speed=20))
    async with app.run_test(size=(140, 20)) as pilot:
        await wait_for(pilot, lambda: app.team and app.team._lead_loop is not None)
        log = app.query_one("#lead-log")
        from collaborators.events import Notice
        for i in range(60):
            app.view.query_one("LeadPanel").feed(Notice("lead", f"line {i}"))
        await pilot.pause(0.3)
        log.focus()
        await pilot.press("G")
        await pilot.pause(0.1)
        assert log.scroll_y == log.max_scroll_y > 0
        await pilot.press("g", "g")
        await pilot.pause(0.1)
        assert log.scroll_y == 0
        await pilot.press("j", "j")
        await pilot.pause(0.1)
        assert log.scroll_y == 2
        await pilot.press("ctrl+d")
        await pilot.pause(0.1)
        assert log.scroll_y > 2
        # The command line remembers what was typed.
        await ex(pilot, "sp")
        await pilot.press(":", "up")
        assert app.query_one("#cmdline").value == "sp"
        await pilot.press("escape")
        assert not app.mode_line.is_open and app.focused is log
