#!/usr/bin/env bash
# start-team.sh — spin up a Lead / Worker / Tester Claude team in one terminal
# multiplexer window (tmux by default, or GNU screen).
#
# Layout:
#   +----------------+----------------+
#   |                |    WORKER      |
#   |     LEAD       +----------------+
#   |                |    TESTER      |
#   +----------------+----------------+
#
# All three agents work in the SAME directory — by default the directory you
# launch this script from (so `cd /some/project && .../start-team.sh` puts the
# whole team in /some/project). Override with --dir=PATH or TEAM_WORKDIR.
# You interact only with the LEAD; it dispatches to the Worker/Tester.
#
# Usage:
#   cd /my/project && /path/to/team/start-team.sh        # team works in /my/project (tmux)
#   ./start-team.sh -s                                   # use GNU screen
#   ./start-team.sh --screen | --tmux | --terminal=screen
#   ./start-team.sh --dir=/some/project                  # explicit working dir

set -euo pipefail

# ---- where things are -----------------------------------------------------
INVOKE_DIR="$PWD"                                    # where you launched from
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" # where the team scripts live
STATE_DIR="$HERE/.team"
PANES_ENV="$STATE_DIR/panes.env"

# ---- config (env-overridable) ---------------------------------------------
SESSION="${TEAM_SESSION:-team}"
BACKEND="${TEAM_BACKEND:-tmux}"          # tmux | screen  (overridden by flags)
WORKDIR="${TEAM_WORKDIR:-$INVOKE_DIR}"   # where the agents operate (default: PWD)

# The Lead is interactive (you talk to it) → normal permission prompts.
# Worker/Tester run unattended → skip prompts by default, else automation
# freezes on the first prompt. AUTO_APPROVE=0 makes them ask like the Lead.
AUTO_APPROVE="${AUTO_APPROVE:-1}"
LEAD_CMD="${LEAD_CMD:-claude}"
if [[ "$AUTO_APPROVE" == "1" ]]; then
  AGENT_CMD="${AGENT_CMD:-claude --dangerously-skip-permissions}"
else
  AGENT_CMD="${AGENT_CMD:-claude}"
fi
# ---------------------------------------------------------------------------

# ---- parse flags ----------------------------------------------------------
for arg in "$@"; do
  case "$arg" in
    -s|--screen)    BACKEND=screen ;;
    -t|--tmux)      BACKEND=tmux ;;
    --terminal=*)   BACKEND="${arg#*=}" ;;
    --dir=*)        WORKDIR="${arg#*=}" ;;
    -h|--help)      sed -n '2,30p' "$HERE/start-team.sh"; exit 0 ;;
    *) echo "unknown option: $arg (try -h)" >&2; exit 1 ;;
  esac
done

case "$BACKEND" in
  tmux|screen) ;;
  *) echo "unknown backend: '$BACKEND' (use tmux or screen)" >&2; exit 1 ;;
esac
command -v "$BACKEND" >/dev/null 2>&1 || { echo "'$BACKEND' is not installed or not on PATH." >&2; exit 1; }

# Normalize the working dir to an absolute path and make sure it exists.
_req_workdir="$WORKDIR"
WORKDIR="$(cd "$WORKDIR" 2>/dev/null && pwd)" || { echo "working dir does not exist: ${_req_workdir}" >&2; exit 1; }
QWORKDIR="$(printf '%q' "$WORKDIR")"

mkdir -p "$STATE_DIR/tasks"

# One-line bootstrap for the Lead (single line: newlines would submit early).
BOOTSTRAP="You are the LEAD orchestrator of a 3-agent team; all three of you work in the directory $WORKDIR. Read your playbook at $HERE/ORCHESTRATION.md now, then use $HERE/team.sh to dispatch tasks to the Worker and Tester (running in the other panes) and put any task files under $STATE_DIR/tasks/. After reading, reply READY and wait for my instructions."

echo "backend=$BACKEND  workdir=$WORKDIR"

# ===========================================================================
# tmux backend  (panes get -c "$WORKDIR", so all three start in WORKDIR)
# ===========================================================================
start_tmux() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' already exists. Attaching."
    echo "(Run 'tmux kill-session -t $SESSION' first for a fresh team.)"
    exec tmux attach -t "$SESSION"
  fi

  tmux new-session -d -s "$SESSION" -n team -c "$WORKDIR"
  local lead worker tester
  lead="$(tmux display-message -p -t "$SESSION:team" '#{pane_id}')"
  tmux split-window -h -t "$lead" -c "$WORKDIR"
  worker="$(tmux display-message -p '#{pane_id}')"
  tmux split-window -v -t "$worker" -c "$WORKDIR"
  tester="$(tmux display-message -p '#{pane_id}')"

  tmux set -t "$SESSION" pane-border-status top
  tmux set -t "$SESSION" pane-border-format ' #{pane_title} '
  tmux select-pane -t "$lead"   -T "LEAD  (you talk here)"
  tmux select-pane -t "$worker" -T "WORKER"
  tmux select-pane -t "$tester" -T "TESTER"
  tmux resize-pane -t "$lead" -x 55% || true

  cat > "$PANES_ENV" <<EOF
TEAM_BACKEND=tmux
TEAM_SESSION=$SESSION
TEAM_WORKDIR=$WORKDIR
LEAD_PANE=$lead
WORKER_PANE=$worker
TESTER_PANE=$tester
EOF
  echo "Wrote pane map -> $PANES_ENV"

  tmux send-keys -t "$worker" "$AGENT_CMD" Enter
  tmux send-keys -t "$tester" "$AGENT_CMD" Enter
  tmux send-keys -t "$lead"   "$LEAD_CMD"  Enter

  sleep 6
  tmux send-keys -t "$lead" -l "$BOOTSTRAP"
  tmux send-keys -t "$lead" Enter

  tmux select-pane -t "$lead"
  exec tmux attach -t "$SESSION"
}

# ===========================================================================
# screen backend
#   Windows: 0=lead, 1=worker, 2=tester (targeted via `screen -p <n>`).
#   IMPORTANT: screen's `chdir` only affects windows created afterward, and
#   window 0 is born in the launch dir — so we don't trust chdir. Instead we
#   `cd "$WORKDIR"` inside every window before starting its CLI. That is what
#   guarantees all three agents share the working directory.
# ===========================================================================
start_screen() {
  if screen -ls 2>/dev/null | grep -qE "[0-9]+\.${SESSION}[[:space:]]"; then
    echo "Session '$SESSION' already exists. Attaching."
    echo "(Run 'screen -S $SESSION -X quit' first for a fresh team.)"
    exec screen -r "$SESSION"
  fi

  screen -dmS "$SESSION"
  screen -S "$SESSION" -p 0 -X title lead
  screen -S "$SESSION" -X screen -t worker
  screen -S "$SESSION" -X screen -t tester

  cat > "$PANES_ENV" <<EOF
TEAM_BACKEND=screen
TEAM_SESSION=$SESSION
TEAM_WORKDIR=$WORKDIR
LEAD_PANE=0
WORKER_PANE=1
TESTER_PANE=2
EOF
  echo "Wrote pane map -> $PANES_ENV"

  # cd every window into WORKDIR, then launch its CLI.
  screen_send_raw 1 "cd $QWORKDIR && $AGENT_CMD"
  screen_send_raw 2 "cd $QWORKDIR && $AGENT_CMD"
  screen_send_raw 0 "cd $QWORKDIR && $LEAD_CMD"

  screen -S "$SESSION" -X caption always '%{= kw}[%n %t]%{-}'

  # After attach (display exists) split into 3 regions, then once the Lead CLI
  # has booted, stuff its playbook. Runs detached so `exec screen -r` can hand
  # you the terminal.
  (
    sleep 2
    screen -S "$SESSION" -X eval \
      'select 0' 'split -v' 'focus right' 'select 1' \
      'split' 'focus down' 'select 2' 'focus left' 2>/dev/null || true
    sleep 5
    screen_send_raw 0 "$BOOTSTRAP"
  ) &

  exec screen -r "$SESSION"
}

# stuff <win> <text>  — send text + Enter to a screen window
screen_send_raw() {
  local win="$1"; shift
  screen -S "$SESSION" -p "$win" -X stuff "$*"
  screen -S "$SESSION" -p "$win" -X stuff $'\r'
}

case "$BACKEND" in
  tmux)   start_tmux ;;
  screen) start_screen ;;
esac
