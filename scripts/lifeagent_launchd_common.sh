# Shared helpers for LifeAgent macOS host LaunchAgents.

DEFAULT_OLLAMA_MODEL="qwen3-32gb:latest"
DEFAULT_OLLAMA_MODEL_DIGEST="d039cde69ac1f5a43d5134182adfefa65bdb533362a625b936e6171a53296eb3"
DEFAULT_LOCAL_BASE_URL="http://127.0.0.1:11434"
DEFAULT_OLLAMA_HOST="0.0.0.0:11434"
DEFAULT_STARTUP_TIMEOUT_SECONDS="30"
DEFAULT_LAUNCHD_READINESS_TIMEOUT_SECONDS="10"
DEFAULT_LAUNCHD_STABILITY_SECONDS="2"
LIFEAGENT_OLLAMA_LABEL="${LIFEAGENT_OLLAMA_LABEL:-com.lifeagent.ollama}"
LIFEAGENT_DISCORD_WAKE_LABEL="${LIFEAGENT_DISCORD_WAKE_LABEL:-com.lifeagent.discord-wake}"

strip_simple_quotes() {
  case "$1" in
    \"*\")
      value="${1#\"}"
      printf '%s\n' "${value%\"}"
      ;;
    \'*\')
      value="${1#\'}"
      printf '%s\n' "${value%\'}"
      ;;
    *)
      printf '%s\n' "$1"
      ;;
  esac
}

normalize_local_base_url() {
  base_url="$OLLAMA_LOCAL_BASE_URL_VALUE"
  if [ -z "$base_url" ] && [ -n "$OLLAMA_BASE_URL_VALUE" ]; then
    case "$OLLAMA_BASE_URL_VALUE" in
      http://host.docker.internal:*)
        port="${OLLAMA_BASE_URL_VALUE#http://host.docker.internal:}"
        base_url="http://127.0.0.1:${port%%/*}"
        ;;
      http://localhost:*|http://127.0.0.1:*)
        base_url="$OLLAMA_BASE_URL_VALUE"
        ;;
    esac
  fi
  base_url="${base_url:-$DEFAULT_LOCAL_BASE_URL}"
  printf '%s\n' "${base_url%/}"
}

normalize_positive_seconds() {
  python3 - "$1" <<'PY'
import math
import sys

try:
    value = float(sys.argv[1])
except ValueError:
    print("OLLAMA_STARTUP_TIMEOUT_SECONDS must be positive", file=sys.stderr)
    sys.exit(2)
if not math.isfinite(value) or value <= 0:
    print("OLLAMA_STARTUP_TIMEOUT_SECONDS must be positive", file=sys.stderr)
    sys.exit(2)
print(math.ceil(value))
PY
}

command_required() {
  command -v "$1" >/dev/null || {
    echo "$1 is required on the host PATH" >&2
    exit 127
  }
}

absolute_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
}

lexical_absolute_path() {
  python3 - "$1" <<'PY'
import os
import sys

print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
}

validate_discord_python() {
  validation_python="$1"
  validation_venv="$2"
  validation_python_root="${3:-}"
  (
    cd "$REPO_DIR"
    "$validation_python" - "$validation_venv" "$validation_python_root" <<'PY'
from pathlib import Path
import sys

if sys.version_info[:2] != (3, 12):
    raise SystemExit("Python 3.12 is required")
if Path(sys.prefix).resolve() != Path(sys.argv[1]).resolve():
    raise SystemExit("interpreter did not start inside the installed runtime virtualenv")
if sys.argv[2]:
    managed_root = Path(sys.argv[2]).resolve()
    base_prefix = Path(sys.base_prefix).resolve()
    if not base_prefix.is_relative_to(managed_root):
        raise SystemExit("interpreter base is outside the installed managed Python directory")
import app.host.daemon  # noqa: E402,F401
import httpx  # noqa: E402,F401
import websockets  # noqa: E402,F401
PY
  )
}

fetch_json() {
  curl -fsS --max-time 2 "$1" > "$2" 2>/dev/null
}

launchd_domain() {
  printf 'gui/%s\n' "$UID"
}

launch_agents_dir() {
  if [ -n "${LIFEAGENT_LAUNCH_AGENTS_DIR:-}" ]; then
    printf '%s\n' "$LIFEAGENT_LAUNCH_AGENTS_DIR"
  else
    printf '%s\n' "${HOME:?HOME is required}/Library/LaunchAgents"
  fi
}

lifeagent_install_root() {
  printf '%s\n' "${LIFEAGENT_INSTALL_ROOT:-${HOME:?HOME is required}/Library/Application Support/LifeAgent}"
}

installed_runtime_dir() {
  printf '%s/runtime\n' "$(lifeagent_install_root)"
}

host_runtime_dir() {
  printf '%s/.artifacts/discord-wake\n' "$(installed_runtime_dir)"
}

host_log_dir() {
  printf '%s\n' "${LIFEAGENT_HOST_LOG_DIR:-${HOME:?HOME is required}/Library/Logs/LifeAgent}"
}

deployed_image_id_file() {
  printf '%s\n' "${LIFEAGENT_DEPLOYED_IMAGE_ID_FILE:-$(host_runtime_dir)/deployed-image-id}"
}

discord_wake_hmac_secret_file() {
  printf '%s\n' "${LIFEAGENT_DISCORD_WAKE_HMAC_SECRET_FILE:-$(host_runtime_dir)/discord-wake-hmac.key}"
}

ensure_private_dir() {
  private_dir="$1"
  mkdir -p "$private_dir"
  chmod 0700 "$private_dir"
}

plist_path() {
  printf '%s/%s.plist\n' "$(launch_agents_dir)" "$1"
}

launchd_status() {
  launchd_output="$(launchctl print "$(launchd_domain)/$1" 2>/dev/null)" || {
    printf 'unloaded\n'
    return 0
  }
  launchd_raw_state="$({
    printf '%s\n' "$launchd_output" |
      sed -n 's/^[[:space:]]*state = //p'
  } | head -n 1)"
  if [ "$launchd_raw_state" = "running" ]; then
    printf 'running\n'
    return 0
  fi
  launchd_exit_status="$({
    printf '%s\n' "$launchd_output" |
      sed -n -E 's/^[[:space:]]*last exit (code|status) = (-?[0-9]+).*$/\2/p'
  } | head -n 1)"
  case "$launchd_exit_status" in
    ""|0)
      printf 'loaded/inactive\n'
      ;;
    *)
      printf 'crash-looping\n'
      ;;
  esac
}

launchd_last_exit_status() {
  launchd_output="$(launchctl print "$(launchd_domain)/$1" 2>/dev/null)" || return 1
  launchd_exit_status="$({
    printf '%s\n' "$launchd_output" |
      sed -n -E 's/^[[:space:]]*last exit (code|status) = (-?[0-9]+).*$/\2/p'
  } | head -n 1)"
  [ -n "$launchd_exit_status" ] || return 1
  printf '%s\n' "$launchd_exit_status"
}

launchd_log_stem() {
  case "$1" in
    "$LIFEAGENT_DISCORD_WAKE_LABEL") printf 'discord-wake\n' ;;
    "$LIFEAGENT_OLLAMA_LABEL") printf 'ollama\n' ;;
    *) printf 'unknown\n' ;;
  esac
}

report_launch_agent_failure() {
  failed_label="$1"
  failed_state="$(launchd_status "$failed_label")"
  failed_last_exit="$(launchd_last_exit_status "$failed_label" 2>/dev/null || printf 'unavailable\n')"
  failed_log_stem="$(launchd_log_stem "$failed_label")"
  failed_log_dir="$(host_log_dir)"
  echo "LaunchAgent $failed_label is not healthy: state=$failed_state last_exit_status=$failed_last_exit" >&2
  echo "Inspect logs: $failed_log_dir/$failed_log_stem.stdout.log and $failed_log_dir/$failed_log_stem.stderr.log" >&2
}

wait_for_launch_agent_running() {
  wait_label="$1"
  wait_timeout="${LIFEAGENT_LAUNCHD_READINESS_TIMEOUT_SECONDS:-$DEFAULT_LAUNCHD_READINESS_TIMEOUT_SECONDS}"
  wait_stability="${LIFEAGENT_LAUNCHD_STABILITY_SECONDS:-$DEFAULT_LAUNCHD_STABILITY_SECONDS}"
  case "$wait_timeout" in
    ""|*[!0-9]*)
      echo "LIFEAGENT_LAUNCHD_READINESS_TIMEOUT_SECONDS must be a nonnegative integer" >&2
      return 2
      ;;
  esac
  case "$wait_stability" in
    ""|*[!0-9]*)
      echo "LIFEAGENT_LAUNCHD_STABILITY_SECONDS must be a nonnegative integer" >&2
      return 2
      ;;
  esac
  wait_deadline="$(( $(date +%s) + wait_timeout ))"
  while [ "$(launchd_status "$wait_label")" != "running" ]; do
    if [ "$(date +%s)" -ge "$wait_deadline" ]; then
      report_launch_agent_failure "$wait_label"
      return 1
    fi
    sleep 1
  done
  if [ "$wait_stability" -gt 0 ]; then
    sleep "$wait_stability"
  fi
  if [ "$(launchd_status "$wait_label")" != "running" ]; then
    report_launch_agent_failure "$wait_label"
    return 1
  fi
  echo "LaunchAgent is running: $wait_label"
}

render_launchd_template() {
  template="$1"
  destination="$2"
  tmp="${destination}.tmp"
  python3 - "$template" "$tmp" <<'PY'
import os
import sys
from xml.sax.saxutils import escape
from pathlib import Path

template = Path(sys.argv[1])
destination = Path(sys.argv[2])
mapping = {
    "__REPO_DIR__": escape(os.environ["LIFEAGENT_RENDER_REPO_DIR"]),
    "__SCRIPT_DIR__": escape(os.environ["LIFEAGENT_RENDER_SCRIPT_DIR"]),
    "__OLLAMA_BIN__": escape(os.environ.get("LIFEAGENT_RENDER_OLLAMA_BIN", "")),
    "__OLLAMA_HOST__": escape(os.environ.get("LIFEAGENT_RENDER_OLLAMA_HOST", "")),
    "__ENV_FILE__": escape(os.environ.get("LIFEAGENT_RENDER_ENV_FILE", "")),
    "__IMAGE_ID_FILE__": escape(os.environ.get("LIFEAGENT_RENDER_IMAGE_ID_FILE", "")),
    "__HMAC_SECRET_FILE__": escape(os.environ.get("LIFEAGENT_RENDER_HMAC_SECRET_FILE", "")),
    "__PYTHON_BIN__": escape(os.environ.get("LIFEAGENT_RENDER_PYTHON_BIN", "")),
    "__DOCKER_BIN__": escape(os.environ.get("LIFEAGENT_RENDER_DOCKER_BIN", "")),
    "__LAUNCHCTL_BIN__": escape(os.environ.get("LIFEAGENT_RENDER_LAUNCHCTL_BIN", "")),
    "__LOG_DIR__": escape(os.environ["LIFEAGENT_RENDER_LOG_DIR"]),
}
content = template.read_text(encoding="utf-8")
for placeholder, value in mapping.items():
    content = content.replace(placeholder, value)
destination.write_text(content, encoding="utf-8")
PY
  mv "$tmp" "$destination"
  chmod 0644 "$destination"
}

install_ollama_launch_agent() {
  command_required ollama
  command_required launchctl
  command_required python3
  agents_dir="$(launch_agents_dir)"
  logs_dir="$(host_log_dir)"
  mkdir -p "$agents_dir"
  ensure_private_dir "$logs_dir"
  plist="$(plist_path "$LIFEAGENT_OLLAMA_LABEL")"
  export LIFEAGENT_RENDER_REPO_DIR
  export LIFEAGENT_RENDER_SCRIPT_DIR
  export LIFEAGENT_RENDER_OLLAMA_BIN
  export LIFEAGENT_RENDER_OLLAMA_HOST
  export LIFEAGENT_RENDER_LOG_DIR
  LIFEAGENT_RENDER_REPO_DIR="$(absolute_path "$REPO_DIR")"
  LIFEAGENT_RENDER_SCRIPT_DIR="$(absolute_path "$SCRIPT_DIR")"
  LIFEAGENT_RENDER_OLLAMA_BIN="$(absolute_path "$(command -v ollama)")"
  LIFEAGENT_RENDER_OLLAMA_HOST="$OLLAMA_HOST_VALUE"
  LIFEAGENT_RENDER_LOG_DIR="$(absolute_path "$logs_dir")"
  render_launchd_template "$SCRIPT_DIR/com.lifeagent.ollama.plist.template" "$plist"
  launchctl bootout "$(launchd_domain)" "$plist" >/dev/null 2>&1 || true
  launchctl bootstrap "$(launchd_domain)" "$plist"
  launchctl enable "$(launchd_domain)/$LIFEAGENT_OLLAMA_LABEL" >/dev/null 2>&1 || true
  echo "installed LaunchAgent: $LIFEAGENT_OLLAMA_LABEL"
}

install_discord_wake_launch_agent() {
  command_required launchctl
  command_required python3
  command_required docker
  command_required ollama
  python_bin="$REPO_DIR/.venv/bin/python"
  [ -x "$python_bin" ] || {
    echo "Python 3.12 virtual environment is required at the installed runtime .venv/bin/python" >&2
    exit 127
  }
  expected_venv="$REPO_DIR/.venv"
  expected_python_root="$(lifeagent_install_root)/python"
  validate_discord_python "$python_bin" "$expected_venv" "$expected_python_root" || {
    echo "Discord wake daemon virtualenv validation failed; recreate .venv and install runtime dependencies" >&2
    exit 2
  }
  agents_dir="$(launch_agents_dir)"
  logs_dir="$(host_log_dir)"
  mkdir -p "$agents_dir"
  ensure_private_dir "$logs_dir"
  plist="$(plist_path "$LIFEAGENT_DISCORD_WAKE_LABEL")"
  export LIFEAGENT_RENDER_REPO_DIR
  export LIFEAGENT_RENDER_SCRIPT_DIR
  export LIFEAGENT_RENDER_ENV_FILE
  export LIFEAGENT_RENDER_IMAGE_ID_FILE
  export LIFEAGENT_RENDER_HMAC_SECRET_FILE
  export LIFEAGENT_RENDER_PYTHON_BIN
  export LIFEAGENT_RENDER_DOCKER_BIN
  export LIFEAGENT_RENDER_LAUNCHCTL_BIN
  export LIFEAGENT_RENDER_OLLAMA_BIN
  export LIFEAGENT_RENDER_LOG_DIR
  LIFEAGENT_RENDER_REPO_DIR="$(absolute_path "$REPO_DIR")"
  LIFEAGENT_RENDER_SCRIPT_DIR="$(absolute_path "$SCRIPT_DIR")"
  LIFEAGENT_RENDER_ENV_FILE="$(absolute_path "$ENV_FILE")"
  LIFEAGENT_RENDER_IMAGE_ID_FILE="$(absolute_path "$(deployed_image_id_file)")"
  LIFEAGENT_RENDER_HMAC_SECRET_FILE="$(absolute_path "$(discord_wake_hmac_secret_file)")"
  LIFEAGENT_RENDER_PYTHON_BIN="$(lexical_absolute_path "$python_bin")"
  LIFEAGENT_RENDER_DOCKER_BIN="$(absolute_path "$(command -v docker)")"
  LIFEAGENT_RENDER_LAUNCHCTL_BIN="$(absolute_path "$(command -v launchctl)")"
  LIFEAGENT_RENDER_OLLAMA_BIN="$(absolute_path "$(command -v ollama)")"
  LIFEAGENT_RENDER_LOG_DIR="$(absolute_path "$logs_dir")"
  render_launchd_template "$SCRIPT_DIR/com.lifeagent.discord-wake.plist.template" "$plist"
  launchctl bootout "$(launchd_domain)" "$plist" >/dev/null 2>&1 || true
  launchctl bootstrap "$(launchd_domain)" "$plist"
  launchctl enable "$(launchd_domain)/$LIFEAGENT_DISCORD_WAKE_LABEL" >/dev/null 2>&1 || true
  echo "installed LaunchAgent: $LIFEAGENT_DISCORD_WAKE_LABEL"
}

kickstart_launch_agent() {
  launchctl kickstart "$(launchd_domain)/$1"
}

restart_launch_agent() {
  launchctl kickstart -k "$(launchd_domain)/$1"
}

uninstall_launch_agent() {
  label="$1"
  plist="$(plist_path "$label")"
  launchctl bootout "$(launchd_domain)/$label" >/dev/null 2>&1 || true
  launchctl bootout "$(launchd_domain)" "$plist" >/dev/null 2>&1 || true
  rm -f "$plist"
  echo "uninstalled LaunchAgent: $label"
}
