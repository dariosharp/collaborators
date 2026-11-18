"""Claude accounts the agents can run under, switchable at runtime with /switch.

Besides the implicit `login` one (Claude Code's own login), an account is either

- a token: a long-lived token made with `claude setup-token`, handed to each agent
  as CLAUDE_CODE_OAUTH_TOKEN. It is inference-only, so no Remote Control; or
- a login account: its own Claude Code config dir, holding a full claude.ai login
  made with `claude auth login --claudeai`, handed to each agent as
  CLAUDE_CONFIG_DIR. Conversations, settings and skills in it are symlinks to
  ~/.claude, so they are the same under every account; only the credentials and
  the account part of .claude.json are its own.

Either way switching account and resuming the same session id carries the
conversation over. Stored in ~/.config/collaborators/accounts.json, readable only
by the user (0600); login accounts' dirs in ~/.config/collaborators/accounts/ (0700).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import ConfigError, check_team_name, user_config_path

LOGIN = "login"  # Claude Code's own login: no token, no extra environment
ADD = "add"  # `/switch add`: not an account, the way to add one
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
VERSION = 1

# What a login account's config dir shares with ~/.claude, as symlinks: the
# conversations (so --resume works across accounts), settings, skills, plugins…
SHARED = ("projects", "sessions", "session-env", "shell-snapshots", "file-history",
          "plans", "todos", "skills", "agents", "commands", "plugins", "settings.json",
          "CLAUDE.md", "history.jsonl")
# The account's own, never shared: its login and its organisation's policies.
PRIVATE = (".credentials.json", ".claude.json", "policy-limits*", "remote-settings*")
# Keys of .claude.json that belong to one account (who is logged in, and what was
# cached for its organisation); a login account's copy starts without them.
ACCOUNT_KEYS = {"oauthAccount", "userID", "anonymousId", "primaryApiKey",
                "customApiKeyResponses", "claudeCodeFirstTokenDate",
                "managedSettingsNoticeSeenOrg", "claudeAiMcpEverConnected",
                "subscriptionNoticeCount", "hasAvailableSubscription"}
# Kept up to date from ~/.claude.json on every switch to a login account.
SYNCED_KEYS = ("mcpServers", "projects")


def accounts_path() -> Path:
    return user_config_path().parent / "accounts.json"


def login_dir(name: str) -> Path:
    """Where the login account `name` keeps its own Claude Code config dir."""
    return user_config_path().parent / "accounts" / name


def claude_home() -> Path:
    """Claude Code's own config dir (~/.claude, or $CLAUDE_CONFIG_DIR if set)."""
    return Path(os.environ.get(CONFIG_DIR_ENV) or Path.home() / ".claude")


def claude_json() -> Path:
    """Claude Code's own global state (~/.claude.json for the default dir)."""
    if os.environ.get(CONFIG_DIR_ENV):
        return claude_home() / ".claude.json"
    return Path.home() / ".claude.json"


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
    name = check_new_name(name)
    token = token.strip()
    if not token:
        raise ConfigError("the token is empty: paste the one `claude setup-token` prints")
    data = _read()
    old = data["accounts"].get(name)
    data["accounts"][name] = {"token": token}
    _write(data)
    if isinstance(old, dict) and old.get("config_dir"):
        _delete_login_dir(Path(old["config_dir"]))
    return name


def check_new_name(name: str) -> str:
    """A valid name for an account the user adds (or re-adds)."""
    name = check_team_name(name)
    if name == LOGIN:
        raise ConfigError(f"{LOGIN!r} is Claude Code's own login; pick another name")
    if name == ADD:
        raise ConfigError(f"{ADD!r} is taken by `/switch add`; pick another name")
    return name


def add_login(name: str) -> str:
    """Register `name` as a login account, using login_dir(name) (prepared and
    logged in beforehand, see prepare_login_dir and login_command). Replaces a
    token account of the same name."""
    name = check_new_name(name)
    path = login_dir(name)
    if not is_logged_in(path):
        raise ConfigError(f"{name!r} is not logged in: run "
                          f"`{CONFIG_DIR_ENV}={path} claude auth login --claudeai`")
    data = _read()
    data["accounts"][name] = {"config_dir": str(path)}
    _write(data)
    return name


def kind(name: str) -> str:
    """`login` (Claude Code's own), `claude.ai` (a login account) or `token`."""
    if name == LOGIN:
        return LOGIN
    info = _read()["accounts"].get(name)
    if not isinstance(info, dict):
        raise ConfigError(f"no account called {name!r}")
    return "claude.ai" if info.get("config_dir") else "token"


def supports_remote_control(name: str) -> bool:
    """Remote Control needs a claude.ai login: setup-tokens are inference-only."""
    try:
        return kind(name) != "token"
    except ConfigError:
        return False


def remove(name: str) -> None:
    data = _read()
    if name not in data["accounts"]:
        raise ConfigError(f"no account called {name!r}")
    info = data["accounts"].pop(name)
    if data.get("current") == name:
        data["current"] = LOGIN
    _write(data)
    if isinstance(info, dict) and info.get("config_dir"):
        _delete_login_dir(Path(info["config_dir"]))


def env_for(name: str) -> dict[str, str]:
    """The environment an agent needs to run under account `name`."""
    if name == LOGIN:
        return {}
    info = _read()["accounts"].get(name)
    if isinstance(info, dict) and info.get("config_dir"):
        path = Path(info["config_dir"])
        try:
            prepare_login_dir(path)  # new ~/.claude items, MCP servers, trusted projects
        except OSError as exc:
            raise ConfigError(f"cannot prepare {path} for the account {name!r}: "
                              f"{exc}") from exc
        return {CONFIG_DIR_ENV: str(path)}
    if not isinstance(info, dict) or not info.get("token"):
        raise ConfigError(f"no account called {name!r}")
    return {TOKEN_ENV: str(info["token"])}


# ---- login accounts' config dirs -----------------------------------------------------
def prepare_login_dir(path: Path) -> Path:
    """Create or refresh a login account's config dir: 0700, symlinks to what ~/.claude
    shares, and its own .claude.json, with ~/.claude.json's MCP servers and projects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.mkdir(exist_ok=True)
    os.chmod(path, 0o700)
    home = claude_home()
    (home / "projects").mkdir(parents=True, exist_ok=True)
    for item in SHARED:
        target, link = home / item, path / item
        if not target.exists():
            continue
        if link.is_symlink():
            if os.readlink(link) == str(target):
                continue
            link.unlink()  # stale or broken: point it at ~/.claude again
        elif link.exists():
            continue  # Claude Code made the account its own one before ~/.claude had it
        link.symlink_to(target, target_is_directory=target.is_dir())
    _sync_claude_json(path / ".claude.json")
    return path


def _sync_claude_json(dest: Path) -> None:
    try:
        main = json.loads(claude_json().read_text())
    except (OSError, json.JSONDecodeError):
        main = {}
    if not isinstance(main, dict):
        main = {}
    try:
        own = json.loads(dest.read_text())
    except FileNotFoundError:
        own = {k: v for k, v in main.items() if not _account_key(k)}
    except (OSError, json.JSONDecodeError):
        return  # Claude Code is writing it, or it is broken: leave it alone
    if not isinstance(own, dict):
        return
    for key in SYNCED_KEYS:
        if key in main:
            own[key] = main[key]
    tmp = dest.with_name(dest.name + ".collab-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(own, indent=2) + "\n")
    os.replace(tmp, dest)


def _account_key(key: str) -> bool:
    """A .claude.json key that belongs to the logged-in account (or its org's caches)."""
    return key in ACCOUNT_KEYS or "cache" in key.lower()


def _delete_login_dir(path: Path) -> None:
    """Delete a login account's dir: its own files only. Symlinks are unlinked, never
    followed, so what they point to in ~/.claude is untouched."""
    if path.is_symlink() or not path.is_dir():
        return
    for entry in os.scandir(path):
        p = Path(entry.path)
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            p.unlink()
        else:
            shutil.rmtree(p)  # a real dir of the account's own; rmtree never follows links
    path.rmdir()


def is_logged_in(path: Path) -> bool:
    """Has this config dir a claude.ai login? (Claude Code keeps it in .credentials.json
    on Linux.)"""
    return (path / ".credentials.json").is_file()


def claude_env(path: Path) -> dict[str, str]:
    """The environment for running `claude` against the config dir `path`: without
    anything that would make Claude Code log in some other way."""
    env = {k: v for k, v in os.environ.items()
           if k not in (TOKEN_ENV, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env[CONFIG_DIR_ENV] = str(path)
    return env


def login_command() -> list[str]:
    """`claude auth login --claudeai`: run it with claude_env(dir)."""
    return [shutil.which("claude") or "claude", "auth", "login", "--claudeai"]


def login_hint(path: Path) -> str:
    """The command a user can run themselves to log a config dir in."""
    return f"{CONFIG_DIR_ENV}={path} claude auth login --claudeai"


def login_status(path: Path) -> dict[str, Any]:
    """`claude auth status` for the config dir `path` ({} if it cannot be run)."""
    with contextlib.suppress(OSError, subprocess.SubprocessError, json.JSONDecodeError):
        out = subprocess.run([shutil.which("claude") or "claude", "auth", "status", "--json"],
                             env=claude_env(path), capture_output=True, text=True,
                             timeout=30).stdout
        data = json.loads(out)
        if isinstance(data, dict):
            return data
    return {}


def check_login(name: str) -> str:
    """After `claude auth login`: make sure login_dir(name) has a claude.ai login, and
    register the account. Returns a short description of who is logged in."""
    path = login_dir(name)
    status = login_status(path)
    if not status.get("loggedIn") or status.get("authMethod") != "claude.ai":
        raise ConfigError(f"no claude.ai login found for {name!r}; run "
                          f"`{login_hint(path)}`, then try again")
    add_login(name)
    return str(status.get("email") or status.get("orgName") or "logged in")
