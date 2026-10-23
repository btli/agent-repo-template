#!/bin/sh
# Checks the tools bootstrap needs, then hands every argument to the bootstrap command.
set -eu

fail() {
  printf '%s\n' "$1" >&2
  exit 1
}

case $0 in
  */*) here=${0%/*} ;;
  *) here=. ;;
esac

command -v python3 >/dev/null 2>&1 || fail "bootstrap needs python3 3.11 or newer"
python3 -I -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' ||
  fail "bootstrap needs python3 3.11 or newer"
command -v gh >/dev/null 2>&1 || fail "bootstrap needs the GitHub CLI (gh)"
command -v yq >/dev/null 2>&1 || fail "bootstrap needs yq v4"
case $(yq --version 2>/dev/null || true) in
  *"version v4."*) ;;
  *) fail "bootstrap needs yq v4" ;;
esac

exec python3 "$here/lib/agent_repo_cli.py" bootstrap "$@"
