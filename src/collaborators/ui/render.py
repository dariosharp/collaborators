"""Turn tool calls and results into compact, readable lines (and rich details)."""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text

TEAM = "mcp__team__"


@dataclass(frozen=True)
class ToolLine:
    icon: str
    verb: str
    target: str


def short_path(path: str, workdir: Path | None) -> str:
    if workdir is not None:
        try:
            return str(Path(path).resolve().relative_to(workdir))
        except (ValueError, OSError):
            pass
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def first_line(text: str, width: int = 90) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= width else line[: width - 1] + "…"


def describe_call(name: str, args: dict[str, Any], workdir: Path | None = None) -> ToolLine:
    def p(key: str = "file_path") -> str:
        return short_path(str(args.get(key, "")), workdir)

    if name == "Read":
        rng = ""
        if args.get("offset") or args.get("limit"):
            start = int(args.get("offset") or 1)
            rng = f":{start}-{start + int(args.get('limit') or 0)}" if args.get("limit") else ""
        return ToolLine("▤", "Read", p() + rng)
    if name in ("Edit", "MultiEdit"):
        return ToolLine("✎", "Edit", p())
    if name == "Write":
        return ToolLine("✚", "Write", p())
    if name == "NotebookEdit":
        return ToolLine("✎", "Notebook", p("notebook_path"))
    if name == "Bash":
        return ToolLine("$", "", first_line(str(args.get("command", "")), 120))
    if name == "Grep":
        where = f"  in {p('path')}" if args.get("path") else ""
        return ToolLine("⌕", "Grep", f"{args.get('pattern', '')}{where}")
    if name == "Glob":
        return ToolLine("⌕", "Glob", str(args.get("pattern", "")))
    if name in ("WebFetch", "WebSearch"):
        return ToolLine("◍", name, str(args.get("url") or args.get("query") or ""))
    if name in ("Task", "Agent"):
        return ToolLine("◆", "Subagent", str(args.get("description", "")))
    if name == "TodoWrite":
        todos = args.get("todos") or []
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        return ToolLine("☑", "Todos", f"{done}/{len(todos)} done")
    if name == TEAM + "dispatch_task":
        return ToolLine("▸", "Dispatch", f"→ {args.get('agent', '?')}  "
                        + first_line(str(args.get("task", "")), 70))
    if name == TEAM + "wait_tasks":
        ids = ", ".join(args.get("task_ids") or []) or "all running"
        mode = " (first to finish)" if args.get("mode") == "any" else ""
        return ToolLine("⧗", "Wait", ids + mode)
    if name == TEAM + "team_status":
        return ToolLine("☰", "Team status", "")
    if name == TEAM + "cancel_task":
        return ToolLine("✕", "Cancel", str(args.get("task_id", "")))
    if name.startswith("mcp__"):
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        return ToolLine("⚙", f"{server}·{tool}", _compact(args))
    return ToolLine("•", name, _compact(args))


def _compact(args: dict[str, Any], width: int = 80) -> str:
    text = json.dumps(args, ensure_ascii=False, default=str)
    return text if len(text) <= width else text[: width - 1] + "…"


def describe_result(name: str, args: dict[str, Any], content: str, is_error: bool) -> str:
    if is_error:
        return first_line(content) or "error"
    lines = [ln for ln in content.splitlines() if ln.strip()]
    if name in ("Edit", "MultiEdit"):
        edits = args.get("edits") or [args]
        added = removed = 0
        for e in edits:
            if isinstance(e, dict):
                old = str(e.get("old_string", "")).splitlines()
                new = str(e.get("new_string", "")).splitlines()
                for op in difflib.ndiff(old, new):
                    added += op.startswith("+ ")
                    removed += op.startswith("- ")
        return f"+{added} −{removed}"
    if name == "Write":
        n = len(str(args.get("content", "")).splitlines())
        return f"{n} line{'s' * (n != 1)}"
    if name == "Read":
        return f"{len(lines)} line{'s' * (len(lines) != 1)}"
    if name in ("Grep", "Glob"):
        return "no matches" if not lines else f"{len(lines)} result{'s' * (len(lines) != 1)}"
    if name == TEAM + "dispatch_task":
        return first_line(content)
    if name == TEAM + "wait_tasks":
        heads = [ln for ln in content.splitlines() if ln.startswith("T") and " · " in ln]
        return "; ".join(h.split(" · ")[0] + " " + h.split(" · ")[2] for h in heads) or \
            first_line(content)
    return first_line(lines[-1]) if lines else "✓"


def detail(name: str, args: dict[str, Any], content: str | None, is_error: bool) -> \
        RenderableType | None:
    """The expanded view of a tool call, for the agent detail screen."""
    parts: list[RenderableType] = []
    if name in ("Edit", "MultiEdit"):
        for e in args.get("edits") or [args]:
            if not isinstance(e, dict):
                continue
            diff = difflib.unified_diff(
                str(e.get("old_string", "")).splitlines(),
                str(e.get("new_string", "")).splitlines(), lineterm="", n=2)
            body = "\n".join(list(diff)[2:])
            if body:
                parts.append(Syntax(body, "diff", theme="ansi_dark", background_color="default"))
    elif name == "Write":
        text = str(args.get("content", ""))
        lexer = Syntax.guess_lexer(str(args.get("file_path", "")), text)
        clipped = "\n".join(text.splitlines()[:40])
        parts.append(Syntax(clipped, lexer, theme="ansi_dark", background_color="default",
                            line_numbers=True))
    elif name == "Bash" and "\n" in str(args.get("command", "")):
        # one-line commands are already in the header
        parts.append(Syntax(str(args["command"]), "bash", theme="ansi_dark",
                            background_color="default"))
    elif name == TEAM + "dispatch_task":
        parts.append(Text(str(args.get("task", "")), style="italic"))
    if content and name not in ("Read", "Edit", "MultiEdit", "Write", TEAM + "dispatch_task"):
        tail = content.splitlines()[-40:]
        parts.append(Text("\n".join(tail), style="red" if is_error else "dim"))
    elif content and is_error:
        parts.append(Text(content, style="red"))
    return Group(*parts) if parts else None
