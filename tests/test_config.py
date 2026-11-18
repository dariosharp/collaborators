import pytest

from collaborators.config import ConfigError, RoleConfig, load_config
from collaborators.events import TaskRecord


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def test_defaults(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.agent_names == ["lead", "worker1", "tester1"]
    assert cfg.role_config("lead") == RoleConfig("claude-fable-5-2", "medium")
    assert cfg.role_config("worker1") == RoleConfig("claude-opus-5-5", "medium")
    assert cfg.role_config("tester1") == RoleConfig("claude-sonnet-5-5", "medium")
    assert cfg.permission_mode == "bypassPermissions"


def test_layering(tmp_path):
    user = tmp_path / "xdg" / "collaborators"
    user.mkdir(parents=True)
    (user / "config.toml").write_text('workers = 3\n[tester]\neffort = "max"\n')
    proj = tmp_path / "proj"
    (proj / ".collab").mkdir(parents=True)
    (proj / ".collab" / "config.toml").write_text('workers = 2\n[worker]\nmodel = "m"\n')
    cfg = load_config(proj, {"testers": 0, "worker": {"effort": "low", "model": None}})
    assert cfg.agent_names == ["lead", "worker1", "worker2"]
    assert cfg.roles["worker"].model == "m" and cfg.roles["worker"].effort == "low"
    assert cfg.roles["tester"].effort == "max"


@pytest.mark.parametrize("over, msg", [
    ({"workers": 0}, "workers"),
    ({"lead": {"effort": "huge"}}, "effort"),
    ({"permission_mode": "yolo"}, "permission_mode"),
])
def test_validation(tmp_path, over, msg):
    with pytest.raises(ConfigError, match=msg):
        load_config(tmp_path, over)


def test_unknown_key(tmp_path):
    (tmp_path / ".collab").mkdir()
    (tmp_path / ".collab" / "config.toml").write_text("wrokers = 2\n")
    with pytest.raises(ConfigError, match="wrokers"):
        load_config(tmp_path)


@pytest.mark.parametrize("text, verdict", [
    ("VERDICT: PASS\nall good", "PASS"),
    ("**VERDICT: FAIL**\nbroken", "FAIL"),
    ("Looks fine", None),
])
def test_verdict(text, verdict):
    assert TaskRecord("T1", "tester1", "p", result=text).verdict == verdict
