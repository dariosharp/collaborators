"""Where agents work, and how their changes are tracked, undone and merged.

Without git, everyone works in the project directory and nothing is tracked.
With git, every task is bracketed by two snapshots, so its changes can be shown and
undone. With `worktrees` on, each worker also gets its own git worktree: workers
never step on each other, and the Lead merges a worker's work into the main
directory once it has been verified.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path

from .config import TeamConfig
from .gitsnap import Tree, find_top, git

# One lock per repository: merges and undos into the main directory never interleave,
# whichever team (tab) starts them.
_LOCKS: dict[Path, asyncio.Lock] = {}


@dataclass
class MergeOutcome:
    files: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    before: str = ""
    after: str = ""


class Workspace:
    def __init__(self, cfg: TeamConfig) -> None:
        self.cfg = cfg
        self.top = find_top(cfg.workdir)
        self.main = Tree(self.top) if self.top else None
        self.isolated = bool(cfg.worktrees and self.top)
        # where the project directory sits inside the repository
        self.rel = cfg.workdir.resolve().relative_to(self.top.resolve()) if self.top else Path()

    @property
    def tracked(self) -> bool:
        return self.main is not None

    @property
    def lock(self) -> asyncio.Lock:
        assert self.top is not None
        return _LOCKS.setdefault(self.top, asyncio.Lock())

    def ref(self, *parts: str) -> str:
        return "refs/collab/" + "/".join((self.cfg.team, *parts))

    # ---- worker worktrees -------------------------------------------------------
    def isolates(self, agent: str) -> bool:
        return self.isolated and self.cfg.role_of(agent) == "worker"

    def worktree_dir(self, agent: str) -> Path:
        return self.cfg.project_dir / "worktrees" / self.cfg.team / agent

    def cwd_for(self, agent: str) -> Path:
        if self.isolates(agent):
            return self.worktree_dir(agent) / self.rel
        return self.cfg.workdir

    def where(self, agent: str) -> str:
        """The working tree an agent's changes land in (absolute path)."""
        if self.isolates(agent):
            return str(self.worktree_dir(agent))
        return str(self.top or self.cfg.workdir)

    def tree_for(self, agent: str) -> Tree | None:
        if self.isolates(agent):
            return Tree(self.worktree_dir(agent))
        return self.main

    async def ensure_worktree(self, agent: str) -> None:
        """Create the worker's worktree from the main directory as it is now
        (kept across restarts, with any work not merged yet)."""
        assert self.main is not None and self.top is not None
        wt = self.worktree_dir(agent)
        if (wt / ".git").exists():
            if not await self._base(agent):
                await git(self.top, "update-ref", self.ref(agent, "base"),
                          await Tree(wt).snapshot())
            return
        base = await self.main.snapshot()
        wt.parent.mkdir(parents=True, exist_ok=True)
        await git(self.top, "worktree", "prune")
        await git(self.top, "worktree", "add", "--detach", "--force", str(wt), base)
        await git(self.top, "update-ref", self.ref(agent, "base"), base)

    async def _base(self, agent: str) -> str:
        assert self.top is not None
        run = await git(self.top, "rev-parse", "--verify", "-q", self.ref(agent, "base"),
                        check=False)
        return run.text.strip() if run.code == 0 else ""

    async def pending(self, agent: str) -> list[str]:
        """Files the worker changed that are not in the main directory yet."""
        base = await self._base(agent)
        if not base:
            return []
        wt = Tree(self.worktree_dir(agent))
        return await wt.changed(base, await wt.snapshot())

    async def prepare(self, agent: str) -> None:
        """Before a worker's task: if everything it did is merged, start it from the
        main directory as it is now; otherwise let it keep building on its work."""
        assert self.top is not None and self.main is not None
        await self.ensure_worktree(agent)
        if await self.pending(agent):
            return
        wt = self.worktree_dir(agent)
        fresh = await self.main.snapshot()
        await git(wt, "checkout", "-q", "-f", "--detach", fresh)
        await git(wt, "clean", "-fdq")
        await git(self.top, "update-ref", self.ref(agent, "base"), fresh)

    async def merge(self, agent: str) -> MergeOutcome:
        """Bring a worker's changes into the main directory. If the main directory
        moved on meanwhile, its changes go into the worktree first (a three-way merge
        per file); conflicts are left there, with markers, for the worker to fix."""
        assert self.top is not None and self.main is not None
        async with self.lock:
            wt = Tree(self.worktree_dir(agent))
            base = await self._base(agent)
            cur = await wt.snapshot()
            mine = await wt.changed(base, cur)
            if not mine:
                return MergeOutcome()
            main_now = await self.main.snapshot()
            theirs = await wt.changed(base, main_now)
            if theirs:
                conflicts = []
                for path in theirs:
                    if path in mine:
                        data, clean = await wt.merge_file(base, main_now, cur, path)
                        if not clean:
                            conflicts.append(path)
                    else:
                        data = await wt.read(main_now, path)
                    await wt.write(path, data)
                await git(self.top, "update-ref", self.ref(agent, "base"), main_now)
                if conflicts:
                    return MergeOutcome(conflicts=conflicts)
                cur = await wt.snapshot()
                mine = await wt.changed(main_now, cur)
            await self.main.put(cur, mine)
            after = await self.main.snapshot()
            # The worktree now matches the main directory: nothing pending.
            await git(self.top, "update-ref", self.ref(agent, "base"), cur)
            return MergeOutcome(files=mine, before=main_now, after=after)

    async def discard(self, agent: str) -> list[str]:
        """Throw away the worker's changes that were not merged."""
        files = await self.pending(agent)
        if files:
            await Tree(self.worktree_dir(agent)).put(await self._base(agent), files)
        return files

    async def drop_worktree(self, agent: str) -> bool:
        """Remove the worker's worktree, unless it holds work not merged yet."""
        if not self.isolates(agent) or self.top is None:
            return True
        wt = self.worktree_dir(agent)
        if not (wt / ".git").exists():
            return True
        if await self.pending(agent):
            return False
        with contextlib.suppress(Exception):
            await git(self.top, "worktree", "remove", "--force", str(wt))
            await git(self.top, "update-ref", "-d", self.ref(agent, "base"))
        return True
