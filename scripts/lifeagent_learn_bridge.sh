#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$(dirname -- "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"
PYTHON_BIN="$REPO_DIR/.venv/bin/python"
LEARN_BRIDGE_DIR="${LEARN_BRIDGE_DIR:-${HOME:?HOME is required}/Library/Application Support/LifeAgent/learn-bridge}"
SECRET_FILE="${LEARN_BRIDGE_HMAC_SECRET_FILE:-$LEARN_BRIDGE_DIR/learn-bridge-hmac.key}"
PROFILE_DIR="${LEARN_BRIDGE_PROFILE_DIR:-$LEARN_BRIDGE_DIR/profile}"
HOST="${LEARN_BRIDGE_HOST:-127.0.0.1}"
PORT="${LEARN_BRIDGE_PORT:-8765}"
LEARN_BASE="${LEARN_BASE_URL:-https://learn.uwaterloo.ca}"
LAUNCHD_LABEL="com.lifeagent.learn-bridge"
PLIST_TEMPLATE="$SCRIPT_DIR/com.lifeagent.learn-bridge.plist.template"
PLIST_TARGET="${HOME:?HOME is required}/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
LOG_DIR="$LEARN_BRIDGE_DIR/logs"
BRIDGE_RUNTIME_DIR="${LEARN_BRIDGE_RUNTIME_DIR:-$LEARN_BRIDGE_DIR/runtime}"
STARTUP_WAIT_SECONDS="${LEARN_BRIDGE_STARTUP_WAIT_SECONDS:-45}"

usage() {
  cat <<'USAGE'
Usage: scripts/lifeagent_learn_bridge.sh COMMAND [options]

Commands:
  run       Run the loopback-only authenticated LEARN bridge.
  login     Open headed Chromium for manual Waterloo SSO/MFA.
  health    Query signed bridge health without printing secrets.
  verify    Prove the manual session survives headless relaunch and exposes bounded data.
  install   Install and start the dedicated per-user macOS LaunchAgent.
  status    Report whether the dedicated LaunchAgent is loaded.
  uninstall Stop and remove the LaunchAgent; keep the profile and secret.

Options:
  --env-file PATH
  --python PATH
  --secret-file PATH
  --profile-dir PATH
  --host HOST
  --port PORT
USAGE
}

COMMAND="${1:-}"
if [ -n "$COMMAND" ]; then
  shift
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="${2:?missing --env-file value}"; shift 2 ;;
    --python) PYTHON_BIN="${2:?missing --python value}"; shift 2 ;;
    --secret-file) SECRET_FILE="${2:?missing --secret-file value}"; shift 2 ;;
    --profile-dir) PROFILE_DIR="${2:?missing --profile-dir value}"; shift 2 ;;
    --host) HOST="${2:?missing --host value}"; shift 2 ;;
    --port) PORT="${2:?missing --port value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$COMMAND" in
  run|login|health|verify|install|status|uninstall) ;;
  "") usage >&2; exit 2 ;;
  *) echo "unknown command: $COMMAND" >&2; usage >&2; exit 2 ;;
esac

case "$PYTHON_BIN" in
  /*) ;;
  *) echo "LEARN bridge Python path must be absolute" >&2; exit 2 ;;
esac
[ -x "$PYTHON_BIN" ] || {
  echo "LEARN bridge Python executable is unavailable" >&2
  exit 127
}

strip_simple_quotes() {
  case "$1" in
    \"*\") value="${1#\"}"; printf '%s\n' "${value%\"}" ;;
    \'*\') value="${1#\'}"; printf '%s\n' "${value%\'}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

if [ -f "$ENV_FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ""|\#*) continue ;;
      export\ *) line="${line#export }" ;;
    esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    value="$(strip_simple_quotes "${line#*=}")"
    case "$key" in
      LEARN_BRIDGE_HOST) HOST="$value" ;;
      LEARN_BRIDGE_PORT) PORT="$value" ;;
      LEARN_BRIDGE_PROFILE_DIR) PROFILE_DIR="$value" ;;
      LEARN_BRIDGE_HMAC_SECRET_FILE) SECRET_FILE="$value" ;;
      LEARN_BASE_URL) LEARN_BASE="$value" ;;
      LEARN_BRIDGE_MAX_REQUEST_BYTES|LEARN_BRIDGE_MAX_RESPONSE_BYTES|\
      LEARN_BRIDGE_MAX_CLOCK_SKEW_SECONDS|LEARN_BRIDGE_NONCE_TTL_SECONDS|\
      LEARN_BRIDGE_TIMEOUT_SECONDS|LEARN_ANNOUNCEMENT_LOOKBACK_HOURS|\
      LEARN_BRIDGE_MAX_COURSES|LEARN_BRIDGE_MAX_SCHEDULED_ITEMS|\
      LEARN_BRIDGE_MAX_ANNOUNCEMENTS|LEARN_ANNOUNCEMENT_MAX_BODY_CHARS)
        export "$key=$value"
        ;;
    esac
  done < "$ENV_FILE"
fi

case "$HOST" in
  127.*|::1|localhost) ;;
  *) echo "LEARN bridge host must be loopback-only" >&2; exit 2 ;;
esac
case "$STARTUP_WAIT_SECONDS" in
  ""|*[!0-9]*) echo "LEARN_BRIDGE_STARTUP_WAIT_SECONDS must be an integer" >&2; exit 2 ;;
esac
if [ "$STARTUP_WAIT_SECONDS" -gt 120 ]; then
  echo "LEARN_BRIDGE_STARTUP_WAIT_SECONDS must be <= 120" >&2
  exit 2
fi

mkdir -p "$(dirname -- "$SECRET_FILE")" "$PROFILE_DIR"
chmod 0700 "$(dirname -- "$SECRET_FILE")" "$PROFILE_DIR"
if [ ! -f "$SECRET_FILE" ]; then
  umask 077
  "$PYTHON_BIN" - <<'PY' > "$SECRET_FILE"
import secrets

print(secrets.token_hex(32))
PY
  chmod 0600 "$SECRET_FILE"
  echo "created LEARN bridge HMAC secret file"
fi
chmod 0600 "$SECRET_FILE"

export LIFEAGENT_REPOSITORY_ROOT="$REPO_DIR"

launchd_domain="gui/$(id -u)"

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
  if [ -x "$REPO_DIR/.tools/uv-bootstrap/bin/uv" ]; then
    printf '%s\n' "$REPO_DIR/.tools/uv-bootstrap/bin/uv"
    return
  fi
  echo "uv is required to install the dedicated LEARN bridge runtime" >&2
  exit 127
}

if [ "$COMMAND" = "install" ]; then
  command -v launchctl >/dev/null 2>&1 || {
    echo "launchctl is required to install the LEARN bridge" >&2
    exit 127
  }
  [ -f "$PLIST_TEMPLATE" ] || {
    echo "LEARN bridge LaunchAgent template is unavailable" >&2
    exit 2
  }
  uv_bin="$(resolve_uv)"
  launchctl bootout "$launchd_domain/$LAUNCHD_LABEL" >/dev/null 2>&1 || true
  mkdir -p \
    "$BRIDGE_RUNTIME_DIR/app/host" \
    "$BRIDGE_RUNTIME_DIR/scripts" \
    "$LEARN_BRIDGE_DIR/python" \
    "$LEARN_BRIDGE_DIR/uv-cache" \
    "$(dirname -- "$PLIST_TARGET")" \
    "$LOG_DIR"
  chmod 0700 \
    "$LEARN_BRIDGE_DIR" \
    "$BRIDGE_RUNTIME_DIR" \
    "$BRIDGE_RUNTIME_DIR/app" \
    "$BRIDGE_RUNTIME_DIR/app/host" \
    "$BRIDGE_RUNTIME_DIR/scripts" \
    "$LEARN_BRIDGE_DIR/python" \
    "$LEARN_BRIDGE_DIR/uv-cache" \
    "$LOG_DIR"
  install -m 0600 "$REPO_DIR/app/__init__.py" "$BRIDGE_RUNTIME_DIR/app/__init__.py"
  "$PYTHON_BIN" - "$BRIDGE_RUNTIME_DIR/app/host/__init__.py" <<'PY'
from pathlib import Path
import sys

Path(sys.argv[1]).write_text('"""Dedicated LEARN bridge host package."""\n', encoding="utf-8")
PY
  chmod 0600 "$BRIDGE_RUNTIME_DIR/app/host/__init__.py"
  install -m 0600 \
    "$REPO_DIR/app/host/learn_bridge.py" \
    "$BRIDGE_RUNTIME_DIR/app/host/learn_bridge.py"
  install -m 0700 "$SCRIPT_DIR/lifeagent_learn_bridge.sh" "$BRIDGE_RUNTIME_DIR/scripts/"
  install -m 0600 "$REPO_DIR/pyproject.toml" "$REPO_DIR/uv.lock" "$BRIDGE_RUNTIME_DIR/"
  DEPLOYED_SCRIPT_DIR="$BRIDGE_RUNTIME_DIR/scripts"
  DEPLOYED_ENV_FILE="$BRIDGE_RUNTIME_DIR/.env"
  DEPLOYED_PYTHON_BIN="$BRIDGE_RUNTIME_DIR/.venv/bin/python"
  "$PYTHON_BIN" - "$ENV_FILE" "$DEPLOYED_ENV_FILE" "$HOST" "$PORT" "$LEARN_BASE" <<'PY'
from pathlib import Path
import sys

source, target, host, port, learn_base = sys.argv[1:]
allowed = {
    "LEARN_BRIDGE_MAX_REQUEST_BYTES",
    "LEARN_BRIDGE_MAX_RESPONSE_BYTES",
    "LEARN_BRIDGE_MAX_CLOCK_SKEW_SECONDS",
    "LEARN_BRIDGE_NONCE_TTL_SECONDS",
    "LEARN_BRIDGE_TIMEOUT_SECONDS",
    "LEARN_ANNOUNCEMENT_LOOKBACK_HOURS",
    "LEARN_BRIDGE_MAX_COURSES",
    "LEARN_BRIDGE_MAX_SCHEDULED_ITEMS",
    "LEARN_BRIDGE_MAX_ANNOUNCEMENTS",
    "LEARN_ANNOUNCEMENT_MAX_BODY_CHARS",
}
values = {
    "LEARN_BRIDGE_HOST": host,
    "LEARN_BRIDGE_PORT": port,
    "LEARN_BASE_URL": learn_base,
}
source_path = Path(source)
if source_path.is_file():
    for raw_line in source_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[7:]
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in allowed:
            values[key] = value
Path(target).write_text(
    "".join(f"{key}={value}\n" for key, value in sorted(values.items())),
    encoding="utf-8",
)
PY
  chmod 0600 "$DEPLOYED_ENV_FILE"
  UV_CACHE_DIR="$LEARN_BRIDGE_DIR/uv-cache" \
    UV_PYTHON_INSTALL_DIR="$LEARN_BRIDGE_DIR/python" \
    UV_PROJECT_ENVIRONMENT="$BRIDGE_RUNTIME_DIR/.venv" \
    "$uv_bin" sync --locked --no-dev --project "$BRIDGE_RUNTIME_DIR" \
      --managed-python --python 3.12
  (
    cd "$BRIDGE_RUNTIME_DIR"
    "$DEPLOYED_PYTHON_BIN" -c 'import playwright, pydantic; import app.host.learn_bridge'
  ) || {
    echo "dedicated LEARN bridge runtime validation failed" >&2
    exit 2
  }
  "$PYTHON_BIN" - "$PLIST_TEMPLATE" "$PLIST_TARGET" \
    "$DEPLOYED_SCRIPT_DIR" "$DEPLOYED_ENV_FILE" "$SECRET_FILE" "$PROFILE_DIR" \
    "$DEPLOYED_PYTHON_BIN" "$BRIDGE_RUNTIME_DIR" "$LOG_DIR" <<'PY'
from pathlib import Path
from xml.sax.saxutils import escape
import sys

template, target, script_dir, env_file, secret_file, profile_dir, python_bin, repo_dir, log_dir = (
    sys.argv[1:]
)
text = Path(template).read_text(encoding="utf-8")
for marker, value in {
    "__SCRIPT_DIR__": script_dir,
    "__ENV_FILE__": env_file,
    "__LEARN_HMAC_SECRET_FILE__": secret_file,
    "__LEARN_PROFILE_DIR__": profile_dir,
    "__PYTHON_BIN__": python_bin,
    "__REPO_DIR__": repo_dir,
    "__LOG_DIR__": log_dir,
}.items():
    text = text.replace(marker, escape(value))
Path(target).write_text(text, encoding="utf-8")
PY
  chmod 0600 "$PLIST_TARGET"
  : > "$LOG_DIR/learn-bridge.stdout.log"
  : > "$LOG_DIR/learn-bridge.stderr.log"
  chmod 0600 "$LOG_DIR/learn-bridge.stdout.log" "$LOG_DIR/learn-bridge.stderr.log"
  launchctl bootstrap "$launchd_domain" "$PLIST_TARGET"
  launchctl kickstart -k "$launchd_domain/$LAUNCHD_LABEL"
  echo "learn_bridge_launchd=installed"
  elapsed=0
  health_output=""
  while [ "$elapsed" -lt "$STARTUP_WAIT_SECONDS" ]; do
    set +e
    health_output="$(
      "$DEPLOYED_SCRIPT_DIR/lifeagent_learn_bridge.sh" health \
        --env-file "$DEPLOYED_ENV_FILE" \
        --secret-file "$SECRET_FILE" \
        --profile-dir "$PROFILE_DIR" \
        --python "$DEPLOYED_PYTHON_BIN" \
        --host "$HOST" \
        --port "$PORT" 2>/dev/null
    )"
    health_status="$?"
    set -e
    if [ "$health_status" -eq 0 ]; then
      printf '%s\n' "$health_output"
      exit 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  if [ "$STARTUP_WAIT_SECONDS" -gt 0 ]; then
    echo "LEARN bridge did not become reachable within ${STARTUP_WAIT_SECONDS}s" >&2
    echo "inspect $LOG_DIR/learn-bridge.stderr.log" >&2
    exit 1
  fi
  exit 0
fi

if [ "$COMMAND" = "status" ]; then
  if launchctl print "$launchd_domain/$LAUNCHD_LABEL" >/dev/null 2>&1; then
    echo "learn_bridge_launchd=loaded"
    exit 0
  fi
  echo "learn_bridge_launchd=not_loaded"
  exit 1
fi

if [ "$COMMAND" = "uninstall" ]; then
  launchctl bootout "$launchd_domain/$LAUNCHD_LABEL" >/dev/null 2>&1 || true
  rm -f "$PLIST_TARGET"
  echo "learn_bridge_launchd=uninstalled profile_and_secret_preserved=true"
  exit 0
fi

MODULE_COMMAND="$COMMAND"
if [ "$MODULE_COMMAND" = "run" ]; then
  MODULE_COMMAND="serve"
fi

run_module() {
  "$PYTHON_BIN" -m app.host.learn_bridge "$MODULE_COMMAND" \
    --secret-file "$SECRET_FILE" \
    --profile-dir "$PROFILE_DIR" \
    --host "$HOST" \
    --port "$PORT" \
    --repository-root "$REPO_DIR" \
    --learn-base-url "$LEARN_BASE"
}

if { [ "$COMMAND" = "login" ] || [ "$COMMAND" = "verify" ]; } && \
  launchctl print "$launchd_domain/$LAUNCHD_LABEL" >/dev/null 2>&1; then
  launchctl bootout "$launchd_domain/$LAUNCHD_LABEL" >/dev/null 2>&1 || true
  set +e
  run_module
  foreground_status="$?"
  set -e
  if [ -f "$PLIST_TARGET" ]; then
    launchctl bootstrap "$launchd_domain" "$PLIST_TARGET"
    launchctl kickstart -k "$launchd_domain/$LAUNCHD_LABEL"
  fi
  exit "$foreground_status"
fi

run_module
