"""Claude accounts the agents can run under, switchable at runtime with /switch.

Every account but the implicit `login` one is a long-lived token made with
`claude setup-token`, handed to each agent as CLAUDE_CODE_OAUTH_TOKEN. Settings,
skills, MCP servers and conversations all stay in ~/.claude, so switching account
and resuming the same session id carries the conversation over.

Stored in ~/.config/collaborators/accounts.json, readable only by the user (0600).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import ConfigError, check_team_name, user_config_path

LOGIN = "login"  # Claude Code's own login: no token, no extra environment
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
VERSION = 1


def accounts_path() -> Path:
    return user_config_path().parent / "accounts.json"


def _read() -> dict[str, Any]:
    try:
        data = json.loads(accounts_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": VERSION, "current": LOGIN, "accounts": {}}
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
        return {"version": VERSION, "current": LOGIN, "accounts": {}}
    return data


def _write(data: dict[str, Any]) -> None:
    path = accounts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def names() -> list[str]:
    return [LOGIN, *sorted(_read()["accounts"])]


def current() -> str:
    name = _read().get("current", LOGIN)
    return name if name in names() else LOGIN


def set_current(name: str) -> None:
    if name not in names():
        raise ConfigError(f"no account called {name!r}; accounts: {', '.join(names())}")
    data = _read()
    data["current"] = name
    _write(data)


def add(name: str, token: str) -> str:
    name = check_team_name(name)
    if name == LOGIN:
        raise ConfigError(f"{LOGIN!r} is Claude Code's own login; pick another name")
    token = token.strip()
    if not token:
        raise ConfigError("the token is empty: paste the one `claude setup-token` prints")
    data = _read()
    data["accounts"][name] = {"token": token}
    _write(data)
    return name


def remove(name: str) -> None:
    data = _read()
    if name not in data["accounts"]:
        raise ConfigError(f"no account called {name!r}")
    del data["accounts"][name]
    if data.get("current") == name:
        data["current"] = LOGIN
    _write(data)


def env_for(name: str) -> dict[str, str]:
    """The environment an agent needs to run under account `name`."""
    if name == LOGIN:
        return {}
    info = _read()["accounts"].get(name)
    if not isinstance(info, dict) or not info.get("token"):
        raise ConfigError(f"no account called {name!r}")
    return {TOKEN_ENV: str(info["token"])}
