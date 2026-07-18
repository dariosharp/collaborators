#!/usr/bin/env bash
# team.sh — the Lead's remote control for the Worker and Tester panes.
#
# Works with either backend (tmux or screen); it reads which one from the
# state file written by start-team.sh.
#   tmux:   send-keys  (send)  / capture-pane (read)
#   screen: stuff      (send)  / hardcopy     (read)
#
# Usage:
#   team.sh send   <worker|tester> "<prompt>"      # inline prompt (single line)
#   team.sh sendf  <worker|tester> <file>          # long/multi-line prompt from a file
#   team.sh read   <worker|tester|lead> [lines]    # dump the pane's recent output (default 120)
#   team.sh wait   <worker|tester> [sentinel] [timeout_s]  # block until sentinel appears
#   team.sh status                                 # show backend + pane map
#
# Sentinel protocol: end every task prompt with an instruction to print a
# unique line when finished, e.g. "...When done, print exactly: WORKER_TASK_DONE"
# then call:  team.sh wait worker
# (the sentinel defaults to <ROLE>_TASK_DONE, kept symbol-free so it survives
#  being typed unquoted in zsh.)

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

pane_for() {
  case "$1" in
    lead|LEAD)     echo "$LEAD_PANE" ;;
    worker|WORKER) echo "$WORKER_PANE" ;;
    tester|TESTER) echo "$TESTER_PANE" ;;
    *) echo "unknown role: $1 (use lead|worker|tester)" >&2; return 1 ;;
  esac
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
# ---------------------------------------------------------------------------

cmd="${1:-}"; shift || true

case "$cmd" in
  send)
    role="${1:?role required}"; shift
    pane="$(pane_for "$role")"
    msg="$*"
    _send_line "$pane" "$msg"
    echo "-> $role: sent (${#msg} chars via $BACKEND)"
    ;;

  sendf)
    role="${1:?role required}"; file="${2:?file required}"
    [[ -f "$file" ]] || { echo "no such file: $file" >&2; exit 1; }
    pane="$(pane_for "$role")"
    # For long/multi-line tasks, don't blast newlines (each = submit).
    # Point the agent at the file instead — still one send.
    _send_line "$pane" "Read the file '$file' and carry out the task described in it fully."
    echo "-> $role: pointed at $file (via $BACKEND)"
    ;;

  read)
    role="${1:?role required}"; lines="${2:-120}"
    pane="$(pane_for "$role")"
    _dump "$pane" "$lines"
    ;;

  wait)
    role="${1:?role required}"
    sentinel="${2:-${role^^}_TASK_DONE}"
    timeout="${3:-600}"
    pane="$(pane_for "$role")"
    elapsed=0
    while (( elapsed < timeout )); do
      if _dump "$pane" 400 | grep -qF -- "$sentinel"; then
        echo "OK: '$sentinel' seen after ${elapsed}s"
        exit 0
      fi
      sleep 3; elapsed=$((elapsed + 3))
    done
    echo "TIMEOUT: '$sentinel' not seen within ${timeout}s" >&2
    exit 2
    ;;

  status)
    echo "backend=$BACKEND session=$TEAM_SESSION"
    echo "lead=$LEAD_PANE worker=$WORKER_PANE tester=$TESTER_PANE"
    ;;

  ""|-h|--help|help)
    sed -n '2,25p' "$HERE/team.sh"
    ;;

  *)
    echo "unknown command: $cmd (try: send sendf read wait status)" >&2
    exit 1
    ;;
esac
