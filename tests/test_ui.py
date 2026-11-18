"""Drive the real Textual app with the scripted backend."""

import asyncio

from collaborators.backend_fake import fake_backend
from collaborators.cli import DEMO_PROMPT
from collaborators.config import load_config
from collaborators.ui.app import CollabApp
from collaborators.ui.screens import AgentDetailScreen, ConfirmScreen, TaskBoardScreen
from collaborators.ui.widgets import AgentCard, LeadPanel


async def wait_for(pilot, cond, timeout=10.0, state=None):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met" + (f": {state()}" if state else ""))


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
            assert [c.name for c in menu.commands] == ["code-review", "compact", "context",
                                                       "remote-control"]
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
            assert menu.display and len(menu.commands) == 8
            await pilot.press("escape")
            assert not menu.display

            # a click on an entry completes it too, and the input keeps the focus
            inp.value = "/co"
            await pilot.pause(0.1)
            await pilot.click(menu, offset=(3, 2))  # 2nd entry: compact
            await pilot.pause(0.1)
            assert inp.value == "/compact " and not menu.display and inp.has_focus
            inp.value = ""

            # unknown commands (and "/ model") are refused instead of being sent
            sent = len(app.backend.sessions["lead"].prompts)
            for text in ("/remote-controll", "/ model"):
                inp.value = text  # no menu for these: Enter sends (and is refused)
                await pilot.press("enter")
                await pilot.pause(0.1)
            assert len(app.backend.sessions["lead"].prompts) == sent

            # /remote-control is handled by collab itself, for the Lead
            assert app.commands_for("lead")[0].name == "remote-control"
            lead = app.query_one(LeadPanel)
            inp.value = "/remote-control"
            await pilot.press("escape", "enter")
            await wait_for(pilot, lambda: lead.remote_url is not None)
            inp.value = "/remote-control off"
            await pilot.press("enter")
            await wait_for(pilot, lambda: lead.remote_url is None)
            assert not any("remote-control" in p for p in app.backend.sessions["lead"].prompts)

            # /model X switches the model, and the header follows
            inp.value = "/model claude-opus-4-8"
            await pilot.press("enter")
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


async def test_wait_hourglass_turns_until_the_result():
    from textual.app import App

    from collaborators.events import ToolCall, ToolResult
    from collaborators.ui.widgets import HOURGLASS, ToolEntry

    call = ToolCall("lead", "t1", "mcp__team__wait_tasks", {"task_ids": ["T13"]})
    entry = ToolEntry(call, None, expanded=False)

    class Host(App):
        def compose(self):
            yield entry

    async with Host().run_test() as pilot:
        seen = set()
        for _ in range(12):
            seen.add(str(entry.render()).split()[0])
            await pilot.pause(0.1)
        assert len(seen) >= 3 and seen <= set(HOURGLASS)
        entry.set_result(ToolResult("lead", "t1", "T13 · worker1 · done", False))
        await pilot.pause(0.5)
        assert str(entry.render()).startswith("⧗ Wait T13")


async def test_teams_in_tabs(tmp_path, monkeypatch):
    import json

    from textual.widgets import TabbedContent

    from collaborators.ui.screens import NameScreen
    from collaborators.ui.team_view import TeamView

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = load_config(tmp_path / ".", {"workers": 1, "testers": 0})
    backend = fake_backend(speed=20)

    async def scenario() -> None:
        app = CollabApp(cfg, backend)
        # The pilot waits for animations after each key; the tab bar's can keep it
        # waiting (the real app never waits on them).
        app.animation_level = "none"
        async with app.run_test(size=(140, 40)) as pilot:
            await wait_for(pilot, lambda: app.team and app.team._lead_loop is not None)
            tabs = app.query_one(TabbedContent)
            assert tabs.has_class("-single")  # one team: no tab bar
            await pilot.press("ctrl+n")
            assert isinstance(app.screen, NameScreen)
            inp = app.screen.query_one("#name-input")
            assert inp.value == "frontend"
            inp.value = "main"
            await pilot.press("enter")
            assert isinstance(app.screen, NameScreen)  # taken: refused
            inp.value = "backend"
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.view is not None
                           and app.view.team_name == "backend"
                           and app.team and app.team._lead_loop is not None,
                           state=lambda: (tabs.active, app.screen, app.team,
                                          app.team and app.team._lead_loop,
                                          [v.team_name for v in app.views]))
            assert not tabs.has_class("-single")
            assert [v.team_name for v in app.views] == ["main", "backend"]
            assert app.focused is app.view.query_one("#lead-input")

            # each tab talks to its own Lead
            app.view.query_one("#lead-input").value = "/model claude-opus-4-8"
            await pilot.press("enter")
            await wait_for(pilot, lambda: "/model claude-opus-4-8"
                           in backend.sessions["backend/lead"].prompts)
            assert backend.sessions["lead"].prompts == []

            # a Lead's message shows up in the other tab
            main = app.view_of("main")
            app.hub.send(app.view.team, "main", "the API changed")
            await wait_for(pilot, lambda: any(
                "the API changed" in p for p in backend.sessions["lead"].prompts))
            assert any("✉ from team backend" in str(getattr(e, "text", ""))
                       for e in main.history["lead"])

            await pilot.press("ctrl+g")
            await wait_for(pilot, lambda: app.view is main, state=lambda: (
                tabs.active, app.screen, app.focused, [v.team_name for v in app.views],
                [w.name for w in app.workers]))
            saved = json.loads((tmp_path / ".collab" / "tabs.json").read_text())
            assert saved["teams"] == ["main", "backend"]

            app.action_close_team("backend")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await wait_for(pilot, lambda: [v.team_name for v in app.views] == ["main"])
            assert "backend" not in app.hub.teams
            saved = json.loads((tmp_path / ".collab" / "tabs.json").read_text())
            assert saved["teams"] == ["main"]
            await app.action_quit()

        # --resume reopens the tabs that were open, with their conversations
        app2 = CollabApp(cfg, backend, resume=True)
        app2.animation_level = "none"
        (tmp_path / ".collab" / "tabs.json").write_text('{"teams": ["main", "backend"]}')
        async with app2.run_test(size=(140, 40)) as pilot:
            await wait_for(pilot, lambda: len(app2.views) == 2 and all(
                v.team and v.team._lead_loop for v in app2.views))
            assert app2.view.team_name == "main"
            assert app2.view_of("backend").team.resumed == ["lead", "worker1"]
            assert isinstance(app2.view_of("backend"), TeamView)
            # (not pilot.press: it waits for the tab bar's animation, which quitting stops)
            await app2.action_quit()
        assert not app2.is_running and not app2.hub.teams

    await asyncio.wait_for(scenario(), timeout=60)


async def test_notification_diff_and_undo_from_the_task_board(tmp_path, monkeypatch):
    from rich.syntax import Syntax
    from test_orchestrator import git_repo, writing_brain
    from textual.widgets import Static

    from collaborators.ui import team_view

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(team_view, "NOTIFY_AFTER_S", 0)
    git_repo(tmp_path)
    cfg = load_config(tmp_path, {"workers": 1, "testers": 0})
    app = CollabApp(cfg, fake_backend(writing_brain({"A": [("app.py", "x = 1", "x = 10")]})))

    async def scenario() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await wait_for(pilot, lambda: app.team and app.team._lead_loop is not None)
            app.query_one("#lead-input").value = "hello"
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.sent_notifications)
            assert app.sent_notifications == ["collab: ok"]

            app.view.send_direct("worker1", "A")
            await wait_for(pilot, lambda: app.tasks and all(
                t.status.finished for t in app.tasks.values()))
            assert (tmp_path / "app.py").read_text().startswith("x = 10")
            await pilot.press("ctrl+t")
            await pilot.pause(0.2)
            body = app.screen.query_one("#board-report-body", Static)
            await pilot.press("d")
            await wait_for(pilot, lambda: isinstance(body.content, Syntax))
            assert "+x = 10" in body.content.code
            await pilot.press("u")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await wait_for(pilot, lambda: app.tasks["T1"].undone)
            assert (tmp_path / "app.py").read_text().startswith("x = 1\n")
            await pilot.press("escape")
            await pilot.press("ctrl+q")

    await asyncio.wait_for(scenario(), timeout=60)


async def test_multiline_input(tmp_path, monkeypatch):
    from collaborators.ui.commands import CommandInput

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = load_config(tmp_path / ".", {"workers": 1, "testers": 0})
    app = CollabApp(cfg, fake_backend(speed=20))
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(pilot, lambda: app.team and app.team._lead_loop is not None)
        inp = app.query_one("#lead-input", CommandInput)
        inp.focus()
        sent: list[str] = []
        monkeypatch.setattr(app.team, "tell_lead", lambda text, chat=True: sent.append(text))
        # Shift+Enter, Ctrl+J and `\` + Enter all start a new line; Enter sends.
        await pilot.press(*"one", "shift+enter", *"two", "ctrl+j", *"three\\", "enter", *"four")
        assert inp.value == "one\ntwo\nthree\nfour"
        await pilot.press("enter")
        await wait_for(pilot, lambda: sent == ["one\ntwo\nthree\nfour"])
        assert inp.value == ""
        # Up recalls it; on a later line Up moves the cursor; Down past the end
        # brings back what was being typed.
        await pilot.press(*"draft", "up")
        assert inp.value == "one\ntwo\nthree\nfour"
        await pilot.press("up")
        assert inp.value == "one\ntwo\nthree\nfour" and inp.cursor_location[0] == 2
        await pilot.press("down", "down")
        assert inp.value == "draft"
        await pilot.press("ctrl+c")
        assert inp.value == "" and app.is_running


async def test_switch_account_and_limit_prompt(tmp_path, monkeypatch):
    from collaborators import accounts
    from collaborators.backend_fake import LIMIT_MARK
    from collaborators.ui.commands import CommandInput
    from collaborators.ui.widgets import TopBar

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    accounts.add("work", "tok-work")
    cfg = load_config(tmp_path / ".", {"workers": 1, "testers": 0})
    app = CollabApp(cfg, fake_backend(speed=20))
    sessions = app.backend.sessions
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(pilot, lambda: app.team and app.team._lead_loop is not None)
        assert app.account == "login" and app.query_one(TopBar).account == "login"
        inp = app.query_one("#lead-input", CommandInput)
        inp.focus()

        # The Lead hits the usage limit: collab offers the other account; y switches.
        inp.value = f"hello {LIMIT_MARK}"
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("y")
        await wait_for(pilot, lambda: app.team.account == "work"
                       and sessions["lead"].spec.env == {"CLAUDE_CODE_OAUTH_TOKEN": "tok-work"})
        assert sessions["lead"].spec.resume and app.query_one(TopBar).account == "work"
        assert accounts.current() == "work"
        # …and the Lead is told to carry on, under the new account.
        await wait_for(pilot, lambda: any("carry on" in p for p in sessions["lead"].prompts))

        # /switch <name> goes straight back.
        inp.focus()
        inp.value = "/switch login"
        await pilot.press("enter")
        await wait_for(pilot, lambda: app.team.account == "login"
                       and sessions["lead"].spec.env == {})
        assert sessions["worker1"].spec.env == {}

        # /switch alone opens the account picker.
        inp.focus()
        inp.value = "/switch"
        await pilot.press("escape", "enter")
        await wait_for(pilot, lambda: type(app.screen).__name__ == "AccountScreen")
        await pilot.press("escape")

        # /switch add goes straight to naming a new account; then its token.
        inp.focus()
        inp.value = "/switch add"
        await pilot.press("enter")
        await wait_for(pilot, lambda: type(app.screen).__name__ == "NameScreen")
        await pilot.press(*"personal", "enter")
        # A claude.ai login (first) or a setup-token: pick the token.
        await wait_for(pilot, lambda: type(app.screen).__name__ == "AccountKindScreen")
        await pilot.press("down", "enter")
        await wait_for(pilot, lambda: type(app.screen).__name__ == "TokenScreen")
        await pilot.press(*"tok-2", "enter")
        await wait_for(pilot, lambda: "personal" in accounts.names())
        assert accounts.env_for("personal") == {"CLAUDE_CODE_OAUTH_TOKEN": "tok-2"}


def test_hint_options():
    from collaborators.ui.commands import hint_options

    assert [v for v, _ in hint_options("[low|medium|high|xhigh|max] [--fix] [<pr#>]")] == \
        ["low", "medium", "high", "xhigh", "max"]
    assert [v for v, _ in hint_options("<low|max|ultracode [on|off]>")] == ["low", "max",
                                                                          "ultracode"]
    assert [v for v, _ in hint_options("[auto|<tokens>]")] == ["auto"]
    assert hint_options("[model]") == [] and hint_options("<instructions>") == []
    assert hint_options("") == []


async def test_argument_completion(tmp_path, monkeypatch):
    from collaborators import accounts
    from collaborators.ui.commands import CommandInput, CommandMenu

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    accounts.add("work", "tok")
    cfg = load_config(tmp_path / ".", {"workers": 1, "testers": 0})
    app = CollabApp(cfg, fake_backend(speed=20))
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(pilot, lambda: app.team and app.team._lead_loop is not None
                       and app.commands_for("lead"))
        inp = app.query_one("#lead-input", CommandInput)
        menu = app.query_one("#lead-input-menu", CommandMenu)
        inp.focus()

        def shown():
            return [v for v, _ in menu.values] if menu.display and menu.arg_of else []

        # Completing a command that takes values lists them at once.
        inp.value = "/mod"
        await pilot.press("tab")
        assert inp.value == "/model "
        assert shown() == ["default", "opus", "fable", "sonnet", "haiku"]
        await pilot.press(*"so")
        assert shown() == ["sonnet"]
        await pilot.press("enter")  # picks it and sends
        await wait_for(pilot, lambda: "/model sonnet" in app.backend.sessions["lead"].prompts)
        assert inp.value == ""

        # /switch lists the accounts (the one in use last); values from a hint too.
        inp.value = "/switch "
        assert shown() == ["work", "login", "add"]
        inp.value = "/effort h"
        assert shown() == ["high", "xhigh"]
        await pilot.press("down", "tab")
        assert inp.value == "/effort xhigh" and not menu.display
        # Free text after the value, or a command without values: no menu.
        inp.value = "/compact keep the API notes"
        assert not menu.display
        inp.value = "/switch work"
        assert not menu.display  # typed in full


async def test_switch_add_with_claude_ai_login(tmp_path, monkeypatch):
    """`/switch add` → a name → claude.ai login. Headless, collab cannot step aside for
    `claude auth login`, so it shows the command; y then checks it and adds the account."""
    from collaborators import accounts
    from collaborators.ui.commands import CommandInput

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    accounts.add("personal", "tok")  # a token account, turned into a login one
    monkeypatch.setattr(accounts, "login_status", lambda path: {
        "loggedIn": True, "authMethod": "claude.ai", "email": "p@example.com"})
    cfg = load_config(tmp_path / ".", {"workers": 1, "testers": 0})
    app = CollabApp(cfg, fake_backend(speed=20))
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(pilot, lambda: app.team and app.team._lead_loop is not None)
        inp = app.query_one("#lead-input", CommandInput)
        inp.focus()
        inp.value = "/switch add"
        await pilot.press("enter")
        await wait_for(pilot, lambda: type(app.screen).__name__ == "NameScreen")
        await pilot.press(*"personal", "enter")
        await wait_for(pilot, lambda: type(app.screen).__name__ == "AccountKindScreen")
        await pilot.press("enter")  # log in with claude.ai
        await wait_for(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        path = accounts.login_dir("personal")
        assert accounts.login_hint(path) in app.screen.detail
        assert (path / "projects").is_symlink()
        (path / ".credentials.json").write_text("{}")  # as `claude auth login` would
        await pilot.press("y")
        await wait_for(pilot, lambda: accounts.kind("personal") == "claude.ai")
        assert accounts.supports_remote_control("personal")
        await pilot.press("escape")
        inp.focus()
        inp.value = "/switch personal"
        await pilot.press("enter")
        await wait_for(pilot, lambda: app.backend.sessions["lead"].spec.env
                       == {"CLAUDE_CONFIG_DIR": str(path)})
        assert app.team.account == "personal"
