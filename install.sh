#!/bin/sh
# install.sh — installs the `collab` command.
#
#   curl -fsSL https://raw.githubusercontent.com/dariosharp/collaborators/main/install.sh | sh
#
# Run from a clone instead (./install.sh) and it installs that checkout, local
# changes included.
#
# Uses uv if available, then pipx, then a private virtualenv as a fallback.
# Environment:
#   COLLAB_REF   git branch/tag to install (default: main)
#   COLLAB_HOME  virtualenv location for the fallback (default ~/.local/share/collaborators)
#   COLLAB_BIN   where the fallback links `collab` (default ~/.local/bin)
set -eu

REPO="https://github.com/dariosharp/collaborators"
REF="${COLLAB_REF:-main}"
SRC="git+$REPO@$REF"
# Started as ./install.sh inside a clone (and no COLLAB_REF asked for): install the clone.
HERE=""
case "$0" in  # not when piped into sh: $0 is then just "sh"
  *install.sh) HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)" ;;
esac
if [ -z "${COLLAB_REF:-}" ] && [ -n "$HERE" ] && [ -f "$HERE/pyproject.toml" ] \
    && grep -q '^name = "collaborators"' "$HERE/pyproject.toml"; then
  SRC="$HERE"
  REF="local checkout $HERE"
fi
HOME_DIR="${COLLAB_HOME:-$HOME/.local/share/collaborators}"
BIN_DIR="${COLLAB_BIN:-$HOME/.local/bin}"

log()  { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
die()  { log "error: $*" >&2; exit 1; }

find_python() {
  for py in python3.14 python3.13 python3.12 python3.11 python3; do
    if have "$py" && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      echo "$py"; return 0
    fi
  done
  return 1
}

log "Installing collaborators ($REF)…"

if have uv; then
  uv tool install --force --python ">=3.11" "$SRC"
elif have pipx; then
  pipx install --force "$SRC"
else
  PY="$(find_python)" || die "Python 3.11+ is required (or install uv: https://docs.astral.sh/uv/)."
  "$PY" -m venv "$HOME_DIR/venv" 2>/dev/null || die "could not create a virtualenv with $PY.
On Debian/Ubuntu: sudo apt install python3-venv   — or install uv / pipx and re-run."
  "$HOME_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
  "$HOME_DIR/venv/bin/python" -m pip install --quiet --upgrade "$SRC"
  mkdir -p "$BIN_DIR"
  ln -sf "$HOME_DIR/venv/bin/collab" "$BIN_DIR/collab"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) log "Add $BIN_DIR to your PATH:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
fi

have claude || log "note: the 'claude' CLI was not found on PATH. Log in to Claude Code once
      (https://claude.com/claude-code) so the agents can authenticate."

log ""
log "Done. Try it without spending anything:   collab --demo"
log "Then, in your project:                     cd /your/project && collab"
