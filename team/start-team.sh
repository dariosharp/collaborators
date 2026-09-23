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
#
# Resume (-r): bring a closed or crashed team back exactly as you left it —
# every pane reattached to its own previous conversation.
#   ./start-team.sh -r                                     # resume this dir's team
#   ./start-team.sh -r -y                                  # skip the confirmation
#   ./start-team.sh -r -n                                  # show the plan, start nothing
# Each agent is launched with a fixed --session-id, recorded in
# .team/sessions/<team>.env, so -r replays those ids with --resume. For a team
# started before -r existed there is no record, so -r falls back to identifying
# each agent's transcript under ~/.claude/projects by the sentinel only that
# agent was ever told to print, and asks you to confirm. An agent it cannot
# place — and any role added since the last run — simply starts fresh.

set -euo pipefail

# ---- where things are -----------------------------------------------------
INVOKE_DIR="$PWD"                                    # where you launched from
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" # where the team scripts live
STATE_DIR="$HERE/.team"
PANES_ENV="$STATE_DIR/panes.env"
SESSIONS_DIR="$STATE_DIR/sessions"                   # per-team session-id records
TEAM_STATE=""                                        # set once SESSION is known

# ---- config (env-overridable) ---------------------------------------------
SESSION="${TEAM_SESSION:-}"              # empty -> derived from the working dir
BACKEND="${TEAM_BACKEND:-tmux}"          # tmux | screen  (overridden by flags)
WORKDIR="${TEAM_WORKDIR:-$INVOKE_DIR}"   # where the agents operate (default: PWD)
WORKERS="${TEAM_WORKERS:-1}"             # how many Worker agents
TESTERS="${TEAM_TESTERS:-1}"             # how many Tester agents
RESUME=0                                 # -r: reattach agents to their old sessions
ASSUME_YES=0                             # -y: don't ask about a detected mapping
DRY_RUN=0                                # -n: show what would be launched, then stop

# All agents (Lead included) run unattended → skip permission prompts by
# default, else automation freezes on the first prompt. AUTO_APPROVE=0 makes
# every agent ask for permissions instead.
AUTO_APPROVE="${AUTO_APPROVE:-1}"
_skip=""
[[ "$AUTO_APPROVE" == "1" ]] && _skip=" --dangerously-skip-permissions"

# Per-role defaults: model + reasoning effort. Override any of these via env
# (LEAD_CMD / WORKER_CMD / TESTER_CMD). AGENT_CMD, if set, is the fallback for
# both Workers and Testers (kept for backward compatibility). The permission
# flag is appended from AUTO_APPROVE above, so overrides needn't include it.
#   Lead    -> Fable 5.1, effort high
#   Worker  -> Fable 5.1, effort max
#   Tester  -> Opus 4.8,  effort xhigh
LEAD_CMD="${LEAD_CMD:-claude --model claude-fable-5-1 --effort high}${_skip}"
WORKER_CMD="${WORKER_CMD:-${AGENT_CMD:-claude --model claude-fable-5-1 --effort max}}${_skip}"
TESTER_CMD="${TESTER_CMD:-${AGENT_CMD:-claude --model claude-opus-4-8 --effort xhigh}}${_skip}"

# worker1 -> $WORKER_CMD, tester1 -> $TESTER_CMD (used when launching each pane)
cmd_for_role() {
  case "$1" in
    lead)    printf '%s' "$LEAD_CMD" ;;
    worker*) printf '%s' "$WORKER_CMD" ;;
    tester*) printf '%s' "$TESTER_CMD" ;;
    *)       printf '%s' "$WORKER_CMD" ;;
  esac
}
# ---------------------------------------------------------------------------

# ---- resume support -------------------------------------------------------
# Every agent gets an explicit --session-id at launch, saved to
# $SESSIONS_DIR/<team>.env. `-r` feeds those ids back as --resume, so each pane
# reopens its own conversation. We never pass --fork-session: the ids stay
# stable, which keeps -r repeatable across any number of crashes.
declare -A SID=()      # role -> session uuid
declare -A RESUMED=()  # role -> set when that id is an existing conversation
declare -A SFILE=()    # role -> transcript path, when we detected it
declare -A SMODEL=()   # role -> model found in that transcript (shown for review)

# Assign a transcript to a role, and remember it so no other role can take it.
claim() {
  SID[$1]="$(sid_of "$2")"; RESUMED[$1]=1; SFILE[$1]="$2"
  SMODEL[$1]="$(transcript_model "$2")"
}
taken() {   # already assigned to some role?
  local o
  for o in "${!SFILE[@]}"; do
    if [[ "${SFILE[$o]}" == "$1" ]]; then return 0; fi
  done
  return 1
}

new_uuid() {
  if [[ -r /proc/sys/kernel/random/uuid ]]; then
    cat /proc/sys/kernel/random/uuid
  elif command -v uuidgen >/dev/null 2>&1; then
    uuidgen | tr '[:upper:]' '[:lower:]'
  else
    printf '%08x-%04x-4%03x-8%03x-%04x%08x\n' $((RANDOM*RANDOM)) $((RANDOM%65536)) \
      $((RANDOM%4096)) $((RANDOM%4096)) $((RANDOM%65536)) $((RANDOM*RANDOM))
  fi
}

# Claude keeps a directory's transcripts under ~/.claude/projects/<abs path with
# every non-alphanumeric character replaced by '-'>.
project_dir_for() {
  printf '%s/.claude/projects/%s' "$HOME" "$(printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g')"
}

sid_of() { local b="${1##*/}"; printf '%s' "${b%.jsonl}"; }
transcript_model() {   # first model id recorded in a transcript
  { grep -m1 -o '"model":[[:space:]]*"[^"]*"' "$1" 2>/dev/null || true; } \
    | head -1 | sed 's/.*"\([^"]*\)"$/\1/'
}

# Read the ids this team was last started with. Only *_SID lines are parsed —
# sourcing the file would clobber SESSION/WORKERS/TESTERS for this run.
load_recorded_sids() {
  [[ -f "$TEAM_STATE" ]] || return 1
  local key val role got=0
  while IFS='=' read -r key val; do
    [[ "$key" == *_SID && -n "$val" ]] || continue
    role="$(printf '%s' "${key%_SID}" | tr '[:upper:]' '[:lower:]')"
    SID[$role]="$val"; RESUMED[$role]=1
    [[ "$role" == lead ]] && got=1
  done < "$TEAM_STATE"
  (( got ))
}

# Fallback for teams started before -r existed: work out who was who from the
# transcripts of this working dir. The Lead is the only agent that ever got the
# bootstrap line; the rest are told apart by the model each role runs.
detect_sessions() {
  local pdir; pdir="$(project_dir_for "$WORKDIR")"
  [[ -d "$pdir" ]] || return 1

  local -a files=(); local f
  while IFS= read -r f; do [[ -n "$f" ]] && files+=("$f"); done \
    < <(ls -1t "$pdir"/*.jsonl 2>/dev/null || true)
  (( ${#files[@]} )) || return 1

  # The Lead is the only agent that ever received the bootstrap line. It must be
  # identified first: its transcript quotes EVERY agent's sentinel, so leaving it
  # in the pool below would make it match any role.
  local lead_file="" f
  for f in "${files[@]}"; do
    if grep -qF "You are the LEAD orchestrator" "$f" 2>/dev/null; then lead_file="$f"; break; fi
  done
  [[ -n "$lead_file" ]] || return 1
  claim lead "$lead_file"

  # Strongest signal: each agent's transcript carries the sentinel the Lead told
  # it to print, and only that one — WORKER2_TASK_DONE only ever reached worker2.
  local r
  for r in "${ROLES[@]}"; do
    for f in "${files[@]}"; do
      if ! taken "$f" && grep -qF "$(role_upper "$r")_TASK_DONE" "$f" 2>/dev/null; then
        claim "$r" "$f"; break
      fi
    done
  done

  # A role with no sentinel of its own cannot be pinned down with any confidence,
  # and the directory may well hold unrelated sessions. Guessing would reattach an
  # agent to a stranger's conversation, so we leave it unassigned and
  # plan_sessions gives it a fresh id instead.
  return 0
}

show_plan() {
  local r sid tag when
  for r in lead "${ROLES[@]}"; do
    sid="${SID[$r]:-}"
    if [[ -z "$sid" ]]; then
      printf '  %-9s %-38s %s\n' "$r" "-" "will start fresh (no previous conversation identified)"
      continue
    fi
    tag="resume"; [[ -n "${RESUMED[$r]:-}" ]] || tag="new session"
    when=""
    [[ -n "${SFILE[$r]:-}" && -f "${SFILE[$r]:-}" ]] &&
      when="   last active $(date -r "${SFILE[$r]}" '+%Y-%m-%d %H:%M')${SMODEL[$r]:+   ${SMODEL[$r]}}"
    printf '  %-9s %-38s %s%s\n' "$r" "$sid" "$tag" "$when"
  done
}

# Decide which session id every pane launches with.
plan_sessions() {
  local r ans
  if (( RESUME )); then
    if load_recorded_sids; then
      echo "Resuming team '$SESSION' from $TEAM_STATE"
      show_plan
    elif detect_sessions; then
      echo "No recorded ids for '$SESSION' (team predates -r)."
      echo "Detected the previous agents in $(project_dir_for "$WORKDIR"):"
      show_plan
      if (( ! ASSUME_YES )) && [[ -t 0 ]]; then
        read -r -p "Resume with this mapping? [Y/n] " ans || ans=""
        if [[ -n "$ans" && "$ans" != [Yy]* ]]; then
          echo "Aborted. To pick the ids yourself:" >&2
          echo "  LEAD_CMD='claude --resume <id> --model claude-fable-5-1 --effort high' \\" >&2
          echo "  WORKER_CMD='claude --resume <id> ...' start-team ..." >&2
          exit 1
        fi
      fi
    else
      echo "-r: nothing to resume for $WORKDIR — starting a fresh team." >&2
    fi
  fi
  # Fresh run, or a role added since the last one: give it a brand-new id.
  [[ -n "${SID[lead]:-}" ]] || SID[lead]="$(new_uuid)"
  for r in "${ROLES[@]}"; do
    [[ -n "${SID[$r]:-}" ]] || SID[$r]="$(new_uuid)"
  done
}

# Full CLI for a pane: the role's command plus its session flag. --resume for a
# conversation that already exists, --session-id to pin a new one.
launch_cmd() {
  local role="$1" base flag
  base="$(cmd_for_role "$role")"
  if [[ -n "${RESUMED[$role]:-}" ]]; then flag="--resume"; else flag="--session-id"; fi
  printf '%s %s %s' "$base" "$flag" "${SID[$role]}"
}

emit_sids() {
  local r
  echo "LEAD_SID=${SID[lead]}"
  for r in "${ROLES[@]}"; do echo "$(role_upper "$r")_SID=${SID[$r]}"; done
}

# Durable per-team record, so the next -r knows exactly who was who.
save_team_state() {
  mkdir -p "$SESSIONS_DIR"
  {
    echo "# claude-team — session ids for '$SESSION'. Used by start-team.sh -r."
    echo "TEAM_SESSION=$SESSION"
    echo "TEAM_WORKDIR=$WORKDIR"
    echo "WORKERS=$WORKERS"
    echo "TESTERS=$TESTERS"
    emit_sids
  } > "$TEAM_STATE"
  echo "Wrote session ids -> $TEAM_STATE"
}
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
    -r|--resume)    RESUME=1 ;;
    -y|--yes)       ASSUME_YES=1 ;;
    -n|--dry-run)   DRY_RUN=1 ;;
    -h|--help)      awk 'NR>1{ if ($0 ~ /^set -euo/) exit; print }' "$HERE/start-team.sh"; exit 0 ;;
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

# Which conversation each pane opens (fresh ids, or the previous ones under -r).
TEAM_STATE="$SESSIONS_DIR/$SESSION.env"
plan_sessions

# ---- Lead bootstrap (single line: newlines would submit early) ------------
if (( WORKERS == 1 && TESTERS == 1 )); then
  ROSTER="one Worker (worker1) and one Tester (tester1)"
else
  ROSTER="$WORKERS worker(s) [worker1..worker$WORKERS] and $TESTERS tester(s)$( ((TESTERS>0)) && echo " [tester1..tester$TESTERS]")"
fi
BOOTSTRAP="You are the LEAD orchestrator. You work in $WORKDIR with $ROSTER, each running in its own pane. Read your playbook at $HERE/ORCHESTRATION.md now, then use $HERE/team.sh to dispatch to agents BY NAME (e.g. worker1, worker2, tester1) with send/sendf/read/check/wait, and put task files under $STATE_DIR/tasks/. Run tasks in parallel across workers only when they touch DIFFERENT files. After reading, reply READY and wait for my instructions."

# Under -r the Lead is already briefed — it only needs to know it was restarted.
RESUME_NOTICE="Your team was interrupted and has just been restarted. Your conversation and every agent's are intact, and the pane map has been rewritten, so keep dispatching through $HERE/team.sh BY NAME (e.g. worker1, tester1) exactly as before — nothing else has changed. Any task you had dispatched before the interruption was lost mid-flight, so check each agent with read/check before trusting it finished. Briefly recap where we left off and what is still open, then wait for my instructions."

# Keyed on whether the Lead is really coming back, not on the flag: `-r` with
# nothing to resume starts a fresh Lead, and that one needs the full briefing.
if [[ -n "${RESUMED[lead]:-}" ]]; then
  LEAD_OPENING="$RESUME_NOTICE"
  OPENING_DELAY="${OPENING_DELAY:-12}"   # replaying a long transcript takes longer
else
  LEAD_OPENING="$BOOTSTRAP"
  OPENING_DELAY="${OPENING_DELAY:-6}"
fi

echo "backend=$BACKEND  session=$SESSION  workdir=$WORKDIR  workers=$WORKERS  testers=$TESTERS  resume=$RESUME"

if (( DRY_RUN )); then
  echo "--- dry run: nothing started, no state written ---"
  for _r in lead "${ROLES[@]}"; do echo "  $_r: $(launch_cmd "$_r")"; done
  echo "  lead opening message: ${LEAD_OPENING:0:80}... (after ${OPENING_DELAY}s)"
  echo "  would record ids in: $TEAM_STATE"
  exit 0
fi

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
    emit_sids
  } > "$PANES_ENV"
  echo "Wrote pane map -> $PANES_ENV"
  save_team_state

  # launch the CLIs (role-specific model/effort, pinned to that role's session)
  for idx in "${!ROLES[@]}"; do
    tmux send-keys -t "${panes[$idx]}" "$(launch_cmd "${ROLES[$idx]}")" Enter
  done
  tmux send-keys -t "$lead" "$(launch_cmd lead)" Enter

  sleep "$OPENING_DELAY"
  tmux send-keys -t "$lead" -l "$LEAD_OPENING"
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
    emit_sids
  } > "$PANES_ENV"
  echo "Wrote pane map -> $PANES_ENV"
  save_team_state

  # cd + launch each agent window (role-specific command), then the Lead
  for idx in "${!ROLES[@]}"; do
    screen_send_raw "$(( idx + 1 ))" "cd $QWORKDIR && $(launch_cmd "${ROLES[$idx]}")"
  done
  screen_send_raw 0 "cd $QWORKDIR && $(launch_cmd lead)"

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
    sleep "$OPENING_DELAY"
    screen_send_raw 0 "$LEAD_OPENING"
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
