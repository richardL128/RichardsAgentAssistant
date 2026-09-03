#!/usr/bin/env bash
# Run Claude Code with --dangerously-skip-permissions inside a container whose
# only view of the host filesystem is this repository.
#
# See docs/claude-sandbox.md for the isolation model.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/compose.claude.yaml"
IMAGE="lifeagent-claude-sandbox:local"
HOME_VOLUME="lifeagent_claude_home"
SERVICE="claude"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")

usage() {
    cat <<'USAGE'
Usage: scripts/claude-sandbox.sh [options] [command] [args...]

Commands:
  (none) [args]   Start Claude Code with --dangerously-skip-permissions.
                  Extra args are passed straight to `claude`, so
                  `claude-sandbox.sh -p "run the tests"` works.
  login           Start Claude Code without the permission bypass, for the
                  first-run OAuth login. Credentials persist in the
                  lifeagent_claude_home volume.
  shell [args]    Open a bash shell in the sandbox instead of the agent.
  build           Rebuild the sandbox image (also how you upgrade Claude Code
                  or add tools to it).
  reset           Delete the sandbox home volume, discarding stored
                  credentials, agent history and caches.

Options (before the command):
  --port PORT     Publish container PORT on 127.0.0.1:PORT, for previewing a
                  dev server the agent starts. Repeatable.

Environment:
  ANTHROPIC_API_KEY   Passed through; set it to skip interactive login.
  GIT_USER_NAME       Passed through; git identity for in-sandbox commits.
  GIT_USER_EMAIL      Passed through; git identity for in-sandbox commits.
USAGE
}

run_opts=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            [[ $# -ge 2 ]] || { echo "claude-sandbox: --port needs a value" >&2; exit 2; }
            run_opts+=(--publish "127.0.0.1:$2:$2")
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

ensure_image() {
    if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
        echo "claude-sandbox: building ${IMAGE} (first run only)..." >&2
        "${COMPOSE[@]}" build "${SERVICE}"
    fi
}

# bash 3.2, which is what /usr/bin/env bash finds on stock macOS, treats the
# expansion of an empty array as unset under `set -u`, hence the `+` guard.
sandbox_run() {
    exec "${COMPOSE[@]}" run --rm ${run_opts[@]+"${run_opts[@]}"} "${SERVICE}" "$@"
}

subcommand="${1:-agent}"
case "${subcommand}" in
    build)
        exec "${COMPOSE[@]}" build --pull "${SERVICE}"
        ;;
    reset)
        "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
        if docker volume rm "${HOME_VOLUME}" >/dev/null 2>&1; then
            echo "claude-sandbox: removed ${HOME_VOLUME}"
        else
            echo "claude-sandbox: no ${HOME_VOLUME} volume to remove"
        fi
        ;;
    shell)
        shift
        ensure_image
        sandbox_run bash "$@"
        ;;
    login)
        shift
        ensure_image
        sandbox_run claude "$@"
        ;;
    agent)
        ensure_image
        sandbox_run claude --dangerously-skip-permissions
        ;;
    *)
        ensure_image
        sandbox_run claude --dangerously-skip-permissions "$@"
        ;;
esac
