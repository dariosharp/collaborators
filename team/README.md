# Claude Team (Lead / Worker / Tester in tmux)

Watch three Claude Code agents at once. You talk to the **Lead**; it dispatches
work to the **Worker** and verification to the **Tester**, and you see all three
live in one tmux window.

```
+----------------------+------------------+
|                      |     WORKER       |
|   LEAD  (you type)   +------------------+
|                      |     TESTER       |
+----------------------+------------------+
```

## Requirements

- the **[Claude Code](https://claude.com/claude-code) CLI** (`claude`) on your PATH
- `tmux` **or** GNU `screen` installed
- `git` or `curl` (for installing)

> **Claude only.** This tool launches and drives `claude` processes — it is not
> compatible with other AI agents or CLIs.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/dariosharp/collaborators/main/install.sh | sh
```

(`bash` works too.) The installer asks whether to use **tmux** or **screen** by
default and puts a `start-team` command on your PATH. Then just:

```bash
cd /path/to/your/project
start-team                        # uses the backend you chose at install
```

## Start (from a checkout, without installing)

```bash
./team/start-team.sh              # tmux (default)
./team/start-team.sh -s           # GNU screen  (also: --screen, --terminal=screen)
./team/start-team.sh --tmux       # force tmux  (also: --terminal=tmux)
```

`team.sh` auto-detects which backend is running from the saved state file, so
you use the same `send`/`read`/`wait` commands either way.

### Working directory

All three agents work in the **directory you launch from** by default — so
`cd /my/project && /path/to/team/start-team.sh` puts the whole team in
`/my/project`. Override it explicitly with:

```bash
./team/start-team.sh --dir=/some/project     # or: TEAM_WORKDIR=/some/project ./team/start-team.sh
```

(The team scripts themselves stay where they live; only the agents' working
directory changes.)

This opens the tmux session `team`, launches a Claude in each pane, and hands
the Lead its playbook (`team/ORCHESTRATION.md`). When the Lead prints `READY`,
just tell it what you want built — e.g.:

> Add a `--dry-run` flag to the import script and make sure it doesn't write files.

The Lead will write a task for the Worker, you'll see the Worker start, then the
Tester verify.

## Controls

**tmux**
- Switch panes: `Ctrl-b` then an arrow key.
- Detach (leave it running): `Ctrl-b` then `d`. Re-attach: `tmux attach -t team`.
- Kill the whole team: `tmux kill-session -t team`.

**screen**
- Switch regions: `Ctrl-a` then `Tab`.
- Detach: `Ctrl-a` then `d`. Re-attach: `screen -r team`.
- Kill the whole team: `screen -S team -X quit`.
- Note: `screen` needs a version with vertical splits (4.1+, i.e. anything
  modern). The 3-region layout is applied right after attach.

## Notes / knobs

- **Multiple workers/testers:** `start-team --workers 2 --testers 1` (or
  `TEAM_WORKERS` / `TEAM_TESTERS`). Agents are addressed as `worker1`, `worker2`,
  `tester1`, … The Lead parallelizes only across tasks that touch **different
  files** (all agents share one directory). tmux auto-tiles the pool; screen is
  best-effort and gets cramped with many agents.
- **Auto-approve:** the Worker and Tester start with
  `--dangerously-skip-permissions` so unattended automation doesn't freeze on
  permission prompts. They run in the same directory as your project, so only use
  this on code you're OK with them changing. To make them prompt like the Lead:
  `AUTO_APPROVE=0 ./team/start-team.sh`.
- **Different working dir for agents:** `TEAM_WORKDIR=/path ./team/start-team.sh`.
- **Custom claude command:** `AGENT_CMD="claude --model sonnet-5" ./team/start-team.sh`.
- The Lead controls the others via `team/team.sh` (see comments in that file).
