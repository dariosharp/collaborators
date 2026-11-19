"""Token and context accounting: measuring, summing, formatting, and the resume estimate."""

import asyncio
import json
import re
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from collaborators.agents import AgentRunner, AgentSpec
from collaborators.backend_fake import FakeSession, fake_backend
from collaborators.backend_sdk import SdkSession
from collaborators.config import CTX_HIGH_TOKENS, CTX_WARN_TOKENS, load_config
from collaborators.events import TurnResult, UsageChanged
from collaborators.orchestrator import Team
from collaborators.usage import (
    TAIL_BYTES,
    AgentUsage,
    TokenUsage,
    context_from_transcript,
    context_level,
    fmt_context,
    fmt_tokens,
    transcript_path,
)


# ---- formatting ---------------------------------------------------------------------
def test_fmt_tokens_and_context():
    assert [fmt_tokens(n) for n in (0, 850, 1_234, 12_400, 137_499, 2_400_000)] == \
        ["0", "850", "1.2k", "12k", "137k", "2.4M"]
    assert fmt_context(None) == "ctx ?" and fmt_context(AgentUsage()) == "ctx ?"
    assert fmt_context(AgentUsage(context_tokens=137_000)) == "ctx 137k"
    assert fmt_context(AgentUsage(context_tokens=137_000, estimated=True)) == "ctx ~137k"


def test_context_level_thresholds():
    assert context_level(None) == "ok"
    assert context_level(CTX_WARN_TOKENS - 1) == "ok"
    assert context_level(CTX_WARN_TOKENS) == "warn"
    assert context_level(CTX_HIGH_TOKENS - 1) == "warn"
    assert context_level(CTX_HIGH_TOKENS) == "high"


def test_header_usage_text():
    from collaborators.ui.widgets import usage_text
    assert usage_text(None).plain == "" and usage_text(AgentUsage()).plain == ""
    u = AgentUsage(context_tokens=160_000,
                   totals=TokenUsage(input=1_000, output=99_000, cache_read=2_300_000))
    t = usage_text(u)
    assert t.plain == " · ctx 160k · 2.4M tok"

    def style_of(text, word):
        start = text.plain.index(word)
        return [str(s.style) for s in text.spans if s.start <= start < s.end][0]

    assert style_of(t, "ctx") == "yellow"
    assert style_of(usage_text(AgentUsage(context_tokens=137_000)), "ctx") == "dim"
    assert (CTX_WARN_TOKENS, CTX_HIGH_TOKENS) == (150_000, 250_000)
    assert "red" in style_of(usage_text(AgentUsage(context_tokens=CTX_HIGH_TOKENS)), "ctx")
    # Only the session total, while the context isn't known yet (resumed, no turn).
    assert usage_text(AgentUsage(totals=TokenUsage(output=5_000))).plain == " · 5.0k tok"


# ---- the fake backend, through the team ------------------------------------------------
def make_team(tmp_path: Path, workers: int = 1):
    cfg = load_config(tmp_path, {"workers": workers, "testers": 0})
    events: list = []
    backend = fake_backend(speed=0)
    return Team(cfg, backend, events.append), events


async def test_usage_is_summed_per_agent_and_recorded_per_task(tmp_path):
    team, events = make_team(tmp_path)
    await team.start()
    runner = team.runners["worker1"]
    t1 = team.dispatch("worker1", "add a --dry-run flag to scripts/import_data.py")
    await team.wait([t1.id])
    first = runner.usage.copy()
    assert first.turns == 1 and first.context_tokens and not first.estimated
    assert first.totals.total > first.context_tokens  # each call re-read the context
    assert first.totals.cost_usd == runner.total_cost > 0
    # The task carries its own tokens and the context at its end.
    assert t1.context_tokens == first.context_tokens
    assert t1.output_tokens == first.totals.output and t1.cache_read_tokens > 0
    saved = json.loads(team.store.tasks_file.read_text().splitlines()[-1])
    assert saved["context_tokens"] == first.context_tokens
    assert saved["cache_write_tokens"] == t1.cache_write_tokens > 0
    assert f"worker1's context now {fmt_tokens(first.context_tokens)} tokens" \
        in team.task_report(t1)
    t2 = team.dispatch("worker1", "and a test for it")
    await team.wait([t2.id])
    # The context grows; totals add up over the turns.
    assert runner.usage.turns == 2 and runner.usage.context_tokens > first.context_tokens
    assert runner.usage.totals.total == first.totals.total + (
        t2.input_tokens + t2.output_tokens + t2.cache_read_tokens + t2.cache_write_tokens)
    assert f"effort medium, {fmt_context(runner.usage)}]: idle" in team.status_report()
    updates = [e for e in events if isinstance(e, UsageChanged) and e.agent == "worker1"]
    assert len(updates) == 2 and updates[-1].usage == runner.usage
    assert updates[0].usage is not runner.usage  # snapshots, not the live object
    await team.shutdown()


async def test_turns_started_from_claude_ai_count_too(tmp_path):
    spec = AgentSpec(name="lead", role="lead", model="m", effort="medium", cwd=tmp_path,
                     system_append="", session_id="s")
    events: list = []
    runner = AgentRunner(spec, FakeSession(spec, None, speed=0), events.append)  # type: ignore[arg-type]
    runner._on_remote(TurnResult("lead", "hi", cost_usd=0.3, context_tokens=42_000,
                                 usage=TokenUsage(input=10, output=500, cache_read=41_000,
                                                  cache_write=990, cost_usd=0.3)))
    assert runner.usage.context_tokens == 42_000 and runner.usage.turns == 1
    assert runner.usage.totals == TokenUsage(10, 500, 41_000, 990, 0.3)
    assert [e.usage.context_tokens for e in events if isinstance(e, UsageChanged)] == [42_000]


# ---- the SDK session: context of the last main-chain model call --------------------------
def assistant(text: str, usage: dict | None, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m", usage=usage,
                            parent_tool_use_id=parent)


class ScriptedClient:
    def __init__(self, messages) -> None:
        self.out: asyncio.Queue = asyncio.Queue()
        self.messages = messages

    async def receive_messages(self):
        while True:
            yield await self.out.get()

    async def query(self, prompt: str) -> None:
        for m in self.messages:
            self.out.put_nowait(m)

    async def disconnect(self) -> None:
        pass


async def run_sdk_turn(tmp_path, messages):
    spec = AgentSpec(name="worker1", role="worker", model="m", effort="medium", cwd=tmp_path,
                     system_append="", session_id="s")
    session = SdkSession(spec)
    session._client = ScriptedClient(messages)  # type: ignore[assignment]
    runner = AgentRunner(spec, session, lambda e: None)
    session._pump_task = asyncio.create_task(session._pump())
    result = await runner.run("go")
    await session.close()
    return runner, result


async def test_sdk_context_is_the_last_main_call_and_totals_split_the_cache(tmp_path):
    call = {"input_tokens": 5, "cache_read_input_tokens": 30_000,
            "cache_creation_input_tokens": 1_000, "output_tokens": 200}
    later = {**call, "cache_read_input_tokens": 31_000, "cache_creation_input_tokens": 700}
    sub = {**call, "cache_read_input_tokens": 90_000}  # a subagent's own context
    turn = {"input_tokens": 10, "cache_read_input_tokens": 61_000,
            "cache_creation_input_tokens": 1_700, "output_tokens": 400}
    runner, result = await run_sdk_turn(tmp_path, [
        assistant("thinking", call), assistant("done", later),
        assistant("sub", sub, parent="tool-1"),
        ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                      num_turns=2, session_id="s", total_cost_usd=0.25, result="done",
                      usage=turn),
    ])
    assert result.context_tokens == 5 + 31_000 + 700
    assert result.usage == TokenUsage(10, 400, 61_000, 1_700, 0.25)
    assert runner.usage.context_tokens == 31_705 and runner.usage.totals.cost_usd == 0.25


async def test_sdk_context_falls_back_to_a_one_call_result(tmp_path):
    turn = {"input_tokens": 3, "cache_read_input_tokens": 20_000,
            "cache_creation_input_tokens": 500, "output_tokens": 50}

    def result(num_turns: int) -> ResultMessage:
        return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                             is_error=False, num_turns=num_turns, session_id="s",
                             total_cost_usd=0.01, result="ok", usage=turn)

    _, one = await run_sdk_turn(tmp_path, [assistant("ok", None), result(1)])
    assert one.context_tokens == 20_503
    # Several calls: the result's usage is their sum, not a context size.
    runner, many = await run_sdk_turn(tmp_path, [assistant("ok", None), result(3)])
    assert many.context_tokens is None and runner.usage.context_tokens is None


# ---- resume: the context from the transcript's tail ---------------------------------------
def write_transcript(path: Path, entries: list[dict], pad_to: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in entries]
    filler = json.dumps({"type": "user", "message": {"content": "x" * 1000}})
    head = [filler] * (pad_to // len(filler) + 1) if pad_to else []
    path.write_text("\n".join(head + lines) + "\n")


def usage_entry(read: int, sidechain: bool = False) -> dict:
    return {"type": "assistant", "isSidechain": sidechain,
            "message": {"usage": {"input_tokens": 2, "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": 98, "output_tokens": 10}}}


def test_context_from_transcript_reads_only_the_tail(tmp_path):
    path = tmp_path / "s.jsonl"
    write_transcript(path, [usage_entry(50_000), usage_entry(64_900),
                            usage_entry(200_000, sidechain=True),
                            {"type": "user", "message": {"content": "next"}}],
                     pad_to=TAIL_BYTES * 2)  # a big file: only its end is read
    assert path.stat().st_size > TAIL_BYTES * 2
    assert context_from_transcript(path) == 65_000
    assert context_from_transcript(tmp_path / "missing.jsonl") is None
    (tmp_path / "junk.jsonl").write_text('{"usage": broken\n')
    assert context_from_transcript(tmp_path / "junk.jsonl") is None


def test_transcript_path(tmp_path, monkeypatch):
    cwd = Path("/home/me/my_project.v2")
    home = Path.home() / ".claude" / "projects"
    path = home / "-home-me-my-project-v2" / "abc.jsonl"
    write_transcript(path, [usage_entry(1)])
    assert transcript_path(cwd, "abc") == path
    # Shortened project dirs are found by the session id.
    other = home / "-home-me-shortened-123" / "def.jsonl"
    write_transcript(other, [usage_entry(1)])
    assert transcript_path(cwd, "def") == other
    assert transcript_path(cwd, "nope") is None
    # A login account's config dir.
    own = tmp_path / "acct" / "projects" / "-home-me-my-project-v2" / "abc.jsonl"
    write_transcript(own, [usage_entry(1)])
    assert transcript_path(cwd, "abc", {"CLAUDE_CONFIG_DIR": str(tmp_path / "acct")}) == own


async def test_resumed_agent_estimates_its_context_until_a_turn_says(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    escaped = re.sub(r"[^A-Za-z0-9]", "-", str(project))  # as Claude Code names it
    transcript = Path.home() / ".claude" / "projects" / escaped / "s.jsonl"
    write_transcript(transcript, [usage_entry(80_000)])
    spec = AgentSpec(name="worker1", role="worker", model="m", effort="medium", cwd=project,
                     system_append="", session_id="s", resume=True)
    events: list = []
    session = FakeSession(spec, None, speed=0)  # type: ignore[arg-type]
    runner = AgentRunner(spec, session, events.append)
    await runner.start()
    await runner._estimating
    assert runner.usage.context_tokens == 80_100 and runner.usage.estimated
    assert fmt_context(runner.usage) == "ctx ~80k"
    assert any(isinstance(e, UsageChanged) and e.usage.estimated for e in events)
    # A fresh conversation has nothing to estimate.
    fresh = AgentRunner(AgentSpec(name="w2", role="worker", model="m", effort="medium",
                                  cwd=project, system_append="", session_id="s"),
                        session, events.append)
    await fresh.start()
    assert fresh._estimating is None and fresh.usage.context_tokens is None
