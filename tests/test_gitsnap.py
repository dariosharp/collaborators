"""Snapshots, restore and three-way merge on a real (temporary) git repository."""

import subprocess
from pathlib import Path

from collaborators.gitsnap import Tree, find_top


def repo(path: Path) -> Tree:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "a.txt").write_text("one\ntwo\nthree\n")
    subprocess.run(["git", "-C", str(path), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "init"], check=True)
    return Tree(find_top(path))


def git_state(path: Path) -> tuple[str, str, str]:
    def run(*a: str) -> str:
        return subprocess.run(["git", "-C", str(path), *a], capture_output=True,
                              text=True).stdout
    return run("rev-parse", "HEAD"), run("status", "--porcelain"), run("diff", "--cached")


async def test_snapshot_and_put_leave_the_users_git_alone(tmp_path):
    t = repo(tmp_path)
    (tmp_path / "a.txt").write_text("one\nTWO\nthree\n")  # an uncommitted change of the user
    before_state = git_state(tmp_path)
    before = await t.snapshot("refs/collab/test/before")
    (tmp_path / "a.txt").write_text("changed by the task\n")
    (tmp_path / "new.txt").write_text("new\n")
    after = await t.snapshot()
    assert await t.changed(before, after) == ["a.txt", "new.txt"]
    assert "+changed by the task" in await t.diff(before, after)
    await t.put(before, ["a.txt", "new.txt"])  # undo
    assert (tmp_path / "a.txt").read_text() == "one\nTWO\nthree\n"
    assert not (tmp_path / "new.txt").exists()
    assert git_state(tmp_path) == before_state  # HEAD, status and index unchanged


async def test_merge_file(tmp_path):
    t = repo(tmp_path)
    base = await t.snapshot()
    (tmp_path / "a.txt").write_text("ONE\ntwo\nthree\n")
    ours = await t.snapshot()
    (tmp_path / "a.txt").write_text("one\ntwo\nTHREE\n")
    theirs = await t.snapshot()
    data, clean = await t.merge_file(base, ours, theirs, "a.txt")
    assert clean and data == b"ONE\ntwo\nTHREE\n"
    (tmp_path / "a.txt").write_text("one\ntwo\nthree, said the worker\n")
    clash = await t.snapshot()
    (tmp_path / "a.txt").write_text("one\ntwo\nthree, said main\n")
    main = await t.snapshot()
    data, clean = await t.merge_file(base, main, clash, "a.txt")
    assert not clean and b"<<<<<<< main directory" in data
