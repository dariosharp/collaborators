# collaborators

Run a team of **three Claude Code agents at once — and watch all of them live.**

You talk to a single **Lead**. The Lead turns your request into a precise task,
sends it to a **Worker**, then has a **Tester** verify the result — each in its
own pane of one terminal window, so you see exactly what every agent is doing in
real time.

```
+----------------------+------------------+
|                      |     WORKER       |
|   LEAD  (you type)   +------------------+
|                      |     TESTER       |
+----------------------+------------------+
```

## Why

Claude Code's built-in subagents run *inside* the Lead: you get the final result
but never see the worker think or type. This project instead runs three real
`claude` processes side by side, so the orchestration is fully visible — the Lead
drives the others and you observe the whole loop.

## Requirements

- the **[Claude Code](https://claude.com/claude-code) CLI** (`claude`) on your `PATH`
- `tmux` **or** GNU `screen`
- `git` or `curl` (for installing)

> **Claude only.** This tool orchestrates the Claude Code CLI specifically — it
> launches and drives `claude` processes and is not compatible with other AI
> agents or CLIs.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/dariosharp/collaborators/main/install.sh | sh
```

(`bash` works too.) The installer asks whether the team should use **tmux** or
**screen** by default, downloads the tool, and installs a `start-team` command on
your PATH. Non-interactive? Pass the choice up front:

```sh
curl -fsSL https://raw.githubusercontent.com/dariosharp/collaborators/main/install.sh | sh -s -- --screen
# or: TEAM_BACKEND=screen curl -fsSL .../install.sh | sh
```

The default backend is saved to `~/.config/claude-team/config`; edit it (or
re-run the installer) to change it later.

## Quick start

```bash
cd /path/to/your/project      # the team works in whatever dir you launch from
start-team                    # uses the backend you chose at install
start-team --screen           # override the default just this once
start-team --dir=/some/path   # explicit working directory
```

Three panes open, a `claude` starts in each, and the Lead loads its playbook.
When the Lead prints `READY`, just describe what you want, e.g.:

> Add a `--dry-run` flag to the import script and make sure it doesn't write files.

The Lead writes a task for the Worker (you see it start), waits for it to finish,
then dispatches the Tester to verify — and reports back to you.

## How it works

| Piece | Role |
|-------|------|
| `team/start-team.sh` | Builds the 3-pane layout and launches a `claude` in each (tmux or screen). |
| `team/team.sh`       | The Lead's remote control: `send` / `sendf` / `read` / `wait` / `status`. Auto-detects the backend. |
| `team/ORCHESTRATION.md` | The Lead's playbook — the dispatch → wait → verify loop. |

The Lead sends prompts with tmux `send-keys` / screen `stuff` and reads output
back with tmux `capture-pane` / screen `hardcopy`. Each task ends with a
symbol-free done-sentinel (`WORKER_TASK_DONE` / `TESTER_TASK_DONE`) so the Lead
knows when an agent has finished. All three agents share one working directory —
by default the directory you launch from (override with `--dir=PATH` or
`TEAM_WORKDIR`).

## Options

| Flag / env | Effect |
|------------|--------|
| `-s`, `--screen`, `--terminal=screen` | Use GNU screen instead of tmux. |
| `-t`, `--tmux`, `--terminal=tmux` | Force tmux (default). |
| `--dir=PATH` / `TEAM_WORKDIR=PATH` | Working directory for all three agents. |
| `AUTO_APPROVE=0` | Make the Worker/Tester ask for permissions like the Lead. |
| `AGENT_CMD="claude --model …"` | Custom command for the Worker/Tester. |

> **Note:** by default the Worker and Tester start with
> `--dangerously-skip-permissions` so unattended automation doesn't freeze on a
> permission prompt. They edit files in your working directory — only run this on
> code you're comfortable having them change.

## Controls

**tmux** — switch panes `Ctrl-b`+arrow · detach `Ctrl-b d` · re-attach `tmux attach -t team` · kill `tmux kill-session -t team`

**screen** — switch regions `Ctrl-a Tab` · detach `Ctrl-a d` · re-attach `screen -r team` · kill `screen -S team -X quit`

## More

See [`team/README.md`](team/README.md) for full usage and
[`team/ORCHESTRATION.md`](team/ORCHESTRATION.md) for the Lead's playbook.
