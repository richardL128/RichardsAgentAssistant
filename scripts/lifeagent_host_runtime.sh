#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$(dirname -- "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"

. "$SCRIPT_DIR/lifeagent_launchd_common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/lifeagent_host_runtime.sh COMMAND [--env-file PATH]

Commands:
  deploy     Build the shared app image once, run migration/test preflight,
             record the non-secret image id, create host secrets if absent,
             and install/restart the Ollama and Discord wake LaunchAgents.
  install    Install the Ollama and Discord wake LaunchAgents without build,
             migration, or test preflight.
  status     Report LaunchAgent and Ollama runtime status.
  uninstall  Stop and remove the LaunchAgent plists. Deployed image and secret
             files are left in place.
USAGE
}

COMMAND="${1:-}"
if [ -n "$COMMAND" ]; then
  shift
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:?missing --env-file value}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ "${OLLAMA_HOST+x}" = x ]; then OLLAMA_HOST_ENV_SET="1"; else OLLAMA_HOST_ENV_SET="0"; fi
OLLAMA_HOST_VALUE="${OLLAMA_HOST:-}"

load_env_file() {
  [ -f "$ENV_FILE" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ""|\#*) continue ;;
      export\ *) line="${line#export }" ;;
    esac
    case "$line" in
      *=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    value="$(strip_simple_quotes "${line#*=}")"
    case "$key" in
      OLLAMA_HOST)
        [ "$OLLAMA_HOST_ENV_SET" = "1" ] || OLLAMA_HOST_VALUE="$value"
        ;;
    esac
  done < "$ENV_FILE"
}

run_test_preflight() {
  if [ -n "${LIFEAGENT_TEST_COMMAND:-}" ]; then
    sh -c "$LIFEAGENT_TEST_COMMAND"
    return
  fi
  if [ ! -x "$REPO_DIR/.venv/bin/pytest" ]; then
    echo ".venv/bin/pytest is required for deploy preflight" >&2
    exit 127
  fi
  "$REPO_DIR/.venv/bin/pytest" -q \
    tests/unit/test_host_*.py \
    tests/unit/test_discord_handoff.py \
    tests/unit/test_discord_wake_store.py \
    tests/unit/test_ollama_runtime.py \
    tests/unit/test_ollama_qwen_scripts.py \
    tests/unit/test_academic_delivery.py \
    tests/unit/test_academic_discord_checkin.py \
    tests/unit/test_discord_gateway_checkin.py \
    tests/unit/test_academic_main.py \
    tests/unit/test_phase0_core.py \
    tests/unit/test_queue.py
}

record_deployed_image_id() {
  image_id="$(docker image inspect lifeagent-app:local --format '{{.Id}}' 2>/dev/null || true)"
  if [ -z "$image_id" ]; then
    echo "could not resolve lifeagent-app:local image id after build" >&2
    exit 1
  fi
  image_file="$(deployed_image_id_file)"
  mkdir -p "$(dirname -- "$image_file")"
  tmp="${image_file}.tmp"
  umask 177
  printf '%s\n' "$image_id" > "$tmp"
  mv "$tmp" "$image_file"
  chmod 0600 "$image_file"
  echo "recorded deployed image id: $image_file"
}

ensure_hmac_secret() {
  secret_file="$(discord_wake_hmac_secret_file)"
  mkdir -p "$(dirname -- "$secret_file")"
  if [ -f "$secret_file" ]; then
    chmod 0600 "$secret_file"
    echo "using existing Discord wake HMAC secret file: $secret_file"
    return
  fi
  command_required openssl
  tmp="${secret_file}.tmp"
  umask 177
  openssl rand -hex 32 > "$tmp"
  mv "$tmp" "$secret_file"
  chmod 0600 "$secret_file"
  echo "created Discord wake HMAC secret file: $secret_file"
}

install_agents() {
  OLLAMA_HOST_VALUE="${OLLAMA_HOST_VALUE:-$DEFAULT_OLLAMA_HOST}"
  install_ollama_launch_agent
  install_discord_wake_launch_agent
}

restart_agents() {
  restart_launch_agent "$LIFEAGENT_OLLAMA_LABEL"
  restart_launch_agent "$LIFEAGENT_DISCORD_WAKE_LABEL"
}

deploy() {
  command_required docker
  command_required launchctl
  command_required python3
  cd "$REPO_DIR"
  docker compose --env-file "$ENV_FILE" build api
  docker compose --env-file "$ENV_FILE" up -d --no-build postgres
  docker compose --env-file "$ENV_FILE" run --rm api alembic upgrade head
  run_test_preflight
  record_deployed_image_id
  ensure_hmac_secret
  install_agents
  restart_agents
  # The native daemon is now the sole ingress. Leave the backend cold so the
  # first authorized mention exercises the fixed no-build wake path.
  docker compose --env-file "$ENV_FILE" stop api worker-academic-planner
}

status() {
  command_required launchctl
  echo "discord_wake_launchd=$(launchd_status "$LIFEAGENT_DISCORD_WAKE_LABEL")"
  set +e
  "$SCRIPT_DIR/ollama_qwen_status.sh" --env-file "$ENV_FILE"
  status_code="$?"
  set -e
  return "$status_code"
}

uninstall() {
  command_required launchctl
  uninstall_launch_agent "$LIFEAGENT_DISCORD_WAKE_LABEL"
  uninstall_launch_agent "$LIFEAGENT_OLLAMA_LABEL"
}

load_env_file

case "$COMMAND" in
  deploy)
    deploy
    ;;
  install)
    ensure_hmac_secret
    install_agents
    ;;
  status)
    status
    ;;
  uninstall)
    uninstall
    ;;
  -h|--help|"")
    usage
    [ -n "$COMMAND" ] || exit 2
    ;;
  *)
    echo "unknown command: $COMMAND" >&2
    usage >&2
    exit 2
    ;;
esac
