"""Per-project persistent state, kept in <workdir>/.collab/.

- team.json   — which conversation (session id) each agent owns, for --resume
- tasks.jsonl — one line per finished task (the task board history)
- tabs.json   — which teams (tabs) were open, for --resume

The default team lives in .collab/ itself; other teams in .collab/teams/<name>/.

Keeping it inside the project (not in the install dir) means two teams in two
projects never step on each other, even when the directories share a name.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .config import is_model_id
from .events import TaskRecord

STATE_VERSION = 1


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir
        self.team_file = state_dir / "team.json"
        self.tasks_file = state_dir / "tasks.jsonl"
        self.reports_dir = state_dir / "reports"  # every task's full final report

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        ignore = self.dir / ".gitignore"
        if not ignore.exists():
            # Keep the team's state out of the user's git without touching their .gitignore.
            ignore.write_text("*\n")

    # ---- sessions -------------------------------------------------------
    def load_sessions(self) -> dict[str, str]:
        try:
            data = json.loads(self.team_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        agents = data.get("agents", {}) if isinstance(data, dict) else {}
        return {name: info["session_id"] for name, info in agents.items()
                if isinstance(info, dict) and isinstance(info.get("session_id"), str)}

    def load_models(self) -> dict[str, str]:
        """The models the user picked with /model, by agent (saved with model_chosen):
        the only saved models that win over the config on --resume."""
        try:
            data = json.loads(self.team_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        agents = data.get("agents", {}) if isinstance(data, dict) else {}
        # A display name ("Opus 5.5 (default)", saved by older versions) is not a
        # model Claude Code accepts: skip it, so the config's model is used instead.
        return {name: info["model"] for name, info in agents.items()
                if isinstance(info, dict) and info.get("model_chosen")
                and isinstance(info.get("model"), str) and is_model_id(info["model"])}

    def save_sessions(self, sessions: dict[str, str], meta: dict[str, dict[str, Any]]) -> None:
        self.ensure()
        payload = {
            "version": STATE_VERSION,
            "agents": {name: {"session_id": sid, **meta.get(name, {})}
                       for name, sid in sessions.items()},
        }
        tmp = self.team_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp, self.team_file)

    # ---- tasks ----------------------------------------------------------
    def append_task(self, task: TaskRecord) -> None:
        self.ensure()
        with self.tasks_file.open("a") as fh:
            fh.write(json.dumps(task.to_json()) + "\n")

    def save_report(self, task_id: str, text: str) -> Path:
        self.ensure()
        self.reports_dir.mkdir(exist_ok=True)
        path = self.reports_dir / f"{task_id}.md"
        path.write_text(text if text.endswith("\n") else text + "\n")
        return path

    def last_task_number(self) -> int:
        """Highest T<n> id on record, so task ids keep counting across restarts."""
        best = 0
        try:
            lines = self.tasks_file.read_text().splitlines()
        except FileNotFoundError:
            return 0
        for line in lines:
            try:
                tid = json.loads(line).get("id", "")
            except json.JSONDecodeError:
                continue
            if isinstance(tid, str) and tid[1:].isdigit():
                best = max(best, int(tid[1:]))
        return best


def load_open_teams(project_dir: Path) -> list[str]:
    """The teams (tabs) that were open when collab last quit, in tab order."""
    try:
        data = json.loads((project_dir / "tabs.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    teams = data.get("teams") if isinstance(data, dict) else None
    return [t for t in teams if isinstance(t, str)] if isinstance(teams, list) else []


def save_open_teams(project_dir: Path, teams: list[str]) -> None:
    StateStore(project_dir).ensure()
    tmp = project_dir / "tabs.tmp"
    tmp.write_text(json.dumps({"version": STATE_VERSION, "teams": teams}) + "\n")
    os.replace(tmp, project_dir / "tabs.json")


def has_saved_team(state_dir: Path) -> bool:
    return bool(StateStore(state_dir).load_sessions())


def new_session_id() -> str:
    return str(uuid.uuid4())
