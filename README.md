# collaborators

**A team of Claude agents — a Lead, Workers and Testers — orchestrated live in one terminal UI.**

You talk to the **Lead**. It turns your request into precise tasks, dispatches them
to **Workers**, has **Testers** verify the result, and reports back. Every agent's
work — its reasoning, each file it reads or edits, each command it runs — streams
live into its own card.

![collaborators](docs/screenshot.png)

## Why

Claude Code's built-in subagents run *inside* one session: you get the result, not
the process. collaborators runs every agent as its own Claude Code session and shows
them side by side, so the whole loop — plan, implement, verify, fix — is visible and
steerable.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/dariosharp/collaborators/main/install.sh | sh
```

The installer uses [uv](https://docs.astral.sh/uv/) or pipx when available, or a
private virtualenv otherwise. You can also install it directly:

```sh
uv tool install git+https://github.com/dariosharp/collaborators
```

From a clone, `./install.sh` installs that checkout instead (re-run it after pulling
or changing the code).

Requirements: Python 3.11+, and a [Claude Code](https://claude.com/claude-code) login
(the agents authenticate the same way Claude Code does).

## Quick start

```sh
collab --demo            # a scripted run: no API calls, no cost — look around first
cd /path/to/project
collab                   # the team works in the current directory
```

> Running inside byobu? It takes the function keys: press `Shift+F12` to let F2/F3
> through, or add agents from the command palette (`Ctrl+P`).

Then describe what you want, e.g.

> Add a `--dry-run` flag to the import script and make sure it never writes files.

## Keys

| Key | Action |
|---|---|
| `Enter` | send your message to the Lead / open the focused agent |
| `Ctrl+J` or `\` `Enter` | **new line** in your message (also `Shift+Enter` in terminals that report it: kitty, WezTerm, Ghostty, foot, iTerm2) |
| `/` | **Claude Code slash commands and skills** (yours and the project's), with completion: `↑`/`↓` choose, `Tab` completes — to the Lead, or to one agent from its transcript |
| `/command ` | **its possible values**, suggested as you type: the accounts for `/switch`, the models for `/model`, the levels for `/effort` and `/code-review`, `on`/`off`… — `Enter` picks and sends |
| `/switch` | **switch Claude account** (or `/switch <name>`): every agent carries on with its conversation — see [Several accounts](#several-accounts) |
| `/remote-control` | **continue with the Lead from claude.ai/code or the Claude app**; `/remote-control off` stops it (or start with `collab --remote-control`); not on setup-token accounts |
| `F2` / `F3` | **add a worker / a tester** — the Lead is told immediately and can use it at once |
| `Delete` | **remove the focused worker or tester** (after a confirmation; a running task is cancelled) |
| `Ctrl+N` | **open another team in a new tab** (its own Lead, workers and testers), e.g. one for the frontend and one for the backend |
| `Ctrl+G` | next team's tab (or click it); close a tab from `Ctrl+P` |
| `Tab` | move between the Lead and the agent cards |
| `Ctrl+O` | full transcript of the focused agent: diffs, command output, and a direct line to it |
| `Ctrl+T` | task board: every task, status, verdict, cost, files changed and report — **`d` shows a task's diff, `u` undoes it** |
| `Ctrl+X` | interrupt the focused agent |
| `Ctrl+P` | command palette (add or remove agents, task board, themes — dark and light) |
| `F1` | help |
| `Esc` | **vim normal mode**: `:` commands, `Ctrl+W` windows, `gt`/`gT` tabs — see [Vim mode](#vim-mode) |
| `Ctrl+C` | clear the input; press twice to quit (`Ctrl+Q` works too) — `collab --resume` brings every conversation back |

## Vim mode

Press `Esc` in the Lead's input for vim's **normal mode** (`-- NORMAL --` at the
bottom); `i`, `a`, `o` or `Enter` go back to typing. Every other shortcut keeps working.

| Keys | Action |
|---|---|
| `:` | command line (`↑`/`↓` history, `Esc` cancels) |
| `Ctrl+W s` / `Ctrl+W v` | add a worker / a tester ("split") |
| `Ctrl+W q` | close the selected agent (on the Lead: the tab) |
| `Ctrl+W w` / `W`, `h` `j` `k` `l` | move between the Lead and the agent cards |
| `j` / `k`, `Ctrl+D` / `Ctrl+U`, `gg` / `G` | scroll the selected log |
| `gt` / `gT` | next / previous tab |

| Command | Action |
|---|---|
| `:sp`, `:worker [n]` · `:vs`, `:tester [n]` | add workers · testers |
| `:q` | close the selected agent; on the Lead close the tab, or quit if it is the last |
| `:rm worker2` | remove an agent |
| `:qa` | quit collab |
| `:tabnew [name]`, `:tabc [name]`, `:tabn`, `:tabp` | open, close and switch teams (tabs) |
| `:e worker1` | full transcript of an agent |
| `:stop [agent]`, `:tasks`, `:switch [account]`, `:remote [off]`, `:h` | interrupt, task board, account, Remote Control, help |

Add `!` (`:q!`, `:qa!`, `:tabc!`) to skip the confirmation.

## How it works

Each agent is a headless Claude Code session driven through the
[Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk). The Lead coordinates
through real tools rather than by typing into terminals:

| Lead tool | What it does |
|---|---|
| `dispatch_task(agent, task, files?)` | start a task on an idle worker or tester; returns a task id |
| `wait_tasks(ids, mode="all"\|"any")` | wait for tasks and get each report, files modified, cost, warnings |
| `team_status()` | who is idle or busy, with which task |
| `cancel_task(id)` | interrupt a task |
| `message_lead(team, message)` | write to the Lead of another team (tab) in the same directory |
| `merge_work(agent)` / `discard_work(agent)` | with `--worktrees`: bring a worker's verified work into the main directory, or drop it |

- **Completion is exact.** A task is done when its session returns its result. No
  screen scraping.
- **The team stays in sync.** When you add or remove an agent, the Lead learns about
  it straight away. If it is blocked in `wait_tasks`, the call returns early with the news, so the
  new agent can be used right away.
- **Parallel work is guarded.** Workers share the directory, so the Lead declares the
  files a task will touch. A task that would overlap a running one is refused, and if
  two tasks do write the same file anyway, both reports carry a warning.
- **You can talk while it waits.** A message sent while the Lead is in `wait_tasks`
  ends the wait: the Lead answers, the agents keep working, and each report reaches
  the Lead by itself when it is ready.
- **Several teams, one directory.** Each tab is a team with its own Lead. The file
  checks span every team, so one team cannot start work on files another is
  changing. Leads can write to each other with `message_lead`: one-way messages,
  shown in both tabs, a few at a time until you speak to that Lead again.
- **Every task can be undone.** In a git repository, collab takes a snapshot of the
  files before and after each task (with a private index: your branch, index and
  history are never touched). The task board shows exactly what a task changed and
  puts the files back on `u`; if they changed again since, it asks first. The Lead
  is told.
- **Workers can have their own worktree** (`--worktrees`, or `worktrees = true`).
  Each worker gets a git worktree under `.collab/worktrees/`, so parallel workers
  never collide. Testers verify a worker's change in its worktree, then the Lead
  calls `merge_work`: the change lands in your directory as ordinary uncommitted
  edits (a three-way merge if the directory moved on; conflicts go back to the
  worker). A merge can be undone from the task board like any task.
- **It tells you when it's done.** When the Lead finishes something that took a
  while (30 s or more) or an agent needs your approval, collab rings the bell and
  posts a desktop notification (iTerm2, WezTerm, kitty, Windows Terminal). Inside
  tmux add `set -g allow-passthrough on` to `~/.tmux.conf`; without it the bell
  still gets through (iTerm2: *Profiles → Terminal → Notifications*). Turn it off
  with `--no-notify` or `notify = false`.
- **Skills work everywhere.** Every agent loads your user and project settings and
  skills: they use skills on their own, and you can run any `/command` yourself.
- **The Lead cannot edit code.** It is denied the editing tools: it plans, delegates
  and verifies.
- **Testers report a verdict.** Each one starts its report with `VERDICT: PASS` or
  `FAIL`, which shows on its card and on the task board.
- **State lives in the project.** Session ids and task history are kept in
  `.collab/`, which ignores itself in git, so `--resume` brings everyone back,
  including agents you added at runtime (and not the ones you removed), and every
  tab that was open. Extra teams live in `.collab/teams/<name>/`; open a team with
  a saved name to pick it up again.

## Configuration

Defaults:

| Role | Model | Effort |
|---|---|---|
| Lead | `claude-fable-5-2` | medium |
| Worker | `claude-opus-5-5` | medium |
| Tester | `claude-sonnet-5-5` | medium |

Override them in `~/.config/collaborators/config.toml` (for every project) or
`<project>/.collab/config.toml` (for one project). Command-line flags win over both.

```toml
workers = 2
testers = 1
permission_mode = "bypassPermissions"   # or acceptEdits, default, auto
ask = false                              # true: approve tool use from the UI
worktrees = false                        # true: each worker in its own git worktree
notify = true                            # desktop notification when a long turn ends

[worker]
model = "claude-opus-5-5"
effort = "high"
```

```sh
collab --workers 3 --testers 2 --tester-effort high --dir ~/code/shop
collab --ask          # agents ask before running tools; approve with y / a / n
collab --resume       # reopen every agent's previous conversation here
collab --worktrees    # each worker in its own git worktree; the Lead merges
collab --remote-control   # reach the Lead from claude.ai/code or your phone too
```

> **Permissions:** by default the agents run unattended (`bypassPermissions`) and can
> edit files and run commands in the project directory. Use it on code you are
> comfortable letting them change, or run with `--ask`.

## Several accounts

If you have more than one Claude account, collab can run the agents under any of
them and switch between them while they work: every agent keeps its conversation.
An account signs in one of two ways:

- **claude.ai login** (recommended): the account gets its own Claude Code config
  dir, `~/.config/collaborators/accounts/<name>/`, with its own login in it.
  Conversations, settings, skills, plugins and history are symlinks to
  `~/.claude`, and MCP servers and trusted projects are copied over from
  `~/.claude.json` on every switch. **Remote Control works.**
- **setup-token**: a long-lived token from `claude setup-token`. Inference only:
  **no Remote Control.**

To add one (say `personal`):

1. In collab type `/switch add` (or `/switch`, then `a`), name it `personal`, and
   pick **log in with claude.ai**. collab steps aside and runs
   `claude auth login --claudeai` in the terminal: sign in with that account in the
   browser, and collab comes back and checks the login. From a plain terminal,
   `collab account login personal` does the same (`collab account list` shows the
   accounts).
2. `/switch personal` (or `/switch` and Enter) moves every agent of every tab to it.
   Idle agents switch at once, busy ones when their turn ends; each resumes the
   same conversation.

Re-adding an existing name replaces how it signs in: that is how a setup-token
account becomes a claude.ai login one. For a token instead, pick **paste a token**
and paste what `claude setup-token` prints; it is kept in
`~/.config/collaborators/accounts.json`, readable only by you.

`login` is always there: Claude Code's own login. When an account hits its usage
limit, collab offers to switch to another one (`y`), and tells the Lead which tasks
stopped so it can pick them up again. The account in use shows in the top bar, is
remembered for next time, and can be chosen at start with `collab --account work`.
Switching to a setup-token account turns Remote Control off (collab says so);
between `login` and claude.ai login accounts it stays on.

> If `ANTHROPIC_API_KEY` is set in your environment, Claude Code uses it before any
> account token: unset it to use your accounts.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q && .venv/bin/ruff check src tests
.venv/bin/collab --demo
```

The test suite drives the orchestrator and the Textual UI through a scripted backend
(`backend_fake.py`), so it runs offline and costs nothing.
