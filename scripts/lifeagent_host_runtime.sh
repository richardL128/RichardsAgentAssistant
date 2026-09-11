#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$(dirname -- "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"
CHECKOUT_SCRIPT_DIR="$SCRIPT_DIR"
CHECKOUT_REPO_DIR="$REPO_DIR"

. "$SCRIPT_DIR/lifeagent_launchd_common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/lifeagent_host_runtime.sh COMMAND [--env-file PATH]

Commands:
  deploy     Build the shared app image once, run migration/test preflight,
             snapshot an installed host runtime outside the checkout, record
             the non-secret image id, create host secrets if absent, and
             install/restart the Ollama and Discord wake LaunchAgents.
  install    Snapshot the installed host runtime and install the LaunchAgents
             without build, migration, or test preflight.
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

previous_runtime_dir() {
  printf '%s/.runtime.previous\n' "$(lifeagent_install_root)"
}

remove_path_if_runtime_sibling() {
  remove_target="$1"
  case "$remove_target" in
    "$(lifeagent_install_root)"/.runtime.previous|"$(installed_runtime_dir)") ;;
    *)
      echo "refusing to remove unexpected runtime path: $remove_target" >&2
      exit 2
      ;;
  esac
  if [ -e "$remove_target" ]; then
    rm -rf "$remove_target"
  fi
}

restore_previous_runtime() {
  previous_dir="$(previous_runtime_dir)"
  runtime_dir="$(installed_runtime_dir)"
  if [ -d "$previous_dir" ]; then
    remove_path_if_runtime_sibling "$runtime_dir"
    mv "$previous_dir" "$runtime_dir"
    echo "restored previous installed runtime after failed install; current operation incomplete" >&2
  fi
}

cleanup_previous_runtime() {
  previous_dir="$(previous_runtime_dir)"
  if [ -d "$previous_dir" ]; then
    remove_path_if_runtime_sibling "$previous_dir"
  fi
}

SNAPSHOT_ROLLBACK_ENABLED="0"

rollback_snapshot_on_exit() {
  if [ "$SNAPSHOT_ROLLBACK_ENABLED" = "1" ]; then
    stop_discord_wake_for_snapshot || true
    restore_previous_runtime
  fi
}

begin_snapshot_rollback_window() {
  SNAPSHOT_ROLLBACK_ENABLED="1"
  trap rollback_snapshot_on_exit EXIT HUP INT TERM
}

finish_snapshot_rollback_window() {
  SNAPSHOT_ROLLBACK_ENABLED="0"
  cleanup_previous_runtime
  trap - EXIT HUP INT TERM
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

snapshot_installed_runtime() {
  runtime_dir="$(installed_runtime_dir)"
  source_artifacts="${LIFEAGENT_SOURCE_HOST_RUNTIME_DIR:-$CHECKOUT_REPO_DIR/.artifacts/discord-wake}"
  "$CHECKOUT_SCRIPT_DIR/lifeagent_runtime_snapshot.py" \
    --source-root "$CHECKOUT_REPO_DIR" \
    --source-artifacts "$source_artifacts" \
    --env-file "$ENV_FILE" \
    --runtime-dir "$runtime_dir"
}

stop_discord_wake_for_snapshot() {
  # Freeze the lightweight listener before preserving its SQLite outbox.
  plist="$(plist_path "$LIFEAGENT_DISCORD_WAKE_LABEL")"
  launchctl bootout "$(launchd_domain)/$LIFEAGENT_DISCORD_WAKE_LABEL" >/dev/null 2>&1 || true
  if [ -f "$plist" ]; then
    launchctl bootout "$(launchd_domain)" "$plist" >/dev/null 2>&1 || true
  fi
  discord_state="$(launchd_status "$LIFEAGENT_DISCORD_WAKE_LABEL")"
  if [ "$discord_state" = "running" ]; then
    echo "failed to stop Discord wake LaunchAgent before runtime snapshot" >&2
    report_launch_agent_failure "$LIFEAGENT_DISCORD_WAKE_LABEL"
    return 1
  fi
}

activate_installed_runtime() {
  REPO_DIR="$(installed_runtime_dir)"
  SCRIPT_DIR="$REPO_DIR/scripts"
  ENV_FILE="$REPO_DIR/.env"
}

resolve_uv() {
  if [ -n "${LIFEAGENT_UV:-}" ]; then
    case "$LIFEAGENT_UV" in
      /*) ;;
      *) echo "LIFEAGENT_UV must be an absolute path" >&2; exit 2 ;;
    esac
    [ -x "$LIFEAGENT_UV" ] || {
      echo "LIFEAGENT_UV does not point to an executable uv binary" >&2
      exit 127
    }
    printf '%s\n' "$LIFEAGENT_UV"
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return
  fi
  if [ -x "$CHECKOUT_REPO_DIR/.tools/uv-bootstrap/bin/uv" ]; then
    printf '%s\n' "$CHECKOUT_REPO_DIR/.tools/uv-bootstrap/bin/uv"
    return
  fi
  echo "uv is required to build the installed runtime virtualenv" >&2
  echo "Install uv or restore .tools/uv-bootstrap/bin/uv, then rerun this command" >&2
  exit 127
}

validate_runtime_venv_origin() {
  runtime_dir="$(installed_runtime_dir)"
  python_install_dir="$(lifeagent_install_root)/python"
  pyvenv_cfg="$runtime_dir/.venv/pyvenv.cfg"
  [ -f "$pyvenv_cfg" ] || return 0
  python3 - "$pyvenv_cfg" "$runtime_dir" "$python_install_dir" <<'PY' || exit 2
from pathlib import Path
import sys

cfg = Path(sys.argv[1])
runtime = Path(sys.argv[2]).resolve()
managed_python = Path(sys.argv[3]).resolve()

def is_allowed(path: Path) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    return resolved.is_relative_to(runtime) or resolved.is_relative_to(managed_python)

for line in cfg.read_text(encoding="utf-8").splitlines():
    if "=" not in line:
        continue
    key, value = (part.strip() for part in line.split("=", 1))
    if key not in {"home", "executable", "base-executable"}:
        continue
    candidate = Path(value)
    if candidate.is_absolute() and not is_allowed(candidate):
        raise SystemExit(f"installed runtime virtualenv points outside install root: {cfg}")
PY
}

install_runtime_venv() {
  runtime_dir="$(installed_runtime_dir)"
  uv_bin="$(resolve_uv)"
  python_install_dir="$(lifeagent_install_root)/python"
  ensure_private_dir "$(lifeagent_install_root)"
  ensure_private_dir "$python_install_dir"
  (
    cd "$runtime_dir"
    UV_PYTHON_INSTALL_DIR="$python_install_dir" \
      UV_PROJECT_ENVIRONMENT="$runtime_dir/.venv" \
      "$uv_bin" sync --locked --no-dev --project "$runtime_dir" --managed-python --python 3.12
  )
  validate_runtime_venv_origin
  validate_discord_python \
    "$runtime_dir/.venv/bin/python" \
    "$runtime_dir/.venv" \
    "$python_install_dir" || {
    echo "Installed runtime virtualenv validation failed; inspect $runtime_dir/.venv" >&2
    exit 2
  }
}

record_deployed_image_id() {
  image_id="$(docker image inspect lifeagent-app:local --format '{{.Id}}' 2>/dev/null || true)"
  if [ -z "$image_id" ]; then
    echo "could not resolve lifeagent-app:local image id after build" >&2
    exit 1
  fi
  image_file="$(deployed_image_id_file)"
  ensure_private_dir "$(dirname -- "$image_file")"
  tmp="${image_file}.tmp"
  rm -f "$tmp"
  (umask 177 && printf '%s\n' "$image_id" > "$tmp")
  mv "$tmp" "$image_file"
  chmod 0600 "$image_file"
  echo "recorded deployed image id: $image_file"
}

validate_deployed_image_marker() {
  marker_file="$(deployed_image_id_file)"
  if [ ! -f "$marker_file" ]; then
    echo "deployed image marker is missing: $marker_file" >&2
    exit 1
  fi
  expected_image_id="$(sed -n '1p' "$marker_file")"
  if [ -z "$expected_image_id" ]; then
    echo "deployed image marker is empty: $marker_file" >&2
    exit 1
  fi
  current_image_id="$(docker image inspect lifeagent-app:local --format '{{.Id}}' 2>/dev/null || true)"
  if [ "$current_image_id" != "$expected_image_id" ]; then
    echo "deployed image marker does not match local lifeagent-app:local image" >&2
    echo "marker: $marker_file" >&2
    exit 1
  fi
}

ensure_hmac_secret() {
  secret_file="$(discord_wake_hmac_secret_file)"
  ensure_private_dir "$(dirname -- "$secret_file")"
  if [ -f "$secret_file" ]; then
    chmod 0600 "$secret_file"
    echo "using existing Discord wake HMAC secret file: $secret_file"
    return
  fi
  command_required openssl
  tmp="${secret_file}.tmp"
  rm -f "$tmp"
  (umask 177 && openssl rand -hex 32 > "$tmp")
  mv "$tmp" "$secret_file"
  chmod 0600 "$secret_file"
  echo "created Discord wake HMAC secret file: $secret_file"
}

install_agents() {
  OLLAMA_HOST_VALUE="${OLLAMA_HOST_VALUE:-$DEFAULT_OLLAMA_HOST}"
  install_ollama_launch_agent
  install_discord_wake_launch_agent
  restart_launch_agent "$LIFEAGENT_OLLAMA_LABEL"
  restart_launch_agent "$LIFEAGENT_DISCORD_WAKE_LABEL"
  wait_for_launch_agent_running "$LIFEAGENT_OLLAMA_LABEL"
  wait_for_launch_agent_running "$LIFEAGENT_DISCORD_WAKE_LABEL"
}

activate_scheduled_runtime() {
  # The periodic academic worker must not depend on a conversational wake.
  cd "$REPO_DIR"
  docker compose --env-file "$ENV_FILE" up -d --no-build worker-academic-planner
  docker compose --env-file "$ENV_FILE" stop api
}

deploy() {
  command_required docker
  command_required launchctl
  command_required python3
  cd "$CHECKOUT_REPO_DIR"
  docker compose --env-file "$ENV_FILE" build api
  docker compose --env-file "$ENV_FILE" up -d --no-build postgres
  docker compose --env-file "$ENV_FILE" run --rm api alembic upgrade head
  run_test_preflight
  stop_discord_wake_for_snapshot
  snapshot_installed_runtime
  begin_snapshot_rollback_window
  activate_installed_runtime
  install_runtime_venv
  record_deployed_image_id
  validate_deployed_image_marker
  ensure_hmac_secret
  install_agents
  finish_snapshot_rollback_window
  # The native daemon remains the sole Discord ingress and the API stays cold.
  # The model-free academic worker remains resident for automatic schedules.
  activate_scheduled_runtime
}

install() {
  command_required docker
  command_required launchctl
  command_required python3
  stop_discord_wake_for_snapshot
  snapshot_installed_runtime
  begin_snapshot_rollback_window
  activate_installed_runtime
  install_runtime_venv
  validate_deployed_image_marker
  ensure_hmac_secret
  install_agents
  finish_snapshot_rollback_window
  activate_scheduled_runtime
}

status() {
  command_required launchctl
  discord_state="$(launchd_status "$LIFEAGENT_DISCORD_WAKE_LABEL")"
  echo "discord_wake_launchd=$discord_state"
  discord_status=0
  if [ "$discord_state" != "running" ]; then
    report_launch_agent_failure "$LIFEAGENT_DISCORD_WAKE_LABEL"
    discord_status=1
  fi
  set +e
  "$SCRIPT_DIR/ollama_qwen_status.sh" --env-file "$ENV_FILE"
  ollama_status="$?"
  set -e
  if [ "$ollama_status" -ne 0 ]; then
    return "$ollama_status"
  fi
  return "$discord_status"
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
    install
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
