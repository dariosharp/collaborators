"""Team configuration: built-in defaults < user config < project config < CLI flags."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

EFFORTS = ("low", "medium", "high", "xhigh", "max")
PERMISSION_MODES = ("bypassPermissions", "acceptEdits", "default", "auto")

ROLE_DEFAULTS: dict[str, tuple[str, str]] = {
    "lead": ("claude-fable-5-2", "medium"),
    "worker": ("claude-opus-5-5", "medium"),
    "tester": ("claude-opus-4-8", "medium"),
}


DEFAULT_TEAM = "main"
TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,19}")


class ConfigError(ValueError):
    pass


def check_team_name(name: str) -> str:
    name = name.strip().lower()
    if not TEAM_NAME.fullmatch(name):
        raise ConfigError("a team name is 1-20 lowercase letters, digits or dashes "
                          "(e.g. frontend, api-v2)")
    return name


@dataclass(frozen=True)
class RoleConfig:
    model: str
    effort: str

    def __post_init__(self) -> None:
        if self.effort not in EFFORTS:
            raise ConfigError(f"effort must be one of {', '.join(EFFORTS)} (got {self.effort!r})")


@dataclass(frozen=True)
class TeamConfig:
    workdir: Path
    workers: int = 1
    testers: int = 1
    permission_mode: str = "bypassPermissions"
    ask: bool = False  # route permission prompts to the UI
    team: str = DEFAULT_TEAM  # several teams (tabs) can share one directory
    worktrees: bool = False  # each worker in its own git worktree; the Lead merges
    notify: bool = True  # desktop notification when the Lead finishes a long turn
    roles: dict[str, RoleConfig] = field(
        default_factory=lambda: {r: RoleConfig(*v) for r, v in ROLE_DEFAULTS.items()}
    )

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise ConfigError("workers must be >= 1")
        if self.testers < 0:
            raise ConfigError("testers must be >= 0")
        if self.permission_mode not in PERMISSION_MODES:
            raise ConfigError(
                f"permission_mode must be one of {', '.join(PERMISSION_MODES)}"
            )

    @property
    def project_dir(self) -> Path:
        """<workdir>/.collab: project config, and the state of the default team."""
        return self.workdir / ".collab"

    @property
    def state_dir(self) -> Path:
        if self.team == DEFAULT_TEAM:
            return self.project_dir
        return self.project_dir / "teams" / self.team

    @property
    def agent_names(self) -> list[str]:
        return (["lead"] + [f"worker{i}" for i in range(1, self.workers + 1)]
                + [f"tester{i}" for i in range(1, self.testers + 1)])

    @staticmethod
    def role_of(agent: str) -> str:
        return agent.rstrip("0123456789")

    def role_config(self, agent: str) -> RoleConfig:
        return self.roles[self.role_of(agent)]


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "collaborators" / "config.toml"


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _apply(cfg: TeamConfig, data: dict[str, Any], source: str) -> TeamConfig:
    flags = ("workers", "testers", "permission_mode", "ask", "worktrees", "notify")
    known = {*flags, *ROLE_DEFAULTS}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{source}: unknown key(s): {', '.join(sorted(unknown))}")
    roles = dict(cfg.roles)
    for role in ROLE_DEFAULTS:
        section = data.get(role)
        if section is None:
            continue
        if not isinstance(section, dict) or set(section) - {"model", "effort"}:
            raise ConfigError(f"{source}: [{role}] accepts only 'model' and 'effort'")
        roles[role] = RoleConfig(section.get("model", roles[role].model),
                                 section.get("effort", roles[role].effort))
    scalars = {k: data[k] for k in flags if k in data}
    return replace(cfg, roles=roles, **scalars)


def load_config(workdir: Path, overrides: dict[str, Any] | None = None) -> TeamConfig:
    """Build the effective config for `workdir`.

    `overrides` uses the same shape as the TOML files (e.g. {"workers": 2,
    "worker": {"model": "..."}}); values that are None are ignored.
    """
    cfg = TeamConfig(workdir=workdir.resolve())
    for path in (user_config_path(), cfg.project_dir / "config.toml"):
        cfg = _apply(cfg, _read_toml(path), str(path))
    if overrides:
        clean: dict[str, Any] = {}
        for key, value in overrides.items():
            if isinstance(value, dict):
                value = {k: v for k, v in value.items() if v is not None}
                if value:
                    clean[key] = value
            elif value is not None:
                clean[key] = value
        cfg = _apply(cfg, clean, "command line")
    return cfg
