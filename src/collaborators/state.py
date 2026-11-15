"""Per-project persistent state, kept in <workdir>/.collab/.

- team.json   — which conversation (session id) each agent owns, for --resume
- tasks.jsonl — one line per finished task (the task board history)

Keeping it inside the project (not in the install dir) means two teams in two
projects never step on each other, even when the directories share a name.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .events import TaskRecord

STATE_VERSION = 1


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.dir = state_dir
        self.team_file = state_dir / "team.json"
        self.tasks_file = state_dir / "tasks.jsonl"

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


def new_session_id() -> str:
    return str(uuid.uuid4())
