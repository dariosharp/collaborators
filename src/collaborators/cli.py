"""`collab` command line."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, accounts
from .config import EFFORTS, PERMISSION_MODES, ConfigError, load_config

DEMO_PROMPT = "Add a --dry-run flag to scripts/import_data.py and make sure it never writes files"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="collab",
        description="A Lead, Workers and Testers made of Claude agents, live in your terminal.",
        epilog="Accounts: `collab account login NAME` adds one with its own claude.ai "
               "login; `collab account list` shows them.",
    )
    p.add_argument("-d", "--dir", type=Path, default=None,
                   help="project directory the agents work in (default: current directory)")
    p.add_argument("-w", "--workers", type=int, default=None, help="number of workers (default 1)")
    p.add_argument("-t", "--testers", type=int, default=None, help="number of testers (default 1)")
    p.add_argument("-r", "--resume", action="store_true",
                   help="reopen every agent's previous conversation in this project")
    p.add_argument("--ask", action="store_true", default=None,
                   help="ask in the UI before agents run tools (default: full auto)")
    p.add_argument("--permission-mode", choices=PERMISSION_MODES, default=None,
                   help="Claude Code permission mode for all agents (default bypassPermissions)")
    for role in ("lead", "worker", "tester"):
        p.add_argument(f"--{role}-model", default=None, metavar="MODEL",
                       help=f"model for the {role} ('default': Claude Code's own choice)")
        p.add_argument(f"--{role}-effort", choices=EFFORTS, default=None,
                       help=f"reasoning effort for the {role}")
    p.add_argument("--worktrees", action="store_true", default=None,
                   help="give each worker its own git worktree; the Lead merges verified work")
    p.add_argument("--no-notify", dest="notify", action="store_false", default=None,
                   help="no desktop notification when the Lead finishes a long turn")
    p.add_argument("--remote-control", action="store_true",
                   help="make the Lead controllable from claude.ai/code and the Claude app")
    p.add_argument("--account", default=None, metavar="NAME",
                   help="Claude account to run the agents under (see /switch and `collab account`; "
                        "default: the last one used)")
    p.add_argument("--demo", action="store_true",
                   help="scripted demo: no API calls, no cost, runs in a temp directory")
    p.add_argument("--version", action="version", version=f"collaborators {__version__}")
    return p


def account_main(argv: list[str]) -> int:
    """`collab account …`: manage the Claude accounts outside the UI."""
    p = argparse.ArgumentParser(prog="collab account",
                                description="Claude accounts the agents can run under.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the accounts and how each signs in")
    login = sub.add_parser("login", help="add (or re-add) an account with its own claude.ai "
                                         "login, which supports Remote Control")
    login.add_argument("name", help="the account's name, e.g. personal")
    args = p.parse_args(argv)

    if args.command == "list":
        current = accounts.current()
        for name in accounts.names():
            how = accounts.kind(name)
            if how == "claude.ai" and not accounts.is_logged_in(accounts.login_dir(name)):
                how += " (logged out)"
            print(f"{'*' if name == current else ' '} {name:<16} {how}")
        return 0

    try:
        name = accounts.check_new_name(args.name.strip().lower())
        path = accounts.prepare_login_dir(accounts.login_dir(name))
    except (ConfigError, OSError) as exc:
        print(f"collab: {exc}", file=sys.stderr)
        return 2
    print(f"Logging the account {name!r} in with claude.ai (config dir {path}).")
    try:
        subprocess.run(accounts.login_command(), env=accounts.claude_env(path), check=False)
    except OSError as exc:
        print(f"collab: cannot run claude ({exc}); run `{accounts.login_hint(path)}` "
              f"yourself, then `collab account login {name}` again", file=sys.stderr)
        return 1
    try:
        who = accounts.check_login(name)
    except ConfigError as exc:
        print(f"collab: {exc}", file=sys.stderr)
        return 1
    print(f"Account {name!r} added ({who}). Use it with `/switch {name}` in collab, "
          f"or `collab --account {name}`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["account"]:
        return account_main(argv[1:])
    args = build_parser().parse_args(argv)

    workdir = args.dir or Path.cwd()
    if args.demo:
        workdir = Path(tempfile.mkdtemp(prefix="collab-demo-"))
    if not workdir.is_dir():
        print(f"collab: directory not found: {workdir}", file=sys.stderr)
        return 2

    overrides = {
        "workers": args.workers, "testers": args.testers,
        "permission_mode": args.permission_mode, "ask": args.ask,
        "worktrees": args.worktrees, "notify": args.notify,
        **{role: {"model": getattr(args, f"{role}_model"),
                  "effort": getattr(args, f"{role}_effort")}
           for role in ("lead", "worker", "tester")},
    }
    try:
        cfg = load_config(workdir, overrides)
    except ConfigError as exc:
        print(f"collab: {exc}", file=sys.stderr)
        return 2

    account = None
    if args.account:
        try:
            accounts.env_for(args.account.strip().lower())
            account = args.account.strip().lower()
            accounts.set_current(account)
        except ConfigError as exc:
            print(f"collab: {exc}", file=sys.stderr)
            return 2

    if args.demo:
        from .backend_fake import fake_backend
        backend = fake_backend()
        initial, label = DEMO_PROMPT, "demo"
    else:
        try:
            from .backend_sdk import sdk_backend
        except ImportError as exc:
            print(f"collab: the Claude Agent SDK is not available ({exc}).\n"
                  "Install it with:  pip install claude-agent-sdk", file=sys.stderr)
            return 1
        backend, initial, label = sdk_backend, "", ""

    from .ui.app import CollabApp
    CollabApp(cfg, backend, resume=args.resume, initial_prompt=initial,
              mode_label=label, remote_control=args.remote_control, account=account).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
