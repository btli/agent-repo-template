#!/usr/bin/env bash
# Fails closed unless bubblewrap can create the namespaces the fix agent sandbox needs.
set -uo pipefail

probe() {
  bwrap --unshare-pid --proc /proc --ro-bind / / true
}

if ! command -v bwrap >/dev/null 2>&1; then
  # The install reaches the distro mirror only if the job's egress allowlist includes it.
  sudo apt-get update -qq >/dev/null || true
  sudo apt-get install -y -qq --no-install-recommends bubblewrap >/dev/null || true
  if ! command -v bwrap >/dev/null 2>&1; then
    echo "::error::bwrap is not installed; preinstall bubblewrap or allow the distro mirror." >&2
    exit 1
  fi
fi
if probe; then
  echo "bwrap preflight passed."
  exit 0
fi
# Ubuntu 24.04 AppArmor can block unprivileged user namespaces; relax it once and retry.
if [ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null)" = "1" ]; then
  echo "bwrap preflight failed; allowing unprivileged user namespaces and retrying." >&2
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 >/dev/null || true
  if probe; then
    echo "bwrap preflight passed after the AppArmor change."
    exit 0
  fi
fi
echo "::error::bwrap cannot create user namespaces; the fix agent never runs unsandboxed." >&2
exit 1
