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

# "default": pass no model, so Claude Code picks its own (the account's default, or
# the user's settings).
DEFAULT_MODEL = "default"

ROLE_DEFAULTS: dict[str, tuple[str, str]] = {
    "lead": (DEFAULT_MODEL, "medium"),
    "worker": ("claude-opus-5-5", "medium"),
    "tester": ("claude-sonnet-5-5", "medium"),
}


# Context size (tokens) at which an agent's header turns yellow, then red: every call
# re-reads the whole context. Later: configurable, and auto-compaction thresholds.
CTX_WARN_TOKENS = 150_000
CTX_HIGH_TOKENS = 250_000

# Compact an agent's conversation (/compact) right after its turn when its context
# is above this many tokens, while the prompt cache is still warm. 0: never.
COMPACT_AT_DEFAULTS = {"lead": 300_000, "worker": 250_000, "tester": 250_000}
# Reports longer than this are cut in what the Lead reads (the full one is saved).
REPORT_MAX_CHARS = 3000

DEFAULT_TEAM = "main"
TEAM_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,19}")


def is_default_model(model: str | None) -> bool:
    """True when no model should be passed to Claude Code."""
    return not model or model.strip().lower() == DEFAULT_MODEL


def is_model_id(model: str | None) -> bool:
    """A value Claude Code accepts as a model (an id or alias such as `opus`), not a
    display name such as "Opus 5.5 (default)"."""
    return bool(model) and not re.search(r"[\s()]", str(model))


def model_label(model: str | None) -> str:
    """How a model reads in text for the Lead (status, announcements)."""
    return "Claude Code's default model" if is_default_model(model) else str(model)


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
    # Testers start a new conversation for every task unless this is true.
    tester_memory: bool = False
    compact_at: dict[str, int] = field(default_factory=lambda: dict(COMPACT_AT_DEFAULTS))
    report_max_chars: int = REPORT_MAX_CHARS  # 0: never cut
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
        if set(self.compact_at) - set(COMPACT_AT_DEFAULTS) or any(
                not isinstance(v, int) or isinstance(v, bool) or v < 0
                for v in self.compact_at.values()):
            raise ConfigError("compact_at takes lead, worker and tester, each a token "
                              "count >= 0 (0: never compact)")
        if not isinstance(self.report_max_chars, int) or self.report_max_chars < 0:
            raise ConfigError("report_max_chars must be >= 0 (0: never cut)")

    def compact_threshold(self, agent: str) -> int:
        """Context size (tokens) above which `agent` is compacted after a turn; 0: never.
        Testers without memory start fresh anyway."""
        role = self.role_of(agent)
        if role == "tester" and not self.tester_memory:
            return 0
        return self.compact_at.get(role, 0)

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
    flags = ("workers", "testers", "permission_mode", "ask", "worktrees", "notify",
             "tester_memory", "report_max_chars")
    known = {*flags, *ROLE_DEFAULTS, "compact_at"}
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
    scalars: dict[str, Any] = {k: data[k] for k in flags if k in data}
    if "compact_at" in data:  # a table: the roles given replace the ones before
        if not isinstance(data["compact_at"], dict):
            raise ConfigError(f"{source}: compact_at is a table, e.g. "
                              "compact_at = { lead = 120000, worker = 100000 }")
        scalars["compact_at"] = {**cfg.compact_at, **data["compact_at"]}
    try:
        return replace(cfg, roles=roles, **scalars)
    except ConfigError as exc:
        raise ConfigError(f"{source}: {exc}") from exc


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
