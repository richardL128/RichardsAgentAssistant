#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$(dirname -- "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"
HMAC_SECRET_FILE="${LIFEAGENT_DISCORD_WAKE_HMAC_SECRET_FILE:-$REPO_DIR/.artifacts/discord-wake/discord-wake-hmac.key}"
PYTHON_BIN="$REPO_DIR/.venv/bin/python"
DOCKER_BIN=""
LAUNCHCTL_BIN="/bin/launchctl"
OLLAMA_BIN=""

usage() {
  cat <<'USAGE'
Usage: scripts/lifeagent_discord_wake_daemon.sh [options]

Runs the lightweight native Discord Gateway daemon. It only loads allowlisted
settings, never prints secrets, and starts Docker/Compose only after an
authorized event.

Options:
  --env-file PATH
  --hmac-secret-file PATH
  --python PATH
  --docker PATH
  --launchctl PATH
  --ollama PATH
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="${2:?missing --env-file value}"; shift 2 ;;
    --hmac-secret-file) HMAC_SECRET_FILE="${2:?missing --hmac-secret-file value}"; shift 2 ;;
    --python) PYTHON_BIN="${2:?missing --python value}"; shift 2 ;;
    --docker) DOCKER_BIN="${2:?missing --docker value}"; shift 2 ;;
    --launchctl) LAUNCHCTL_BIN="${2:?missing --launchctl value}"; shift 2 ;;
    --ollama) OLLAMA_BIN="${2:?missing --ollama value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for executable in "$PYTHON_BIN" "$DOCKER_BIN" "$LAUNCHCTL_BIN" "$OLLAMA_BIN"; do
  case "$executable" in
    /*) ;;
    *) echo "host runtime executables must use absolute paths" >&2; exit 2 ;;
  esac
  [ -x "$executable" ] || {
    echo "a configured host runtime executable is unavailable" >&2
    exit 127
  }
done

[ -f "$ENV_FILE" ] || {
  echo "LifeAgent .env is missing; run the documented deploy command" >&2
  exit 1
}
[ -f "$HMAC_SECRET_FILE" ] || {
  echo "Discord wake HMAC key is missing; run the documented deploy command" >&2
  exit 1
}

strip_simple_quotes() {
  case "$1" in
    \"*\") value="${1#\"}"; printf '%s\n' "${value%\"}" ;;
    \'*\') value="${1#\'}"; printf '%s\n' "${value%\'}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ""|\#*) continue ;;
    export\ *) line="${line#export }" ;;
  esac
  case "$line" in *=*) ;; *) continue ;; esac
  key="${line%%=*}"
  value="$(strip_simple_quotes "${line#*=}")"
  case "$key" in
    DISCORD_BOT_TOKEN|DISCORD_APPLICATION_ID|DISCORD_ACADEMIC_CHANNEL_ID|\
    DISCORD_ACADEMIC_AUTHORIZED_USER_IDS|DISCORD_API_BASE_URL|API_PORT|\
    DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED|\
    DISCORD_HANDOFF_REQUEST_TIMEOUT_SECONDS|DISCORD_HANDOFF_RETRY_ATTEMPTS|\
    OLLAMA_MODEL|OLLAMA_MODEL_DIGEST|OLLAMA_STARTUP_TIMEOUT_SECONDS|\
    LIFEAGENT_DOCKER_DESKTOP_TIMEOUT_SECONDS|\
    LIFEAGENT_COMPOSE_WAIT_SECONDS|LIFEAGENT_API_LIVE_TIMEOUT_SECONDS|\
    LIFEAGENT_WAKE_OUTBOX_RETENTION_SECONDS)
      export "$key=$value"
      ;;
  esac
done < "$ENV_FILE"

DISCORD_HOST_HANDOFF_SECRET="$(sed -n '1p' "$HMAC_SECRET_FILE")"
[ -n "$DISCORD_HOST_HANDOFF_SECRET" ] || {
  echo "Discord wake HMAC key is empty; run the documented deploy command" >&2
  exit 1
}
export DISCORD_HOST_HANDOFF_SECRET
export LIFEAGENT_REPOSITORY_ROOT="$REPO_DIR"
export LIFEAGENT_DOCKER="$DOCKER_BIN"
export LIFEAGENT_LAUNCHCTL="$LAUNCHCTL_BIN"
export LIFEAGENT_OLLAMA="$OLLAMA_BIN"

exec "$PYTHON_BIN" -m app.host.daemon
