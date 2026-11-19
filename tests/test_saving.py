"""Token saving: fresh testers, compaction after a turn, short reports to the Lead."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from collaborators.backend_fake import FakeSession, fake_backend
from collaborators.config import COMPACT_AT_DEFAULTS, ConfigError, load_config
from collaborators.events import AgentStatus, Event, Notice, TaskStatus, TaskUpdate
from collaborators.orchestrator import COMPACT_KEEP, Team

LONG = "\n".join(f"line {i}: " + "detail " * 12 for i in range(100))  # ~9000 chars


def brain(reports: dict[str, str] | None = None):
    async def run(s: FakeSession, prompt: str) -> AsyncIterator[Event]:
        yield s.done((reports or {}).get(s.name, f"{s.name} did: {prompt}"), cost=0.01)
    return run


def make_team(tmp_path: Path, testers: int = 1, reports=None, **config) -> tuple[Team, list,
                                                                                 dict]:
    cfg = load_config(tmp_path, {"workers": 1, "testers": testers, **config})
    events: list[Event] = []
    backend = fake_backend(brain(reports), speed=0)
    return Team(cfg, backend, events.append), events, backend.sessions  # type: ignore[attr-defined]


async def until(cond, rounds: int = 500) -> None:
    for _ in range(rounds):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never met")


def notices(events, agent: str) -> list[Notice]:
    return [e for e in events if isinstance(e, Notice) and e.agent == agent]


# ---- config ---------------------------------------------------------------------------
def test_config_defaults_and_toml(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.tester_memory is False and cfg.report_max_chars == 3000
    assert cfg.compact_at == COMPACT_AT_DEFAULTS == {"lead": 300_000, "worker": 250_000,
                                                     "tester": 250_000}
    assert cfg.compact_threshold("lead") == 300_000
    assert cfg.compact_threshold("worker2") == 250_000
    assert cfg.compact_threshold("tester1") == 0  # starts fresh anyway
    (tmp_path / ".collab").mkdir()
    (tmp_path / ".collab" / "config.toml").write_text(
        "tester_memory = true\nreport_max_chars = 0\ncompact_at = { worker = 0 }\n")
    cfg = load_config(tmp_path)
    assert cfg.tester_memory and cfg.report_max_chars == 0
    assert cfg.compact_at == {"lead": 300_000, "worker": 0, "tester": 250_000}
    assert cfg.compact_threshold("worker1") == 0 and cfg.compact_threshold("tester1") == 250_000


@pytest.mark.parametrize("over, msg", [
    ({"compact_at": {"worker": -1}}, "compact_at"),
    ({"compact_at": {"wizard": 5}}, "compact_at"),
    ({"report_max_chars": -5}, "report_max_chars"),
])
def test_config_validation(tmp_path, over, msg):
    with pytest.raises(ConfigError, match=msg):
        load_config(tmp_path, over)


# ---- 1. testers start fresh -------------------------------------------------------------
async def test_tester_gets_a_new_session_for_every_task(tmp_path):
    team, events, sessions = make_team(tmp_path)
    await team.start()
    first = sessions["tester1"]
    t1 = team.dispatch("tester1", "verify A")
    await team.wait([t1.id])
    assert sessions["tester1"] is first  # the first task needs no new session
    t2 = team.dispatch("tester1", "verify B")
    await team.wait([t2.id])
    second = sessions["tester1"]
    assert second is not first and second.session_id != first.session_id
    assert not second.spec.resume and second.prompts == ["verify B"]
    assert team.store.load_sessions()["tester1"] == second.session_id
    assert any(n.text == f"— new session for {t2.id} —" for n in notices(events, "tester1"))
    # Workers keep theirs.
    w = sessions["worker1"]
    for prompt in ("do A", "do B"):
        await team.wait([team.dispatch("worker1", prompt).id])
    assert sessions["worker1"] is w and w.prompts == ["do A", "do B"]
    await team.shutdown()


async def test_tester_memory_keeps_the_conversation(tmp_path):
    team, events, sessions = make_team(tmp_path, tester_memory=True)
    await team.start()
    first = sessions["tester1"]
    for prompt in ("verify A", "verify B"):
        await team.wait([team.dispatch("tester1", prompt).id])
    assert sessions["tester1"] is first and first.prompts == ["verify A", "verify B"]
    assert not any("new session" in n.text for n in notices(events, "tester1"))
    await team.shutdown()


async def test_resumed_tester_starts_fresh_on_its_first_task(tmp_path):
    team, _, sessions = make_team(tmp_path)
    await team.start()
    await team.wait([team.dispatch("tester1", "verify A").id])
    old = sessions["tester1"].session_id
    await team.shutdown()
    cfg = load_config(tmp_path, {"workers": 1, "testers": 1})
    backend = fake_backend(brain(), speed=0)
    again = Team(cfg, backend, lambda e: None, resume=True)
    await again.start()
    assert backend.sessions["tester1"].spec.resume  # type: ignore[attr-defined]
    await again.wait([again.dispatch("tester1", "verify B").id])
    fresh = backend.sessions["tester1"]  # type: ignore[attr-defined]
    assert fresh.session_id != old and not fresh.spec.resume
    await again.shutdown()


# ---- 2. compaction after a turn ----------------------------------------------------------
# One fake turn leaves a context of 20.8k tokens; a compaction brings it to ~20.1k.
OVER = {"lead": 20_500, "worker": 20_500, "tester": 20_500}


async def test_worker_compacts_after_its_task_when_over_the_threshold(tmp_path):
    team, events, sessions = make_team(tmp_path, compact_at=OVER)
    await team.start()
    runner, session = team.runners["worker1"], sessions["worker1"]
    t1 = team.dispatch("worker1", "build it")
    await team.wait([t1.id])
    await until(lambda: session.compactions and not runner.compacting)
    assert session.compactions == [COMPACT_KEEP["worker"]]
    assert "exact paths of files you changed or studied" in session.compactions[0]
    assert "re-read a file before editing" in session.compactions[0]
    assert session.prompts[-1].startswith("/compact Keep precisely: ")
    # The report went out first; the compaction came after.
    done_at = next(i for i, e in enumerate(events)
                   if isinstance(e, TaskUpdate) and e.task.status is TaskStatus.DONE)
    note = next(i for i, e in enumerate(events) if isinstance(e, Notice)
                and e.text.startswith("Compacted:"))
    assert done_at < note
    assert events[note].text == f"Compacted: 20k → {session.context // 1000}k"
    assert runner.usage.context_tokens == session.context < t1.context_tokens
    assert runner.status is AgentStatus.IDLE
    statuses = [e.status for e in events if getattr(e, "agent", "") == "worker1"
                and hasattr(e, "status")]
    assert AgentStatus.COMPACTING in statuses
    # The summary's tokens are counted; the turn count too.
    assert runner.usage.turns == 2
    await team.shutdown()


async def test_no_compaction_under_the_threshold_or_when_disabled(tmp_path):
    for compact_at in ({"worker": 50_000}, {"worker": 0}):
        team, events, sessions = make_team(tmp_path / str(compact_at["worker"]),
                                           compact_at=compact_at)
        await team.start()
        await team.wait([team.dispatch("worker1", "build it").id])
        for _ in range(50):
            await asyncio.sleep(0)
        assert sessions["worker1"].compactions == []
        await team.shutdown()


async def test_dispatch_during_compaction_waits_for_it(tmp_path):
    team, events, sessions = make_team(tmp_path, compact_at=OVER)
    await team.start()
    runner, session = team.runners["worker1"], sessions["worker1"]
    session.compact_gate = asyncio.Event()
    await team.wait([team.dispatch("worker1", "first").id])
    await until(lambda: runner.compacting and runner.status is AgentStatus.COMPACTING)
    t2 = team.dispatch("worker1", "second")  # no "busy" error
    for _ in range(50):
        await asyncio.sleep(0)
    assert t2.status is TaskStatus.RUNNING and "second" not in session.prompts
    session.compact_gate.set()
    await team.wait([t2.id])
    assert t2.status is TaskStatus.DONE
    # (after "second" it is over the threshold again: another compaction may follow)
    assert [p.split()[0] for p in session.prompts][:3] == ["first", "/compact", "second"]
    await team.shutdown()


async def test_failed_compaction_warns_and_carries_on(tmp_path):
    team, events, sessions = make_team(tmp_path, compact_at=OVER)
    await team.start()
    runner, session = team.runners["worker1"], sessions["worker1"]
    session.compact_fails = True
    t1 = team.dispatch("worker1", "build it")
    await team.wait([t1.id])
    await until(lambda: session.compactions and not runner.compacting)
    warning = [n for n in notices(events, "worker1") if n.level == "warning"]
    assert warning and warning[0].text.startswith("Compaction failed")
    assert runner.status is AgentStatus.IDLE
    assert runner.usage.context_tokens == t1.context_tokens  # unchanged
    t2 = team.dispatch("worker1", "go on")
    await team.wait([t2.id])
    assert t2.status is TaskStatus.DONE
    await team.shutdown()


async def test_lead_compacts_after_its_turn_when_nothing_is_pending(tmp_path):
    team, events, sessions = make_team(tmp_path, compact_at=OVER)
    await team.start()
    lead = sessions["lead"]
    team.tell_lead("first")
    team.tell_lead("second")  # pending while the first turn runs: no compaction between
    await until(lambda: lead.compactions and not team.runners["lead"].compacting)
    assert lead.compactions == [COMPACT_KEEP["lead"]]
    assert "the user's goals and decisions" in lead.compactions[0]
    assert [p.split()[0] for p in lead.prompts] == ["first", "second", "/compact"]
    assert any(n.text.startswith("Compacted:") for n in notices(events, "lead"))
    await team.shutdown()


# ---- 3. reports ------------------------------------------------------------------------
async def test_full_reports_are_saved_and_long_ones_cut_for_the_lead(tmp_path):
    team, _, _ = make_team(tmp_path, testers=0, reports={"worker1": LONG})
    await team.start()
    t1 = team.dispatch("worker1", "build it")
    await team.wait([t1.id])
    saved = tmp_path / ".collab" / "reports" / f"{t1.id}.md"
    assert saved.read_text().strip() == LONG.strip()
    assert t1.report_file == f".collab/reports/{t1.id}.md"
    report = team.task_report(t1)
    body = report.split("--- report ---\n", 1)[1]
    assert len(body) < 3000 + 200 and body.startswith("line 0:")
    assert body.endswith(f"… (truncated; full report: .collab/reports/{t1.id}.md — Read it "
                         "if you need the details)")
    assert "line 99" not in body
    await team.shutdown()


async def test_short_reports_pass_unchanged_and_other_teams_have_their_own_dir(tmp_path):
    cfg = replace(load_config(tmp_path, {"workers": 1, "testers": 0}), team="api")
    team = Team(cfg, fake_backend(brain(), speed=0), lambda e: None)
    await team.start()
    t1 = team.dispatch("worker1", "build it")
    await team.wait([t1.id])
    assert team.task_report(t1).endswith("--- report ---\nworker1 did: build it")
    path = tmp_path / ".collab" / "teams" / "api" / "reports" / f"{t1.id}.md"
    assert path.read_text() == "worker1 did: build it\n"
    await team.shutdown()


async def test_report_max_chars_zero_never_cuts(tmp_path):
    team, _, _ = make_team(tmp_path, testers=0, reports={"worker1": LONG},
                           report_max_chars=0)
    await team.start()
    t1 = team.dispatch("worker1", "build it")
    await team.wait([t1.id])
    assert team.task_report(t1).endswith(LONG.strip())
    await team.shutdown()


async def test_compaction_never_runs_in_the_middle_of_a_task(tmp_path):
    """Compaction is only for between tasks: with a task running it is skipped (also
    when checked again once it holds the agent); that task's own end decides."""
    gate = asyncio.Event()

    async def slow(s: FakeSession, prompt: str) -> AsyncIterator[Event]:
        if prompt == "second":
            await gate.wait()
        yield s.done(f"did {prompt}", cost=0.01)

    cfg = load_config(tmp_path, {"workers": 1, "testers": 0, "compact_at": OVER})
    backend = fake_backend(slow, speed=0)
    team = Team(cfg, backend, lambda e: None)
    await team.start()
    session = backend.sessions["worker1"]  # type: ignore[attr-defined]
    runner = team.runners["worker1"]
    await team.wait([team.dispatch("worker1", "first").id])
    await until(lambda: session.compactions and not runner.compacting)
    t2 = team.dispatch("worker1", "second")
    await until(lambda: "second" in session.prompts)
    runner.usage.context_tokens = 10**6  # far over the threshold, mid-task
    assert await team._compact_if_needed("worker1") is False
    # The check made once the compaction holds the agent skips it too.
    gate.set()
    await team.wait([t2.id])
    await until(lambda: not runner.compacting and len(session.compactions) == 2)
    assert await runner.compact("Keep: x", lambda: False) is False
    assert [p.split()[0] for p in session.prompts] == ["first", "/compact", "second",
                                                        "/compact"]
    await team.shutdown()
