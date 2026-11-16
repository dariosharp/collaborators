"""Git snapshots of a working tree, without touching the user's branch, index or history.

A snapshot is a commit object built from a private copy of the index, so it records
the files exactly as they are on disk (tracked or not, minus ignored ones). Nothing
points at it except refs under refs/collab/, which keep it from being garbage
collected. Snapshots make it possible to show what a task changed, to undo it, and to
move work between a worker's worktree and the main directory.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# commit-tree needs an identity; the user may not have one configured.
IDENTITY = {
    "GIT_AUTHOR_NAME": "collab", "GIT_AUTHOR_EMAIL": "collab@localhost",
    "GIT_COMMITTER_NAME": "collab", "GIT_COMMITTER_EMAIL": "collab@localhost",
}


class GitError(RuntimeError):
    pass


@dataclass
class Run:
    code: int
    out: bytes
    err: str

    @property
    def text(self) -> str:
        return self.out.decode(errors="replace")


async def git(cwd: Path, *args: str, env: dict[str, str] | None = None,
              check: bool = True) -> Run:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})})
    out, err = await proc.communicate()
    run = Run(proc.returncode or 0, out, err.decode(errors="replace").strip())
    if check and run.code != 0:
        raise GitError(f"git {args[0]} failed: {run.err or run.code}")
    return run


def find_top(path: Path) -> Path | None:
    """The top of the git working tree that contains `path`, if any."""
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


class Tree:
    """One working tree: the main checkout, or a worker's worktree."""

    def __init__(self, top: Path) -> None:
        self.top = top

    async def snapshot(self, ref: str | None = None) -> str:
        index = Path((await git(self.top, "rev-parse", "--path-format=absolute",
                                "--git-path", "index")).text.strip())
        with tempfile.TemporaryDirectory(prefix="collab-index-") as tmp:
            private = Path(tmp) / "index"
            if index.exists():  # start from the real index: unchanged files are not rehashed
                shutil.copyfile(index, private)
            env = {"GIT_INDEX_FILE": str(private)}
            await git(self.top, "add", "-A", "--", ".", env=env)
            tree = (await git(self.top, "write-tree", env=env)).text.strip()
        commit = (await git(self.top, "commit-tree", tree, "-m", "collab snapshot",
                            env=IDENTITY)).text.strip()
        if ref:
            await git(self.top, "update-ref", ref, commit)
        return commit

    async def changed(self, a: str, b: str) -> list[str]:
        run = await git(self.top, "diff", "--name-only", "--no-renames", a, b)
        return [line for line in run.text.splitlines() if line]

    async def diff(self, a: str, b: str, paths: list[str] | None = None) -> str:
        run = await git(self.top, "diff", "--no-renames", "--no-color", a, b, "--",
                        *(paths or []))
        return run.text

    async def files_in(self, commit: str, paths: list[str]) -> set[str]:
        if not paths:
            return set()
        run = await git(self.top, "ls-tree", "-r", "--name-only", commit, "--", *paths)
        return set(run.text.splitlines())

    async def read(self, commit: str, path: str) -> bytes | None:
        run = await git(self.top, "cat-file", "blob", f"{commit}:{path}", check=False)
        return run.out if run.code == 0 else None

    async def put(self, commit: str, paths: list[str]) -> None:
        """Make `paths` on disk match `commit`: restore them, or delete the ones it
        doesn't have. Only the working tree changes; the index is left alone."""
        present = await self.files_in(commit, paths)
        keep = sorted(present)
        for i in range(0, len(keep), 200):
            await git(self.top, "restore", f"--source={commit}", "--worktree", "--",
                      *keep[i:i + 200])
        for path in paths:
            if path not in present:
                target = self.top / path
                if target.is_file() or target.is_symlink():
                    target.unlink()

    async def merge_file(self, base: str, ours: str, theirs: str, path: str) -> \
            tuple[bytes | None, bool]:
        """Three-way merge of one file. Returns (content or None if deleted, clean?)."""
        b = await self.read(base, path)
        o = await self.read(ours, path)
        t = await self.read(theirs, path)
        if o == t:
            return o, True
        if o == b:
            return t, True
        if t == b:
            return o, True
        if o is None or t is None or b"\0" in (o or b"") + (t or b"") + (b or b""):
            return o, False  # deleted on one side, or binary: no line merge
        with tempfile.TemporaryDirectory(prefix="collab-merge-") as tmp:
            files = []
            for name, data in (("ours", o), ("base", b or b""), ("theirs", t)):
                f = Path(tmp) / name
                f.write_bytes(data)
                files.append(str(f))
            run = await git(self.top, "merge-file", "-p", "-L", "main directory", "-L",
                            "base", "-L", "worker", *files, check=False)
        if run.code < 0:
            raise GitError(f"git merge-file failed on {path}: {run.err}")
        return run.out, run.code == 0

    async def write(self, path: str, data: bytes | None) -> None:
        target = self.top / path
        if data is None:
            if target.is_file() or target.is_symlink():
                target.unlink()
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
