"""A scripted backend: no API calls, no cost.

Used by the test-suite and by `collab --demo`, which plays a realistic
Lead -> Worker -> Tester round so the UI can be seen (and screenshotted)
without spending anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from .agents import AgentSpec, Command
from .events import (
    Event,
    LimitReached,
    TextDelta,
    TextDone,
    Thinking,
    ToolCall,
    ToolResult,
    TurnResult,
)

# A prompt containing this plays a turn cut off by the account's usage limit.
LIMIT_MARK = "[demo:limit]"

# A brain turns (session, prompt) into a stream of events. It must end with a TurnResult.
Brain = Callable[["FakeSession", str], AsyncIterator[Event]]


DEMO_COMMANDS = [
    Command("code-review", "Review the current diff for correctness bugs", "[low|medium|high]"),
    Command("simplify", "Review the changed code for reuse and simplification"),
    Command("compact", "Clear history but keep a summary in context", "[instructions]"),
    Command("context", "Show what is using the context window"),
    Command("model", "Set the AI model for this session", "[model]"),
    Command("security-review", "Security review of the pending changes"),
    Command("init", "Initialize a CLAUDE.md file with codebase documentation"),
]


class FakeSession:
    def __init__(self, spec: AgentSpec, brain: Brain, speed: float = 1.0) -> None:
        self.spec = spec
        self.session_id = spec.session_id
        self.brain = brain
        self.speed = speed
        self.prompts: list[str] = []
        self.connected = False
        self._interrupted = asyncio.Event()
        self._n = 0
        self.last_tool_output = ""
        self.commands: list[Command] = list(DEMO_COMMANDS)
        self.remote_url: str | None = None

    @property
    def name(self) -> str:
        return self.spec.name

    async def connect(self) -> None:
        await self.pause(0.3)
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def interrupt(self) -> None:
        self._interrupted.set()

    async def set_remote_control(self, enabled: bool, name: str | None = None) -> str | None:
        await self.pause(0.2)
        self.remote_url = "https://claude.ai/code/session_demo (demo: not a real link)" \
            if enabled else None
        return self.remote_url

    async def pause(self, seconds: float) -> None:
        if self.speed > 0:
            await asyncio.sleep(seconds / self.speed)
        else:
            await asyncio.sleep(0)

    def tool_id(self) -> str:
        self._n += 1
        return f"{self.name}-tool-{self._n}"

    async def run(self, prompt: str) -> AsyncIterator[Event]:
        self.prompts.append(prompt)
        self._interrupted.clear()
        async for event in self.brain(self, prompt):
            if self._interrupted.is_set():
                yield TurnResult(self.name, "Interrupted by user.", is_error=True,
                                 subtype="error_during_execution")
                return
            yield event

    # ---- helpers for brains -------------------------------------------------
    async def say(self, text: str, chunk: int = 6) -> AsyncIterator[Event]:
        for i in range(0, len(text), chunk):
            yield TextDelta(self.name, text[i:i + chunk])
            await self.pause(0.02)
        yield TextDone(self.name, text)

    async def use(self, name: str, input: dict[str, Any], output: str,
                  seconds: float = 0.6, is_error: bool = False) -> AsyncIterator[Event]:
        tid = self.tool_id()
        yield ToolCall(self.name, tid, name, input)
        await self.pause(seconds)
        yield ToolResult(self.name, tid, output, is_error)

    async def call_team_tool(self, name: str, args: dict[str, Any]) -> AsyncIterator[Event]:
        """Invoke one of the Lead's in-process tools, as the SDK would."""
        spec = next(t for t in self.spec.tools if t.name == name)
        tid = self.tool_id()
        yield ToolCall(self.name, tid, f"mcp__team__{name}", args)
        out = await spec.handler(args)
        self.last_tool_output = out
        yield ToolResult(self.name, tid, out, out.startswith("ERROR"))

    async def hit_limit(self) -> AsyncIterator[Event]:
        yield LimitReached(self.name, None, "five_hour")
        text = "You've hit your limit (demo)."
        yield TextDone(self.name, text)
        yield TurnResult(self.name, text, is_error=True, subtype="error", limited=True,
                         session_id=self.session_id)

    def done(self, text: str, cost: float = 0.0, turns: int = 1) -> TurnResult:
        return TurnResult(self.name, text, cost_usd=cost, num_turns=turns,
                          duration_ms=0, input_tokens=1200 * turns, output_tokens=300 * turns,
                          session_id=self.session_id)


# ---- the demo script ----------------------------------------------------------

async def demo_brain(s: FakeSession, prompt: str) -> AsyncIterator[Event]:
    if LIMIT_MARK in prompt and not s.spec.env:  # another account (an env) goes on
        async for e in s.hit_limit():
            yield e
        return
    if prompt.startswith("/model"):
        arg = prompt.removeprefix("/model").strip()
        text = (f"Set model to `{arg}` for this session only" if arg else
                f"Current model: `{s.spec.model}` (effort: {s.spec.effort})")
        async for e in s.say(text):
            yield e
        yield s.done(text, cost=0.0)
        return
    if prompt.startswith("/"):
        name = prompt.split()[0]
        async for e in s.say(f"**{name}** would run here. In the demo nothing is executed; "
                             "with real agents every Claude Code command and skill works."):
            yield e
        yield s.done("ok", cost=0.0)
        return
    role = s.spec.role
    if role == "lead":
        async for e in _demo_lead(s, prompt):
            yield e
    elif role == "tester":
        async for e in _demo_tester(s, prompt):
            yield e
    else:
        async for e in _demo_worker(s, prompt):
            yield e


async def _demo_lead(s: FakeSession, prompt: str) -> AsyncIterator[Event]:
    if prompt.startswith("[from the Lead of team"):
        sender = prompt.split("team ", 1)[1].split("]", 1)[0]
        text = f"(demo) Noted the message from team {sender}; a real Lead would act on it."
        async for e in s.say(text):
            yield e
        yield s.done(text, cost=0.01)
        return
    if prompt.startswith("[team]"):
        async for e in s.say("Noted — that result is already folded into the summary above."):
            yield e
        yield s.done("ok", cost=0.01)
        return
    yield Thinking(s.name, "Small, well-scoped change: one worker, then one tester.")
    async for e in s.use("Grep", {"pattern": "argparse", "path": "."},
                         "scripts/import_data.py:12: parser = argparse.ArgumentParser()"):
        yield e
    async for e in s.say("I'll have **worker1** add the flag, then **tester1** verify that "
                         "nothing is written in dry-run mode."):
        yield e
    task = ("Add a `--dry-run` flag to scripts/import_data.py. With the flag, the script must "
            "parse and validate the input and print what it would write, but must not create "
            "or modify any file. Add tests in tests/test_import_data.py.")
    async for e in s.call_team_tool("dispatch_task", {
            "agent": "worker1", "task": task,
            "files": ["scripts/import_data.py", "tests/test_import_data.py"]}):
        yield e
    tid = s.last_tool_output.split()[0]
    async for e in _wait_until_done(s, tid):
        yield e
    async for e in s.call_team_tool("dispatch_task", {
            "agent": "tester1",
            "task": "Verify the new --dry-run flag in scripts/import_data.py: run the test "
                    "suite, then run the script with --dry-run on tests/fixtures/sample.csv "
                    "and confirm no file is created or modified."}):
        yield e
    tid2 = s.last_tool_output.split()[0]
    async for e in _wait_until_done(s, tid2):
        yield e
    summary = ("Done. `--dry-run` is in place:\n\n"
               "- **scripts/import_data.py** — new flag; writes are skipped and listed instead\n"
               "- **tests/test_import_data.py** — 3 new tests\n\n"
               "tester1: **PASS** — 15 tests green, and a dry run left the output dir untouched.")
    async for e in s.say(summary, chunk=10):
        yield e
    yield s.done(summary, cost=0.18, turns=7)


async def _wait_until_done(s: FakeSession, task_id: str) -> AsyncIterator[Event]:
    """wait_tasks returns early when an agent joins; a real Lead waits again."""
    while True:
        async for e in s.call_team_tool("wait_tasks", {"task_ids": [task_id]}):
            yield e
        if f"{task_id} · " in s.last_tool_output and " · running" not in \
                s.last_tool_output.split("\n", 1)[0]:
            return
        if "\nWhile you were waiting" in s.last_tool_output:
            said = s.last_tool_output.split("\nWhile you were waiting", 1)[1]
            said = said.split("\n\n")[2].strip()
            said = said if len(said) <= 60 else said[:59] + "…"
            async for e in s.say(f"(demo) You said: “{said}”. A real Lead "
                                 f"would answer that here. {task_id} is still running; "
                                 "back to waiting."):
                yield e
        elif "[team]" in s.last_tool_output:
            async for e in s.say("The team changed; nothing to hand over yet, so I'll keep "
                                 "waiting."):
                yield e


async def _demo_worker(s: FakeSession, prompt: str) -> AsyncIterator[Event]:
    yield Thinking(s.name, "Read the script first, then add the flag where args are parsed.")
    async for e in s.use("Read", {"file_path": "scripts/import_data.py"}, "1  import argparse\n…"):
        yield e
    async for e in s.use("Grep", {"pattern": "open\\(.*'w'", "path": "scripts"},
                         "scripts/import_data.py:48\nscripts/import_data.py:71"):
        yield e
    async for e in s.say("Writes happen in two places; I'll route both through one helper "
                         "that respects `--dry-run`."):
        yield e
    async for e in s.use("Edit", {
            "file_path": "scripts/import_data.py",
            "old_string": "    parser.add_argument(\"input\")\n",
            "new_string": "    parser.add_argument(\"input\")\n    parser.add_argument(\n"
                          "        \"--dry-run\", action=\"store_true\",\n"
                          "        help=\"validate and report, but write nothing\")\n"},
            "The file scripts/import_data.py has been updated.", 1.0):
        yield e
    async for e in s.use("Edit", {
            "file_path": "scripts/import_data.py",
            "old_string": "    with open(path, 'w') as fh:\n        fh.write(data)\n",
            "new_string": "    if args.dry_run:\n        print(f\"would write {path}\")\n"
                          "        return\n    with open(path, 'w') as fh:\n"
                          "        fh.write(data)\n"},
            "The file scripts/import_data.py has been updated.", 0.8):
        yield e
    async for e in s.use("Write", {"file_path": "tests/test_import_data.py",
                                   "content": "def test_dry_run_writes_nothing(tmp_path): ...\n"},
                         "File created successfully.", 0.8):
        yield e
    async for e in s.use("Bash", {"command": "python -m pytest -q tests/test_import_data.py",
                                  "description": "Run the import tests"},
                         "...\n3 passed in 0.41s", 1.6):
        yield e
    report = ("Added `--dry-run` to scripts/import_data.py; both write paths go through "
              "`_write()`, which prints `would write <path>` and returns in dry-run mode.\n"
              "Tests: tests/test_import_data.py (3 new, passing).\n"
              "Verify: `python scripts/import_data.py --dry-run tests/fixtures/sample.csv`.")
    async for e in s.say(report, chunk=12):
        yield e
    yield s.done(report, cost=0.42, turns=9)


async def _demo_tester(s: FakeSession, prompt: str) -> AsyncIterator[Event]:
    async for e in s.use("Bash", {"command": "git diff --stat"},
                         " scripts/import_data.py   | 14 +++++++---\n"
                         " tests/test_import_data.py | 31 +++++++++++++\n", 0.6):
        yield e
    async for e in s.use("Bash", {"command": "python -m pytest -q"}, "15 passed in 1.92s", 2.0):
        yield e
    async for e in s.use("Bash", {"command": "find out -newer /tmp/mark | wc -l; python "
                                  "scripts/import_data.py --dry-run tests/fixtures/sample.csv"},
                         "would write out/sample.json\n0", 1.2):
        yield e
    report = ("VERDICT: PASS\n\nFull suite: 15 passed. A dry run printed `would write "
              "out/sample.json` and created or modified no file (checked with find -newer).")
    async for e in s.say(report, chunk=12):
        yield e
    yield s.done(report, cost=0.21, turns=5)


def fake_backend(brain: Brain = demo_brain, speed: float = 1.0):
    """Backend factory; keeps every session it creates in `.sessions` for tests."""
    sessions: dict[str, FakeSession] = {}

    def factory(spec: AgentSpec) -> FakeSession:
        key = spec.name if spec.team == "main" else f"{spec.team}/{spec.name}"
        sessions[key] = FakeSession(spec, brain, speed)
        return sessions[key]

    factory.sessions = sessions  # type: ignore[attr-defined]
    return factory
