# Shared helpers for LifeAgent macOS host LaunchAgents.

DEFAULT_OLLAMA_MODEL="qwen3-32gb:latest"
DEFAULT_OLLAMA_MODEL_DIGEST="d039cde69ac1f5a43d5134182adfefa65bdb533362a625b936e6171a53296eb3"
DEFAULT_LOCAL_BASE_URL="http://127.0.0.1:11434"
DEFAULT_OLLAMA_HOST="0.0.0.0:11434"
DEFAULT_STARTUP_TIMEOUT_SECONDS="30"
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

host_runtime_dir() {
  printf '%s\n' "${LIFEAGENT_HOST_RUNTIME_DIR:-$REPO_DIR/.artifacts/discord-wake}"
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

plist_path() {
  printf '%s/%s.plist\n' "$(launch_agents_dir)" "$1"
}

launchd_status() {
  if launchctl print "$(launchd_domain)/$1" >/dev/null 2>&1; then
    printf 'loaded\n'
  else
    printf 'unloaded\n'
  fi
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
  mkdir -p "$agents_dir" "$logs_dir"
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
    echo "Python 3.12 virtual environment is required at .venv/bin/python" >&2
    exit 127
  }
  "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' || {
    echo "Discord wake daemon requires Python 3.12" >&2
    exit 2
  }
  agents_dir="$(launch_agents_dir)"
  logs_dir="$(host_log_dir)"
  mkdir -p "$agents_dir" "$logs_dir"
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
  LIFEAGENT_RENDER_PYTHON_BIN="$(absolute_path "$python_bin")"
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
