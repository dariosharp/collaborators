import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from collaborators.backend_fake import FakeSession, fake_backend
from collaborators.config import load_config
from collaborators.events import Event, Notice, TaskStatus, TurnResult
from collaborators.orchestrator import Team
from collaborators.state import StateStore


def make_team(tmp_path: Path, brain=None, workers=2, testers=1, **kw) -> tuple[Team, list, dict]:
    cfg = load_config(tmp_path, {"workers": workers, "testers": testers})
    events: list[Event] = []
    backend = fake_backend(brain, speed=0) if brain else fake_backend(speed=0)
    team = Team(cfg, backend, events.append, **kw)
    return team, events, backend.sessions  # type: ignore[attr-defined]


async def settle(team: Team, rounds: int = 400) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)
        lead = team.runners["lead"]
        if (not lead.busy and team._lead_inbox.empty()
                and all(t.status.finished for t in team.tasks.values())):
            return
    raise AssertionError("team did not settle")


def gated_brain(gates: dict[str, asyncio.Event], files: dict[str, list[str]] | None = None):
    """Workers write `files[agent]` and then block until their gate opens."""

    async def brain(s: FakeSession, prompt: str) -> AsyncIterator[Event]:
        if s.spec.role == "lead":
            yield s.done("ok")
            return
        for f in (files or {}).get(s.name, []):
            async for e in s.use("Edit", {"file_path": f, "old_string": "a",
                                          "new_string": "b"}, "ok", 0):
                yield e
        gate = gates.get(s.name)
        if gate:
            await gate.wait()
        yield s.done(f"{s.name} finished: {prompt}", cost=0.5)

    return brain


async def test_demo_round_trip(tmp_path):
    team, events, sessions = make_team(tmp_path, workers=1)
    await team.start()
    team.tell_lead("add --dry-run")
    await settle(team)
    t1, t2 = team.tasks["T1"], team.tasks["T2"]
    assert (t1.agent, t1.status) == ("worker1", TaskStatus.DONE)
    assert t1.touched_files == ["scripts/import_data.py", "tests/test_import_data.py"]
    assert (t2.agent, t2.verdict) == ("tester1", "PASS")
    # Both reports were collected through wait_tasks: no unsolicited "[team]" prompt.
    assert sessions["lead"].prompts == ["add --dry-run"]
    assert sum(e.cost_usd for e in events if isinstance(e, TurnResult)) == pytest.approx(0.81)
    await team.shutdown()


async def test_wait_any_returns_first_finished(tmp_path):
    gates = {"worker1": asyncio.Event(), "worker2": asyncio.Event()}
    team, _, _ = make_team(tmp_path, gated_brain(gates))
    await team.start()
    a = team.dispatch("worker1", "A")
    b = team.dispatch("worker2", "B")
    gates["worker2"].set()
    done = await team.wait([a.id, b.id], mode="any", timeout=5)
    assert {r.id: r.status for r in done} == {a.id: TaskStatus.RUNNING, b.id: TaskStatus.DONE}
    gates["worker1"].set()
    done = await team.wait([a.id], timeout=5)
    assert done[0].status is TaskStatus.DONE and "worker1 finished: A" in done[0].result
    await team.shutdown()


async def test_wait_timeout_leaves_task_running(tmp_path):
    gates = {"worker1": asyncio.Event()}
    team, _, _ = make_team(tmp_path, gated_brain(gates))
    await team.start()
    t = team.dispatch("worker1", "slow")
    [rec] = await team.wait([t.id], timeout=0.01)
    assert rec.status is TaskStatus.RUNNING
    assert "so far" in team.task_report(rec)
    gates["worker1"].set()
    await settle(team)
    await team.shutdown()


async def test_busy_agent_and_unknown_agent_are_rejected(tmp_path):
    gates = {"worker1": asyncio.Event()}
    team, _, _ = make_team(tmp_path, gated_brain(gates))
    await team.start()
    team.dispatch("worker", "first")  # alias for worker1
    with pytest.raises(ValueError, match="busy"):
        team.dispatch("worker1", "second")
    with pytest.raises(ValueError, match="unknown agent"):
        team.dispatch("worker9", "x")
    with pytest.raises(ValueError, match="itself"):
        team.dispatch("lead", "x")
    gates["worker1"].set()
    await settle(team)
    await team.shutdown()


async def test_planned_file_conflict_blocks_dispatch(tmp_path):
    gates = {"worker1": asyncio.Event()}
    team, _, _ = make_team(tmp_path, gated_brain(gates))
    await team.start()
    team.dispatch("worker1", "A", files=["src/app.py"])
    with pytest.raises(ValueError, match="src/app.py"):
        team.dispatch("worker2", "B", files=["./src/app.py"])
    team.dispatch("worker2", "C", files=["src/other.py"])
    gates["worker1"].set()
    await settle(team)
    await team.shutdown()


async def test_overlapping_writes_raise_a_warning(tmp_path):
    gates = {"worker1": asyncio.Event(), "worker2": asyncio.Event()}
    files = {"worker1": ["shared.py"], "worker2": [str(tmp_path / "shared.py")]}
    team, events, _ = make_team(tmp_path, gated_brain(gates, files))
    await team.start()
    a = team.dispatch("worker1", "A")
    for _ in range(20):
        await asyncio.sleep(0)
    b = team.dispatch("worker2", "B")
    for _ in range(20):
        await asyncio.sleep(0)
    assert a.touched_files == b.touched_files == ["shared.py"]
    assert a.warnings and a.warnings == b.warnings
    assert any(isinstance(e, Notice) and e.level == "warning" for e in events)
    gates["worker1"].set()
    gates["worker2"].set()
    await settle(team)
    await team.shutdown()


async def test_unwaited_task_is_reported_to_the_lead(tmp_path):
    team, _, sessions = make_team(tmp_path, gated_brain({}))
    await team.start()
    t = team.dispatch("worker1", "fire and forget")
    await settle(team)
    for _ in range(50):  # the lead inbox runs the report
        await asyncio.sleep(0)
    await settle(team)
    assert any(p.startswith("[team]") and t.id in p for p in sessions["lead"].prompts)
    await team.shutdown()


async def test_user_direct_message_is_not_reported(tmp_path):
    team, _, sessions = make_team(tmp_path, gated_brain({}))
    await team.start()
    team.dispatch("worker1", "hi", origin="user")
    await settle(team)
    assert sessions["lead"].prompts == []
    await team.shutdown()


async def test_cancel_interrupts(tmp_path):
    gates = {"worker1": asyncio.Event()}
    team, _, sessions = make_team(tmp_path, gated_brain(gates))
    await team.start()
    t = team.dispatch("worker1", "long")
    await asyncio.sleep(0)
    await team.cancel(t.id)
    gates["worker1"].set()  # the fake brain notices the interrupt on its next event
    await settle(team)
    assert t.status is TaskStatus.CANCELLED
    await team.shutdown()


async def test_lead_tools_end_to_end(tmp_path):
    team, _, _ = make_team(tmp_path, gated_brain({}))
    await team.start()
    tools = {t.name: t.handler for t in team.runners["lead"].spec.tools}
    out = await tools["dispatch_task"]({"agent": "worker2", "task": "X"})
    assert out.startswith("T1 started on worker2")
    out = await tools["wait_tasks"]({"task_ids": ["t1"]})  # ids are case-insensitive
    assert "T1 · worker2 · done" in out and "worker2 finished: X" in out
    assert (await tools["dispatch_task"]({"agent": "nobody", "task": "X"})).startswith("ERROR")
    assert "worker1" in await tools["team_status"]({})
    await team.shutdown()


async def test_sessions_and_task_ids_survive_restart(tmp_path):
    team, _, _ = make_team(tmp_path, gated_brain({}))
    await team.start()
    team.dispatch("worker1", "one")
    await settle(team)
    ids = {n: r.spec.session_id for n, r in team.runners.items()}
    await team.shutdown()

    again, _, _ = make_team(tmp_path, gated_brain({}), resume=True)
    assert {n: r.spec.session_id for n, r in again.runners.items()} == ids
    assert all(r.spec.resume for r in again.runners.values())
    assert again.resumed == sorted(ids)
    await again.start()
    assert again.dispatch("worker1", "two").id == "T2"
    await settle(again)
    await again.shutdown()

    fresh, _, _ = make_team(tmp_path, gated_brain({}))
    assert not any(r.spec.resume for r in fresh.runners.values())
    assert StateStore(tmp_path / ".collab").last_task_number() == 2


async def test_added_agent_is_announced_to_an_idle_lead(tmp_path):
    team, events, sessions = make_team(tmp_path, gated_brain({}), workers=1, testers=0)
    await team.start()
    assert await team.add_agent("worker") == "worker2"
    assert await team.add_agent("tester") == "tester1"
    await settle(team)
    for _ in range(50):
        await asyncio.sleep(0)
    await settle(team)
    told = "\n".join(sessions["lead"].prompts)
    assert "worker2" in told and "tester1" in told and "joined the team" in told
    assert team.agent_names == ["lead", "worker1", "worker2", "tester1"]
    assert any(type(e).__name__ == "AgentAdded" for e in events)
    # usable right away
    team.dispatch("worker2", "go")
    await settle(team)
    await team.shutdown()


async def test_wait_tasks_returns_early_when_an_agent_joins(tmp_path):
    gates = {"worker1": asyncio.Event()}
    outputs: list[str] = []
    workers = gated_brain(gates)

    async def brain(s: FakeSession, prompt: str) -> AsyncIterator[Event]:
        if s.spec.role != "lead":
            async for e in workers(s, prompt):
                yield e
            return
        if prompt == "go":  # the Lead dispatches, then blocks in wait_tasks
            async for e in s.call_team_tool("dispatch_task", {"agent": "worker1", "task": "x"}):
                yield e
            async for e in s.call_team_tool("wait_tasks", {"task_ids": ["T1"]}):
                yield e
            outputs.append(s.last_tool_output)
        yield s.done("ok")

    team, _, sessions = make_team(tmp_path, brain, workers=1)
    await team.start()
    team.tell_lead("go")
    for _ in range(50):
        await asyncio.sleep(0)
    assert team.runners["lead"].busy and not outputs  # stuck waiting on T1
    await team.add_agent("worker")
    for _ in range(50):
        await asyncio.sleep(0)
    [out] = outputs
    assert "T1 · worker1 · running" in out
    assert "[team] worker2" in out and "worker2 [" in out  # news + fresh team status
    gates["worker1"].set()
    await settle(team)
    for _ in range(50):
        await asyncio.sleep(0)
    await settle(team)
    # delivered inside the tool result, so no separate "joined" message afterwards
    assert not any("joined" in p for p in sessions["lead"].prompts)
    await team.shutdown()


async def test_resume_brings_back_agents_added_at_runtime(tmp_path):
    team, _, _ = make_team(tmp_path, gated_brain({}), workers=1, testers=1)
    await team.start()
    await team.add_agent("worker")
    await team.add_agent("worker")
    ids = {n: r.spec.session_id for n, r in team.runners.items()}
    await team.shutdown()
    again, _, _ = make_team(tmp_path, gated_brain({}), workers=1, testers=1, resume=True)
    assert again.agent_names == ["lead", "worker1", "tester1", "worker2", "worker3"]
    assert {n: r.spec.session_id for n, r in again.runners.items()} == ids
    assert "worker3" in again.runners["lead"].spec.system_append


async def test_remove_idle_agent_tells_the_lead(tmp_path):
    team, events, sessions = make_team(tmp_path, gated_brain({}), workers=2, testers=1)
    await team.start()
    assert await team.remove_agent("worker2") == "worker2"
    await settle(team)
    for _ in range(50):
        await asyncio.sleep(0)
    await settle(team)
    assert team.agent_names == ["lead", "worker1", "tester1"]
    assert any(type(e).__name__ == "AgentRemoved" and e.agent == "worker2" for e in events)
    told = "\n".join(sessions["lead"].prompts)
    assert "worker2 was removed" in told
    with pytest.raises(ValueError, match="unknown agent"):
        team.dispatch("worker2", "go")
    with pytest.raises(ValueError):
        await team.remove_agent("lead")
    # the saved roster no longer has it, so --resume won't bring it back
    assert "worker2" not in StateStore(team.cfg.state_dir).load_sessions()
    # a re-added worker2 starts a fresh conversation
    old = sessions["worker2"].spec.session_id
    assert await team.add_agent("worker") == "worker2"
    assert team.runners["worker2"].spec.session_id != old
    assert not team.runners["worker2"].spec.resume
    await settle(team)
    await team.shutdown()


async def test_remove_busy_agent_cancels_its_task(tmp_path):
    gates = {"worker1": asyncio.Event()}
    team, _, sessions = make_team(tmp_path, gated_brain(gates), workers=1, testers=0)
    await team.start()
    task = team.dispatch("worker1", "long job")
    for _ in range(20):
        await asyncio.sleep(0)
    await team.remove_agent("worker1")
    assert task.status is TaskStatus.CANCELLED
    await settle(team)
    for _ in range(50):
        await asyncio.sleep(0)
    await settle(team)
    told = "\n".join(sessions["lead"].prompts)
    assert "worker1 was removed" in told and "T1 was cancelled" in told
    # reported once, through the announcement, not again as a finished task
    assert "A task finished while you were not waiting" not in told
    await team.shutdown()
