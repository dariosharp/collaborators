#!/usr/bin/env bash
# start-team.sh — spin up a Lead + a pool of Workers and Testers (Claude agents)
# in one terminal-multiplexer window (tmux by default, or GNU screen).
#
# Layout (tmux, e.g. --workers 2 --testers 1):
#   +----------------+----------------+
#   |                |   Worker 1     |
#   |                +----------------+
#   |     Lead       |   Worker 2     |
#   |                +----------------+
#   |                |   Tester 1     |
#   +----------------+----------------+
#
# All agents work in the SAME directory — by default the directory you launch
# from (override with --dir=PATH or TEAM_WORKDIR). You interact only with the
# Lead; it dispatches to the Workers/Testers. Multiple workers only run in
# parallel safely on tasks that touch DIFFERENT files.
#
# Usage:
#   cd /my/project && /path/to/team/start-team.sh          # 1 worker, 1 tester (tmux)
#   ./start-team.sh --workers 2 --testers 1                # a pool
#   ./start-team.sh -s                                     # GNU screen
#   ./start-team.sh --screen | --tmux | --terminal=screen
#   ./start-team.sh --dir=/some/project

set -euo pipefail

# ---- where things are -----------------------------------------------------
INVOKE_DIR="$PWD"                                    # where you launched from
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" # where the team scripts live
STATE_DIR="$HERE/.team"
PANES_ENV="$STATE_DIR/panes.env"

# ---- config (env-overridable) ---------------------------------------------
SESSION="${TEAM_SESSION:-}"              # empty -> derived from the working dir
BACKEND="${TEAM_BACKEND:-tmux}"          # tmux | screen  (overridden by flags)
WORKDIR="${TEAM_WORKDIR:-$INVOKE_DIR}"   # where the agents operate (default: PWD)
WORKERS="${TEAM_WORKERS:-1}"             # how many Worker agents
TESTERS="${TEAM_TESTERS:-1}"             # how many Tester agents

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
while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--screen)    BACKEND=screen ;;
    -t|--tmux)      BACKEND=tmux ;;
    --terminal=*)   BACKEND="${1#*=}" ;;
    --dir=*)        WORKDIR="${1#*=}" ;;
    --workers=*)    WORKERS="${1#*=}" ;;
    --testers=*)    TESTERS="${1#*=}" ;;
    --workers)      shift; WORKERS="${1:-}" ;;
    --testers)      shift; TESTERS="${1:-}" ;;
    -h|--help)      sed -n '2,24p' "$HERE/start-team.sh"; exit 0 ;;
    *) echo "unknown option: $1 (try -h)" >&2; exit 1 ;;
  esac
  shift
done

case "$BACKEND" in
  tmux|screen) ;;
  *) echo "unknown backend: '$BACKEND' (use tmux or screen)" >&2; exit 1 ;;
esac
command -v "$BACKEND" >/dev/null 2>&1 || { echo "'$BACKEND' is not installed or not on PATH." >&2; exit 1; }

[[ "$WORKERS" =~ ^[0-9]+$ ]] && (( WORKERS >= 1 )) || { echo "--workers must be an integer >= 1" >&2; exit 1; }
[[ "$TESTERS" =~ ^[0-9]+$ ]] || { echo "--testers must be an integer >= 0" >&2; exit 1; }

# Normalize the working dir to an absolute path and make sure it exists.
_req_workdir="$WORKDIR"
WORKDIR="$(cd "$WORKDIR" 2>/dev/null && pwd)" || { echo "working dir does not exist: ${_req_workdir}" >&2; exit 1; }
QWORKDIR="$(printf '%q' "$WORKDIR")"

# Session name: TEAM_SESSION if set, otherwise the working dir's basename with
# any characters tmux/screen dislike (., :, spaces, ...) replaced by '_'.
if [[ -z "$SESSION" ]]; then
  _base="${WORKDIR##*/}"
  SESSION="${_base//[^A-Za-z0-9_-]/_}"
  [[ -n "$SESSION" ]] || SESSION=team
fi

mkdir -p "$STATE_DIR/tasks"

# ---- agent roster ---------------------------------------------------------
# ROLES = ordered list of agent role ids (worker1 worker2 ... tester1 ...)
ROLES=()
for ((i=1;i<=WORKERS;i++)); do ROLES+=("worker$i"); done
for ((i=1;i<=TESTERS;i++)); do ROLES+=("tester$i"); done
TOTAL=$(( 1 + ${#ROLES[@]} ))
(( TOTAL > 8 )) && echo "note: $TOTAL panes will be cramped; tmux handles it better than screen." >&2

role_label() { local k="${1%%[0-9]*}" n="${1##*[!0-9]}"; printf '%s %s' "${k^}" "$n"; }  # worker1 -> "Worker 1"
role_upper() { printf '%s' "${1^^}"; }                                                    # worker1 -> WORKER1

# ---- Lead bootstrap (single line: newlines would submit early) ------------
if (( WORKERS == 1 && TESTERS == 1 )); then
  ROSTER="one Worker (worker1) and one Tester (tester1)"
else
  ROSTER="$WORKERS worker(s) [worker1..worker$WORKERS] and $TESTERS tester(s)$( ((TESTERS>0)) && echo " [tester1..tester$TESTERS]")"
fi
BOOTSTRAP="You are the LEAD orchestrator. You work in $WORKDIR with $ROSTER, each running in its own pane. Read your playbook at $HERE/ORCHESTRATION.md now, then use $HERE/team.sh to dispatch to agents BY NAME (e.g. worker1, worker2, tester1) with send/sendf/read/check/wait, and put task files under $STATE_DIR/tasks/. Run tasks in parallel across workers only when they touch DIFFERENT files. After reading, reply READY and wait for my instructions."

echo "backend=$BACKEND  session=$SESSION  workdir=$WORKDIR  workers=$WORKERS  testers=$TESTERS"

# ===========================================================================
# tmux backend  — Lead is the main pane (left); agents tile down the right.
# ===========================================================================
start_tmux() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' already exists. Attaching."
    echo "(Run 'tmux kill-session -t $SESSION' first for a fresh team.)"
    exec tmux attach -t "$SESSION"
  fi

  local lead last pane role
  lead="$(tmux new-session -d -s "$SESSION" -n "$SESSION" -c "$WORKDIR" -P -F '#{pane_id}')"

  # create one pane per agent (ids captured reliably via -P -F)
  local -a panes=()
  last="$lead"
  for role in "${ROLES[@]}"; do
    pane="$(tmux split-window -t "$last" -c "$WORKDIR" -P -F '#{pane_id}')"
    panes+=("$pane"); last="$pane"
  done

  # Lead = big pane on the left, agents in a column on the right.
  tmux set -t "$SESSION" main-pane-width 50%
  tmux select-layout -t "$SESSION" main-vertical

  # Border labels via a per-pane @role user option (apps can't clobber it).
  tmux set -t "$SESSION" pane-border-status top
  tmux set -t "$SESSION" pane-border-format ' #{@role} '
  tmux set -p -t "$lead" @role Lead
  local idx
  for idx in "${!ROLES[@]}"; do
    tmux set -p -t "${panes[$idx]}" @role "$(role_label "${ROLES[$idx]}")"
  done

  # write the pane map
  {
    echo "TEAM_BACKEND=tmux"
    echo "TEAM_SESSION=$SESSION"
    echo "TEAM_WORKDIR=$WORKDIR"
    echo "WORKERS=$WORKERS"
    echo "TESTERS=$TESTERS"
    echo "LEAD_PANE=$lead"
    for idx in "${!ROLES[@]}"; do
      echo "$(role_upper "${ROLES[$idx]}")_PANE=${panes[$idx]}"
    done
  } > "$PANES_ENV"
  echo "Wrote pane map -> $PANES_ENV"

  # launch the CLIs
  for pane in "${panes[@]}"; do tmux send-keys -t "$pane" "$AGENT_CMD" Enter; done
  tmux send-keys -t "$lead" "$LEAD_CMD" Enter

  sleep 6
  tmux send-keys -t "$lead" -l "$BOOTSTRAP"
  tmux send-keys -t "$lead" Enter

  tmux select-pane -t "$lead"
  exec tmux attach -t "$SESSION"
}

# ===========================================================================
# screen backend  — best-effort: Lead left, agents stacked in a right column.
#   Windows: 0=Lead, 1..K = agents in ROLES order. We `cd "$WORKDIR"` inside
#   every window before launching its CLI (screen's chdir is unreliable for
#   window 0). Region layout is applied after attach.
# ===========================================================================
start_screen() {
  if screen -ls 2>/dev/null | grep -qE "[0-9]+\.${SESSION}[[:space:]]"; then
    echo "Session '$SESSION' already exists. Attaching."
    echo "(Run 'screen -S $SESSION -X quit' first for a fresh team.)"
    exec screen -r "$SESSION"
  fi

  screen -dmS "$SESSION"
  screen -S "$SESSION" -X defdynamictitle off      # default for new windows
  screen -S "$SESSION" -p 0 -X dynamictitle off    # for window 0 (Lead)
  screen -S "$SESSION" -p 0 -X title Lead

  # one window per agent (windows 1..K), titled by role
  local role win=0 idx
  for idx in "${!ROLES[@]}"; do
    win=$(( idx + 1 ))
    screen -S "$SESSION" -X screen -t "$(role_label "${ROLES[$idx]}")"
  done

  # write the pane map (screen: panes are window numbers)
  {
    echo "TEAM_BACKEND=screen"
    echo "TEAM_SESSION=$SESSION"
    echo "TEAM_WORKDIR=$WORKDIR"
    echo "WORKERS=$WORKERS"
    echo "TESTERS=$TESTERS"
    echo "LEAD_PANE=0"
    for idx in "${!ROLES[@]}"; do
      echo "$(role_upper "${ROLES[$idx]}")_PANE=$(( idx + 1 ))"
    done
  } > "$PANES_ENV"
  echo "Wrote pane map -> $PANES_ENV"

  # cd + launch each agent window, then the Lead
  for idx in "${!ROLES[@]}"; do
    screen_send_raw "$(( idx + 1 ))" "cd $QWORKDIR && $AGENT_CMD"
  done
  screen_send_raw 0 "cd $QWORKDIR && $LEAD_CMD"

  screen -S "$SESSION" -X caption always '%{= kw}[%n %t]%{-}'

  # Build the region layout after attach (needs a display), then bootstrap Lead.
  # Right column = K stacked regions; K = number of agents.
  local -a layout=('select 0' 'split -v' 'focus right' 'select 1')
  local w
  for (( w=2; w<=${#ROLES[@]}; w++ )); do
    layout+=('split' 'focus down' "select $w")
  done
  layout+=('focus left')

  (
    sleep 2
    screen -S "$SESSION" -X eval "${layout[@]}" 2>/dev/null || true
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
