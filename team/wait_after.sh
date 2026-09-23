#!/bin/bash
AGENT="$1"; MARKER="$2"; SENTINEL="$3"; TIMEOUT="${4:-1800}"
TEAM=/home/sdb/.local/share/claude-team/team/team.sh
END=$(( $(date +%s) + TIMEOUT ))
while [ "$(date +%s)" -lt "$END" ]; do
  PANE=$("$TEAM" read "$AGENT" 800 2>/dev/null)
  if ! printf '%s\n' "$PANE" | grep -qF -- "$MARKER"; then echo "MARKER NOT FOUND"; exit 3; fi
  AFTER=$(printf '%s\n' "$PANE" | awk -v m="$MARKER" 'index($0,m){n=NR} {l[NR]=$0} END{for(i=n+1;i<=NR;i++) print l[i]}')
  if printf '%s\n' "$AFTER" | grep -qE "^[[:space:]]*${SENTINEL}[[:space:]]*$"; then
    sleep 2; P2=$("$TEAM" read "$AGENT" 40 2>/dev/null); sleep 2; P3=$("$TEAM" read "$AGENT" 40 2>/dev/null)
    if [ "$P2" = "$P3" ]; then echo DONE; exit 0; fi
  fi
  sleep 15
done
echo TIMEOUT; exit 2
