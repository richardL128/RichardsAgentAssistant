#!/usr/bin/env bash
# Entrypoint for the Claude Code sandbox container.
#
# Runs unprivileged with all capabilities dropped, so this only does setup that
# an ordinary user can do: refuse to start if the workspace mount is missing,
# apply an optional git identity, and print what the agent can reach.
set -euo pipefail

if [[ ! -d /workspace ]]; then
    echo "claude-sandbox: /workspace is missing; the repository bind mount did not attach." >&2
    exit 1
fi

if ! mountpoint -q /workspace 2>/dev/null && [[ ! -e /workspace/pyproject.toml ]]; then
    echo "claude-sandbox: /workspace does not look like the LifeAgent repository." >&2
    echo "claude-sandbox: start the sandbox through scripts/claude-sandbox.sh." >&2
    exit 1
fi

# Git identity is optional and comes from the host environment; without it the
# agent can still read history and stage work, it just cannot commit.
if [[ -n "${GIT_USER_NAME:-}" ]]; then
    git config --global user.name "${GIT_USER_NAME}"
fi
if [[ -n "${GIT_USER_EMAIL:-}" ]]; then
    git config --global user.email "${GIT_USER_EMAIL}"
fi

if [[ "${CLAUDE_SANDBOX_QUIET:-0}" != "1" ]]; then
    cat >&2 <<'BANNER'
─────────────────────────────────────────────────────────────
 Claude Code sandbox
   writable : /workspace (the LifeAgent repo) and this
              container's own filesystem, which is discarded
              when the container exits
   read     : nothing else from the host — no other host path
              is mounted
   network  : full internet access
   caveat   : /workspace is the real repo. Deletions and
              history rewrites there are real. Push often.
─────────────────────────────────────────────────────────────
BANNER
fi

exec "$@"
