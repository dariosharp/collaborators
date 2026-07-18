#!/usr/bin/env bash
# team.sh — the Lead's remote control for the Worker/Tester panes.
#
# Agents are addressed by name: lead, worker1, worker2, ..., tester1, tester2, ...
# ("worker" and "tester" are accepted as aliases for worker1 / tester1.)
# Works with either backend (tmux or screen), read from the state file.
#   tmux:   send-keys  (send)  / capture-pane (read)
#   screen: stuff      (send)  / hardcopy     (read)
#
# Usage:
#   team.sh send    <agent> "<prompt>"        # inline prompt (single line)
#   team.sh sendf   <agent> <file>            # long/multi-line prompt from a file
#   team.sh read    <agent> [lines]           # dump the pane's recent output (default 120)
#   team.sh check   <agent> [sentinel]        # NON-BLOCKING: exit 0 if done, 1 if still running
#   team.sh wait    <agent> [sentinel] [timeout_s]   # BLOCK until sentinel appears
#   team.sh wait-any <agent> <agent> ...      # block until ANY listed agent is done; prints which
#   team.sh list                              # list all agents and their panes
#   team.sh status                            # show backend + session
#
# Sentinel protocol: end every task prompt with an instruction to print a unique
# line when finished. The sentinel for an agent is <AGENT>_TASK_DONE in caps —
# e.g. worker2 -> WORKER2_TASK_DONE (symbol-free so it survives unquoted zsh).
# For parallel work, dispatch to several workers, then poll them with `check` in
# a loop (or block on `wait-any`).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="$HERE/.team"
PANES_ENV="$STATE_DIR/panes.env"

if [[ ! -f "$PANES_ENV" ]]; then
  echo "No pane map found. Start the team first: team/start-team.sh" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$PANES_ENV"
BACKEND="${TEAM_BACKEND:-tmux}"
WORKERS="${WORKERS:-1}"
TESTERS="${TESTERS:-1}"

# Canonicalize an agent name: lead | worker<N> | tester<N>
# ("worker"/"tester" -> worker1/tester1). Prints canonical, or fails.
canon() {
  local r; r="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$r" in
    lead)          echo lead ;;
    worker)        echo worker1 ;;
    tester)        echo tester1 ;;
    worker[0-9]*)  echo "worker${r#worker}" ;;
    tester[0-9]*)  echo "tester${r#tester}" ;;
    *) echo "unknown agent: $1 (use lead, worker<N>, tester<N>)" >&2; return 1 ;;
  esac
}

pane_for() {   # <agent> -> pane id / window number
  local c var val
  c="$(canon "$1")" || return 1
  var="$(printf '%s' "$c" | tr '[:lower:]' '[:upper:]')_PANE"
  val="${!var:-}"
  [[ -n "$val" ]] || { echo "no such agent: $1 (not in this team's pane map)" >&2; return 1; }
  printf '%s' "$val"
}

sentinel_for() {   # <agent> -> WORKER2_TASK_DONE
  local c; c="$(canon "$1")" || return 1
  printf '%s_TASK_DONE' "$(printf '%s' "$c" | tr '[:lower:]' '[:upper:]')"
}

# ---- backend-specific primitives ------------------------------------------
_send_line() {   # <pane> <text>   send literal text, then Enter
  local pane="$1"; shift
  case "$BACKEND" in
    tmux)
      tmux send-keys -t "$pane" -l -- "$*"
      tmux send-keys -t "$pane" Enter ;;
    screen)
      screen -S "$TEAM_SESSION" -p "$pane" -X stuff "$*"
      screen -S "$TEAM_SESSION" -p "$pane" -X stuff $'\r' ;;
  esac
}

_dump() {        # <pane> <lines>  print recent pane output
  local pane="$1" lines="$2"
  case "$BACKEND" in
    tmux)
      tmux capture-pane -p -t "$pane" -S "-${lines}" ;;
    screen)
      # hardcopy -h dumps scrollback + the visible screen, but pads the bottom
      # with blank lines. Strip trailing blanks first, then take the last N.
      local f="$STATE_DIR/hardcopy.${pane}"
      screen -S "$TEAM_SESSION" -p "$pane" -X hardcopy -h "$f"
      sleep 0.3
      [[ -f "$f" ]] || return 0
      awk '{a[NR]=$0} END{n=NR; while(n>0 && a[n]~/^[ \t]*$/) n--;
            for(i=1;i<=n;i++) print a[i]}' "$f" | tail -n "$lines" ;;
  esac
}

_is_done() {     # <agent> <sentinel> -> 0 if sentinel present, else 1
  local pane; pane="$(pane_for "$1")" || return 2
  _dump "$pane" 400 | grep -qF -- "$2"
}
# ---------------------------------------------------------------------------

cmd="${1:-}"; shift || true

case "$cmd" in
  send)
    role="${1:?agent required}"; shift
    pane="$(pane_for "$role")"
    msg="$*"
    _send_line "$pane" "$msg"
    echo "-> $role: sent (${#msg} chars via $BACKEND)"
    ;;

  sendf)
    role="${1:?agent required}"; file="${2:?file required}"
    [[ -f "$file" ]] || { echo "no such file: $file" >&2; exit 1; }
    pane="$(pane_for "$role")"
    _send_line "$pane" "Read the file '$file' and carry out the task described in it fully."
    echo "-> $role: pointed at $file (via $BACKEND)"
    ;;

  read)
    role="${1:?agent required}"; lines="${2:-120}"
    pane="$(pane_for "$role")"
    _dump "$pane" "$lines"
    ;;

  check)   # non-blocking
    role="${1:?agent required}"
    sentinel="${2:-$(sentinel_for "$role")}"
    if _is_done "$role" "$sentinel"; then
      echo "done: $role"; exit 0
    else
      echo "running: $role"; exit 1
    fi
    ;;

  wait)    # blocking, single agent
    role="${1:?agent required}"
    sentinel="${2:-$(sentinel_for "$role")}"
    timeout="${3:-600}"
    elapsed=0
    while (( elapsed < timeout )); do
      if _is_done "$role" "$sentinel"; then
        echo "OK: '$sentinel' seen after ${elapsed}s"; exit 0
      fi
      sleep 3; elapsed=$((elapsed + 3))
    done
    echo "TIMEOUT: '$sentinel' not seen within ${timeout}s" >&2
    exit 2
    ;;

  wait-any)  # blocking, returns the first of several agents to finish
    (( $# >= 1 )) || { echo "wait-any needs at least one agent" >&2; exit 1; }
    agents=("$@")
    elapsed=0; timeout=600
    while (( elapsed < timeout )); do
      for a in "${agents[@]}"; do
        if _is_done "$a" "$(sentinel_for "$a")"; then
          echo "done: $a"; exit 0
        fi
      done
      sleep 3; elapsed=$((elapsed + 3))
    done
    echo "TIMEOUT: none of [${agents[*]}] finished within ${timeout}s" >&2
    exit 2
    ;;

  list)
    echo "backend=$BACKEND session=$TEAM_SESSION workers=$WORKERS testers=$TESTERS"
    echo "  lead    -> $LEAD_PANE"
    for ((i=1;i<=WORKERS;i++)); do v="WORKER${i}_PANE"; echo "  worker$i -> ${!v:-?}"; done
    for ((i=1;i<=TESTERS;i++)); do v="TESTER${i}_PANE"; echo "  tester$i -> ${!v:-?}"; done
    ;;

  status)
    echo "backend=$BACKEND session=$TEAM_SESSION workers=$WORKERS testers=$TESTERS"
    ;;

  ""|-h|--help|help)
    sed -n '2,30p' "$HERE/team.sh"
    ;;

  *)
    echo "unknown command: $cmd (try: send sendf read check wait wait-any list status)" >&2
    exit 1
    ;;
esac
