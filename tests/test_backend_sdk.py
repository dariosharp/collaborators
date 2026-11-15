"""The SDK session's reader, driven by a stand-in for ClaudeSDKClient (no CLI, no API)."""

import asyncio
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, UserMessage

from collaborators.agents import AgentRunner, AgentSpec
from collaborators.backend_sdk import SdkSession
from collaborators.events import AgentStatus, Notice, StatusChanged, TextDone, TurnResult


def result(text: str, cost: float, origin=None) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=1, session_id="s", total_cost_usd=cost, result=text,
                         usage={}, origin=origin)


def say(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


class FakeClient:
    """Answers each query with a scripted turn; `inject` plays a turn nobody asked for."""

    def __init__(self) -> None:
        self.out: asyncio.Queue = asyncio.Queue()
        self.queries: list[str] = []
        self.cost = 0.0

    async def receive_messages(self):
        while True:
            yield await self.out.get()

    def _turn(self, text: str, origin=None, prompt=None) -> None:
        self.cost += 1.0
        if prompt is not None:
            self.out.put_nowait(UserMessage(content=prompt, origin=origin))
        self.out.put_nowait(say(text))
        self.out.put_nowait(result(text, self.cost, origin))

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)
        self._turn(f"answer to {prompt}")

    def inject(self, text: str, origin=None, prompt=None) -> None:
        self._turn(text, origin, prompt)

    async def interrupt(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass


def make(tmp_path: Path):
    spec = AgentSpec(name="lead", role="lead", model="m", effort="medium", cwd=tmp_path,
                     system_append="", session_id="s")
    session = SdkSession(spec)
    client = FakeClient()
    session._client = client  # type: ignore[assignment]
    events: list = []
    runner = AgentRunner(spec, session, events.append)
    session._pump_task = asyncio.create_task(session._pump())
    return session, client, runner, events


async def settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


async def test_turn_started_elsewhere_is_shown_and_does_not_steal_our_answer(tmp_path):
    session, client, runner, events = make(tmp_path)
    # someone writes to the Lead from claude.ai while collab is idle
    client.inject("hi from the web", prompt="what's up?")
    await settle()
    assert any(isinstance(e, Notice) and "claude.ai › what's up?" in e.text for e in events)
    assert any(isinstance(e, TextDone) and e.text == "hi from the web" for e in events)
    statuses = [e.status for e in events if isinstance(e, StatusChanged)]
    assert statuses[-2:] == [AgentStatus.RUNNING, AgentStatus.IDLE]
    assert runner.total_cost == 1.0

    # our own turn afterwards gets its own answer, with its own cost
    got = await runner.run("plan it")
    assert got.text == "answer to plan it" and got.cost_usd == 1.0
    await session.close()


async def test_injected_turn_mid_run_is_not_taken_as_our_result(tmp_path):
    session, client, runner, events = make(tmp_path)

    async def query(prompt: str) -> None:  # a background-task turn lands first
        client.queries.append(prompt)
        client.inject("background task finished", origin={"kind": "task-notification"})
        client._turn(f"answer to {prompt}")

    client.query = query  # type: ignore[method-assign]
    got = await runner.run("do it")
    assert got.text == "answer to do it"
    results = [e for e in events if isinstance(e, TurnResult)]
    assert [r.text for r in results] == ["background task finished", "answer to do it"]
    await session.close()


async def test_our_prompt_waits_for_a_turn_started_elsewhere(tmp_path):
    session, client, runner, _ = make(tmp_path)
    client.out.put_nowait(UserMessage(content="long remote job"))  # turn starts, no result yet
    await settle()
    assert session.remote_turn
    run = asyncio.create_task(runner.run("next"))
    await settle()
    assert client.queries == []  # held back until the remote turn ends
    client.out.put_nowait(result("remote done", 1.0))
    got = await asyncio.wait_for(run, 5)
    assert client.queries == ["next"] and got.text == "answer to next"
    await session.close()
