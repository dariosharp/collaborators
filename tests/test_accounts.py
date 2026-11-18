import json
import os
import stat

import pytest

from collaborators import accounts
from collaborators.config import ConfigError


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Never the real ~/.claude: a home of our own.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / "home").mkdir()


def test_login_is_implicit_and_default():
    assert accounts.names() == ["login"]
    assert accounts.current() == "login"
    assert accounts.env_for("login") == {}


def test_add_switch_remove():
    accounts.add("Work", " tok-123 ")
    assert accounts.names() == ["login", "work"]
    assert accounts.env_for("work") == {"CLAUDE_CODE_OAUTH_TOKEN": "tok-123"}
    assert stat.S_IMODE(accounts.accounts_path().stat().st_mode) == 0o600
    accounts.set_current("work")
    assert accounts.current() == "work"
    accounts.remove("work")
    assert accounts.current() == "login"


def test_invalid():
    with pytest.raises(ConfigError):
        accounts.add("login", "x")
    with pytest.raises(ConfigError):
        accounts.add("ok", "  ")
    with pytest.raises(ConfigError):
        accounts.set_current("nope")


@pytest.fixture
def claude_home(tmp_path):
    """A fake ~/.claude and ~/.claude.json."""
    home = tmp_path / "home" / ".claude"
    for d in ("sessions", "skills", "plugins", "file-history"):
        (home / d).mkdir(parents=True)
    (home / "skills" / "mine.md").write_text("a skill")
    (home / "settings.json").write_text("{}")
    (home / "history.jsonl").write_text("")
    (home / ".credentials.json").write_text('{"secret": 1}')
    (home / "policy-limits.json").write_text("{}")
    (home / "remote-settings.json").write_text("{}")
    (tmp_path / "home" / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "me@example.com"}, "userID": "u1",
        "modelAccessCache": {"x": 1}, "theme": "dark", "hasCompletedOnboarding": True,
        "mcpServers": {"one": {"command": "a"}}, "projects": {"/p": {"trusted": True}},
    }))
    return home


def logged_in(name):
    """Prepare login_dir(name) as `claude auth login` would leave it."""
    path = accounts.prepare_login_dir(accounts.login_dir(name))
    (path / ".credentials.json").write_text('{"claudeAiOauth": {}}')
    return path


def test_login_dir_links_what_is_shared_and_keeps_the_login_private(claude_home):
    path = accounts.prepare_login_dir(accounts.login_dir("personal"))
    assert path == accounts.login_dir("personal")
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for item in ("projects", "sessions", "skills", "plugins", "file-history",
                 "settings.json", "history.jsonl"):
        assert (path / item).is_symlink(), item
        assert os.readlink(path / item) == str(claude_home / item)
    assert (claude_home / "projects").is_dir()  # created for conversations to be shared
    assert (path / "skills" / "mine.md").read_text() == "a skill"
    for item in ("todos", "agents", "CLAUDE.md"):  # not in ~/.claude: not linked
        assert not os.path.lexists(path / item)
    for item in (".credentials.json", "policy-limits.json", "remote-settings.json"):
        assert not os.path.lexists(path / item), item
    assert not (path / ".claude.json").is_symlink()
    # Preparing again is harmless and fixes a stale link.
    (path / "sessions").unlink()
    (path / "sessions").symlink_to(claude_home / "gone")
    accounts.prepare_login_dir(path)
    assert os.readlink(path / "sessions") == str(claude_home / "sessions")


def test_claude_json_copy_without_the_account_and_mcp_refresh(claude_home, tmp_path):
    path = accounts.prepare_login_dir(accounts.login_dir("personal"))
    own = json.loads((path / ".claude.json").read_text())
    assert "oauthAccount" not in own and "userID" not in own
    assert "modelAccessCache" not in own
    assert own["theme"] == "dark" and own["hasCompletedOnboarding"] is True
    assert stat.S_IMODE((path / ".claude.json").stat().st_mode) == 0o600
    # Claude Code logs the account in and writes its own keys…
    own["oauthAccount"] = {"emailAddress": "personal@example.com"}
    own["theme"] = "light"
    (path / ".claude.json").write_text(json.dumps(own))
    # …then the user adds an MCP server and trusts a project under `login`.
    main_path = tmp_path / "home" / ".claude.json"
    main = json.loads(main_path.read_text())
    main["mcpServers"]["two"] = {"command": "b"}
    main["projects"]["/q"] = {"trusted": True}
    main_path.write_text(json.dumps(main))
    (path / ".credentials.json").write_text("{}")
    accounts.add_login("personal")
    accounts.env_for("personal")  # what every switch to it does
    own = json.loads((path / ".claude.json").read_text())
    assert set(own["mcpServers"]) == {"one", "two"} and "/q" in own["projects"]
    assert own["oauthAccount"] == {"emailAddress": "personal@example.com"}
    assert own["theme"] == "light"


def test_env_for_both_kinds_and_remote_control(claude_home):
    accounts.add("work", "tok")
    path = logged_in("personal")
    accounts.add_login("personal")
    assert accounts.env_for("work") == {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    assert accounts.env_for("personal") == {"CLAUDE_CONFIG_DIR": str(path)}
    assert accounts.env_for("login") == {}
    assert accounts.kind("personal") == "claude.ai" and accounts.kind("work") == "token"
    assert accounts.supports_remote_control("login")
    assert accounts.supports_remote_control("personal")
    assert not accounts.supports_remote_control("work")
    assert not accounts.supports_remote_control("nope")


def test_add_login_needs_a_login_and_converts_a_token_account(claude_home):
    accounts.add("personal", "tok")
    with pytest.raises(ConfigError, match="not logged in"):
        accounts.add_login("personal")
    assert accounts.kind("personal") == "token"
    assert not accounts.is_logged_in(accounts.login_dir("personal"))
    logged_in("personal")
    assert accounts.is_logged_in(accounts.login_dir("personal"))
    accounts.add_login("personal")
    assert accounts.kind("personal") == "claude.ai"
    assert accounts.names() == ["login", "personal"]


def test_check_login_uses_claude_auth_status(claude_home, monkeypatch):
    path = logged_in("personal")
    seen = []
    monkeypatch.setattr(accounts, "login_status",
                        lambda p: seen.append(p) or {"loggedIn": False, "authMethod": "none"})
    with pytest.raises(ConfigError, match="no claude.ai login"):
        accounts.check_login("personal")
    monkeypatch.setattr(accounts, "login_status", lambda p: {
        "loggedIn": True, "authMethod": "claude.ai", "email": "p@example.com"})
    assert accounts.check_login("personal") == "p@example.com"
    assert seen == [path] and accounts.kind("personal") == "claude.ai"


def test_claude_env_points_at_the_dir_without_other_logins(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    env = accounts.claude_env(tmp_path)
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env


def test_remove_deletes_only_the_accounts_own_files(claude_home):
    path = logged_in("personal")
    accounts.add_login("personal")
    accounts.set_current("personal")
    (path / "todos").mkdir()  # a dir of its own, made by Claude Code
    (path / "todos" / "t.json").write_text("[]")
    accounts.remove("personal")
    assert not path.exists()
    assert accounts.current() == "login" and accounts.names() == ["login"]
    # Everything the links pointed to is still there.
    assert (claude_home / "skills" / "mine.md").read_text() == "a skill"
    assert (claude_home / "sessions").is_dir() and (claude_home / "projects").is_dir()
    assert (claude_home / "settings.json").exists()
    assert (claude_home / "history.jsonl").exists()
    assert (claude_home / ".credentials.json").exists()


def test_a_token_replacing_a_login_account_drops_its_dir(claude_home):
    path = logged_in("personal")
    accounts.add_login("personal")
    accounts.add("personal", "tok")
    assert accounts.kind("personal") == "token" and not path.exists()
    assert (claude_home / "skills" / "mine.md").exists()


def test_collab_account_login_cli(claude_home, monkeypatch, capsys):
    from collaborators import cli
    runs = []

    def fake_run(argv, env, check):
        runs.append((argv[1:], env["CLAUDE_CONFIG_DIR"]))
        (accounts.login_dir("personal") / ".credentials.json").write_text("{}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(accounts, "login_status",
                        lambda p: {"loggedIn": True, "authMethod": "claude.ai"})
    assert cli.main(["account", "login", "Personal"]) == 0
    assert runs == [(["auth", "login", "--claudeai"], str(accounts.login_dir("personal")))]
    assert accounts.kind("personal") == "claude.ai"
    assert cli.main(["account", "list"]) == 0
    assert "personal" in capsys.readouterr().out
    assert cli.main(["account", "login", "login"]) == 2
