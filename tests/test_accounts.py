import stat

import pytest

from collaborators import accounts
from collaborators.config import ConfigError


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


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
