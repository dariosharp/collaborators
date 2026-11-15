"""`collab` command line."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import __version__
from .config import EFFORTS, PERMISSION_MODES, ConfigError, load_config

DEMO_PROMPT = "Add a --dry-run flag to scripts/import_data.py and make sure it never writes files"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="collab",
        description="A Lead, Workers and Testers made of Claude agents, live in your terminal.",
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
                       help=f"model for the {role}")
        p.add_argument(f"--{role}-effort", choices=EFFORTS, default=None,
                       help=f"reasoning effort for the {role}")
    p.add_argument("--demo", action="store_true",
                   help="scripted demo: no API calls, no cost, runs in a temp directory")
    p.add_argument("--version", action="version", version=f"collaborators {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
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
        **{role: {"model": getattr(args, f"{role}_model"),
                  "effort": getattr(args, f"{role}_effort")}
           for role in ("lead", "worker", "tester")},
    }
    try:
        cfg = load_config(workdir, overrides)
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
              mode_label=label).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
