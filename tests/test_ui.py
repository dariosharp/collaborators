"""Drive the real Textual app with the scripted backend."""

import asyncio

from collaborators.backend_fake import fake_backend
from collaborators.cli import DEMO_PROMPT
from collaborators.config import load_config
from collaborators.ui.app import CollabApp
from collaborators.ui.screens import AgentDetailScreen, ConfirmScreen, TaskBoardScreen
from collaborators.ui.widgets import AgentCard, LeadPanel


async def wait_for(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met")


async def test_demo_round_trip_and_shortcuts(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = load_config(tmp_path / ".", {"workers": 1, "testers": 1})
    app = CollabApp(cfg, fake_backend(speed=20), initial_prompt=DEMO_PROMPT)
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(pilot, lambda: app.team and app.team._lead_loop is not None)
        await pilot.press("enter")
        await wait_for(pilot, lambda: len(app.tasks) == 2
                       and all(t.status.finished for t in app.tasks.values())
                       and app.query_one(LeadPanel).cost > 0)
        assert app.tasks["T2"].verdict == "PASS"

        await pilot.press("f2", "f3")
        await wait_for(pilot, lambda: {c.agent for c in app.query(AgentCard)}
                       == {"worker1", "worker2", "tester1", "tester2"})
        assert [c.agent for c in app.query(AgentCard)] == \
            ["worker1", "worker2", "tester1", "tester2"]
        await wait_for(pilot, lambda: any(
            "worker2" in p and "joined" in p
            for p in app.backend.sessions["lead"].prompts))

        await pilot.press("ctrl+t")
        assert isinstance(app.screen, TaskBoardScreen)
        await pilot.press("escape")
        app.open_detail("worker1")
        await pilot.pause(0.2)
        assert isinstance(app.screen, AgentDetailScreen)
        await pilot.press("escape")

        # Delete on a focused card asks first, then removes it and tells the Lead.
        app.query_one("#card-worker2", AgentCard).focus()
        await pilot.press("delete")
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        assert "worker2" in app.team.agent_names
        app.query_one("#card-worker2", AgentCard).focus()
        await pilot.press("delete")
        await pilot.press("y")
        await wait_for(pilot, lambda: [c.agent for c in app.query(AgentCard)]
                       == ["worker1", "tester1", "tester2"])
        await wait_for(pilot, lambda: any(
            "worker2 was removed" in p for p in app.backend.sessions["lead"].prompts))


async def test_slash_commands_and_ctrl_c(tmp_path, monkeypatch):
    from collaborators.ui.commands import CommandInput, CommandMenu

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = load_config(tmp_path / ".", {"workers": 1, "testers": 0})
    app = CollabApp(cfg, fake_backend(speed=20))
    async def scenario() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await wait_for(pilot, lambda: app.team and app.team._lead_loop is not None
                           and app.commands_for("lead"))
            inp = app.query_one("#lead-input", CommandInput)
            menu = app.query_one("#lead-input-menu", CommandMenu)
            await pilot.press("/", "c", "o")
            await pilot.pause(0.1)
            assert menu.display
            assert [c.name for c in menu.commands] == ["code-review", "compact", "context"]
            await pilot.press("down", "tab")
            assert inp.value == "/compact " and not menu.display
            await pilot.press("enter")
            await wait_for(pilot, lambda: "/compact" in app.backend.sessions["lead"].prompts)

            # Enter on a partial name completes it first; Esc closes the list
            await pilot.press("/", "s", "i", "m")
            await pilot.pause(0.1)
            await pilot.press("enter")
            assert inp.value == "/simplify "
            inp.value = "/zzz"
            await pilot.pause(0.1)
            assert not menu.display  # nothing matches
            inp.value = "/"
            await pilot.pause(0.1)
            assert menu.display and len(menu.commands) == 7
            await pilot.press("escape")
            assert not menu.display

            # unknown commands (and "/ model") are refused instead of being sent
            sent = len(app.backend.sessions["lead"].prompts)
            for text in ("/remote-control", "/ model"):
                inp.value = text
                await pilot.press("escape", "enter")
                await pilot.pause(0.1)
            assert len(app.backend.sessions["lead"].prompts) == sent

            # /model X switches the model, and the header follows
            inp.value = "/model claude-opus-4-8"
            await pilot.press("enter")
            lead = app.query_one(LeadPanel)
            await wait_for(pilot, lambda: lead.model == "claude-opus-4-8")
            assert app.team.runners["lead"].spec.model == "claude-opus-4-8"

            # Ctrl+C clears the input first, then needs a second press to quit
            inp.value = "half-typed"
            await pilot.press("ctrl+c")
            assert inp.value == "" and app.is_running
            await pilot.press("ctrl+c")
            assert app.is_running
            await pilot.press("ctrl+c")
            await wait_for(pilot, lambda: app.team is None)  # agents stopped
        # the with-block only ends once the app has really exited (it used to hang)
        assert not app.is_running

    # a hang on quit must fail the test, not stall the suite
    await asyncio.wait_for(scenario(), timeout=60)
