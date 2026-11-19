"""Token and context accounting per agent.

Every model call re-reads the agent's whole context, so what a conversation costs
is roughly its context size times the number of calls. Two numbers matter:

- the context size: the input of the last model call (fresh input + cache reads +
  cache writes), i.e. what the next call will read again;
- the totals since collab started: input, output, cache reads, cache writes, cost.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CTX_HIGH_TOKENS, CTX_WARN_TOKENS


@dataclass
class TokenUsage:
    """Tokens (and cost) of one turn, or a sum of turns."""
    input: int = 0        # fresh input, not from the cache
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0  # cache creation: what a cold cache costs again
    cost_usd: float = 0.0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def add(self, other: TokenUsage) -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.cost_usd += other.cost_usd

    def copy(self) -> TokenUsage:
        return TokenUsage(self.input, self.output, self.cache_read, self.cache_write,
                          self.cost_usd)

    @classmethod
    def from_api(cls, usage: dict[str, Any] | None, cost_usd: float = 0.0) -> TokenUsage:
        """From an API `usage` dict (as in ResultMessage.usage)."""
        u = usage or {}
        return cls(_int(u.get("input_tokens")), _int(u.get("output_tokens")),
                   _int(u.get("cache_read_input_tokens")),
                   _int(u.get("cache_creation_input_tokens")), cost_usd)


@dataclass
class AgentUsage:
    """What an agent's conversation weighs now, and what it used since collab started."""
    context_tokens: int | None = None  # None: unknown (e.g. resumed, no turn yet)
    estimated: bool = False            # context read from the transcript on disk
    totals: TokenUsage = field(default_factory=TokenUsage)
    turns: int = 0

    def copy(self) -> AgentUsage:
        return AgentUsage(self.context_tokens, self.estimated, self.totals.copy(), self.turns)


def context_of(usage: dict[str, Any] | None) -> int | None:
    """The context a model call read: its fresh input plus cache reads and writes."""
    if not usage:
        return None
    n = (_int(usage.get("input_tokens")) + _int(usage.get("cache_read_input_tokens"))
         + _int(usage.get("cache_creation_input_tokens")))
    return n or None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ---- formatting ------------------------------------------------------------------------
def fmt_tokens(n: int) -> str:
    """850, 12k, 137k, 2.4M."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k" if n < 10_000 else f"{n // 1000}k"
    return f"{n / 1_000_000:.1f}M"


def fmt_context(usage: AgentUsage | None) -> str:
    """`ctx 137k`, `ctx ~137k` (estimated from disk), `ctx ?` (unknown)."""
    if usage is None or usage.context_tokens is None:
        return "ctx ?"
    return f"ctx {'~' if usage.estimated else ''}{fmt_tokens(usage.context_tokens)}"


def context_level(tokens: int | None) -> str:
    """`ok`, `warn` (CTX_WARN_TOKENS+) or `high` (CTX_HIGH_TOKENS+)."""
    if tokens is None or tokens < CTX_WARN_TOKENS:
        return "ok"
    return "high" if tokens >= CTX_HIGH_TOKENS else "warn"


CONTEXT_STYLE = {"ok": "dim", "warn": "yellow", "high": "bold red"}


# ---- the context of a resumed conversation, from Claude Code's transcript ----------------
TAIL_BYTES = 256 * 1024  # the last usage is near the end; never read a whole transcript


def transcript_path(cwd: Path, session_id: str, env: dict[str, str] | None = None) -> Path | None:
    """Where Claude Code keeps the session's transcript: <config dir>/projects/
    <cwd with every non-alphanumeric char as '-'>/<session id>.jsonl."""
    base = Path((env or {}).get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
                or Path.home() / ".claude") / "projects"
    path = base / re.sub(r"[^A-Za-z0-9]", "-", str(cwd)) / f"{session_id}.jsonl"
    if path.is_file():
        return path
    # Long paths are shortened by Claude Code: look in every project dir (one level).
    try:
        return next((p for p in base.glob(f"*/{session_id}.jsonl") if p.is_file()), None)
    except OSError:
        return None


def context_from_transcript(path: Path) -> int | None:
    """The context size at the end of a transcript: the usage of its last main-chain
    assistant message. Reads only the file's tail."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return None
    lines = tail.split(b"\n")
    if len(tail) < size:
        lines = lines[1:]  # the first one is cut
    for raw in reversed(lines):
        if b'"usage"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            n = context_of(message["usage"])
            if n:
                return n
    return None
