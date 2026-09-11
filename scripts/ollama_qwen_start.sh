#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$(dirname -- "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"
PULL_MODEL="0"

. "$SCRIPT_DIR/lifeagent_launchd_common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/ollama_qwen_start.sh [--pull] [--env-file PATH]

Installs and kickstarts the host-side Ollama LaunchAgent, then verifies the
configured Qwen model. The script never pulls the model unless --pull is passed.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --pull)
      PULL_MODEL="1"
      shift
      ;;
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

if [ "${OLLAMA_MODEL+x}" = x ]; then OLLAMA_MODEL_ENV_SET="1"; else OLLAMA_MODEL_ENV_SET="0"; fi
if [ "${OLLAMA_MODEL_DIGEST+x}" = x ]; then
  OLLAMA_MODEL_DIGEST_ENV_SET="1"
  OLLAMA_MODEL_DIGEST_CONFIGURED="1"
else
  OLLAMA_MODEL_DIGEST_ENV_SET="0"
  OLLAMA_MODEL_DIGEST_CONFIGURED="0"
fi
if [ "${OLLAMA_BASE_URL+x}" = x ]; then OLLAMA_BASE_URL_ENV_SET="1"; else OLLAMA_BASE_URL_ENV_SET="0"; fi
if [ "${OLLAMA_LOCAL_BASE_URL+x}" = x ]; then OLLAMA_LOCAL_BASE_URL_ENV_SET="1"; else OLLAMA_LOCAL_BASE_URL_ENV_SET="0"; fi
if [ "${OLLAMA_HOST+x}" = x ]; then OLLAMA_HOST_ENV_SET="1"; else OLLAMA_HOST_ENV_SET="0"; fi
if [ "${OLLAMA_STARTUP_TIMEOUT_SECONDS+x}" = x ]; then OLLAMA_STARTUP_TIMEOUT_ENV_SET="1"; else OLLAMA_STARTUP_TIMEOUT_ENV_SET="0"; fi

OLLAMA_MODEL_VALUE="${OLLAMA_MODEL:-}"
OLLAMA_MODEL_DIGEST_VALUE="${OLLAMA_MODEL_DIGEST:-}"
OLLAMA_BASE_URL_VALUE="${OLLAMA_BASE_URL:-}"
OLLAMA_LOCAL_BASE_URL_VALUE="${OLLAMA_LOCAL_BASE_URL:-}"
OLLAMA_HOST_VALUE="${OLLAMA_HOST:-}"
OLLAMA_STARTUP_TIMEOUT_VALUE="${OLLAMA_STARTUP_TIMEOUT_SECONDS:-}"

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
      OLLAMA_MODEL)
        [ "$OLLAMA_MODEL_ENV_SET" = "1" ] || OLLAMA_MODEL_VALUE="$value"
        ;;
      OLLAMA_MODEL_DIGEST)
        if [ "$OLLAMA_MODEL_DIGEST_ENV_SET" != "1" ]; then
          OLLAMA_MODEL_DIGEST_VALUE="$value"
          OLLAMA_MODEL_DIGEST_CONFIGURED="1"
        fi
        ;;
      OLLAMA_BASE_URL)
        [ "$OLLAMA_BASE_URL_ENV_SET" = "1" ] || OLLAMA_BASE_URL_VALUE="$value"
        ;;
      OLLAMA_LOCAL_BASE_URL)
        [ "$OLLAMA_LOCAL_BASE_URL_ENV_SET" = "1" ] || OLLAMA_LOCAL_BASE_URL_VALUE="$value"
        ;;
      OLLAMA_HOST)
        [ "$OLLAMA_HOST_ENV_SET" = "1" ] || OLLAMA_HOST_VALUE="$value"
        ;;
      OLLAMA_STARTUP_TIMEOUT_SECONDS)
        [ "$OLLAMA_STARTUP_TIMEOUT_ENV_SET" = "1" ] || OLLAMA_STARTUP_TIMEOUT_VALUE="$value"
        ;;
    esac
  done < "$ENV_FILE"
}

verify_model_from_tags() {
  python3 - "$1" "$OLLAMA_MODEL_VALUE" "$OLLAMA_MODEL_DIGEST_VALUE" <<'PY'
import json
import sys

tags_path = sys.argv[1]
model_name = sys.argv[2]
expected_digest = sys.argv[3]
try:
    with open(tags_path, encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, json.JSONDecodeError):
    print("malformed Ollama tags response", file=sys.stderr)
    sys.exit(4)
models = payload.get("models")
if not isinstance(models, list):
    print("malformed Ollama tags response", file=sys.stderr)
    sys.exit(4)
for model in models:
    if isinstance(model, dict) and model.get("name") == model_name:
        digest = str(model.get("digest") or "")
        if expected_digest and digest != expected_digest:
            print("configured Qwen model digest does not match installed model", file=sys.stderr)
            sys.exit(3)
        print("configured Qwen model is installed")
        sys.exit(0)
print("configured Qwen model is not installed; rerun with --pull to install it", file=sys.stderr)
sys.exit(2)
PY
}

wait_for_api() {
  deadline="$(( $(date +%s) + OLLAMA_STARTUP_TIMEOUT_SECONDS_NORMALIZED ))"
  while :; do
    if fetch_json "$LOCAL_BASE_URL/api/tags" "$TAGS_FILE"; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      return 1
    fi
    sleep 1
  done
}

load_env_file
OLLAMA_MODEL_VALUE="${OLLAMA_MODEL_VALUE:-$DEFAULT_OLLAMA_MODEL}"
if [ "$OLLAMA_MODEL_DIGEST_CONFIGURED" = "0" ]; then
  OLLAMA_MODEL_DIGEST_VALUE="$DEFAULT_OLLAMA_MODEL_DIGEST"
fi
OLLAMA_HOST_VALUE="${OLLAMA_HOST_VALUE:-$DEFAULT_OLLAMA_HOST}"
OLLAMA_STARTUP_TIMEOUT_VALUE="${OLLAMA_STARTUP_TIMEOUT_VALUE:-$DEFAULT_STARTUP_TIMEOUT_SECONDS}"
LOCAL_BASE_URL="$(normalize_local_base_url)"

command_required ollama
command_required curl
command_required launchctl
command_required python3

OLLAMA_STARTUP_TIMEOUT_SECONDS_NORMALIZED="$(normalize_positive_seconds "$OLLAMA_STARTUP_TIMEOUT_VALUE")"
TAGS_FILE="$(mktemp -t lifeagent-ollama-tags.XXXXXX)"
cleanup() {
  rm -f "$TAGS_FILE"
}
trap cleanup EXIT

install_ollama_launch_agent
kickstart_launch_agent "$LIFEAGENT_OLLAMA_LABEL"
wait_for_launch_agent_running "$LIFEAGENT_OLLAMA_LABEL"

if ! wait_for_api; then
  echo "Ollama API did not become reachable within ${OLLAMA_STARTUP_TIMEOUT_SECONDS_NORMALIZED}s" >&2
  exit 1
fi
echo "Ollama API is reachable at $LOCAL_BASE_URL"

set +e
verify_model_from_tags "$TAGS_FILE"
verify_status="$?"
set -e

if [ "$verify_status" -eq 2 ] && [ "$PULL_MODEL" = "1" ]; then
  echo "pulling configured Qwen model: $OLLAMA_MODEL_VALUE"
  ollama pull "$OLLAMA_MODEL_VALUE"
  kickstart_launch_agent "$LIFEAGENT_OLLAMA_LABEL"
  if ! wait_for_api; then
    echo "Ollama API became unreachable after model pull" >&2
    exit 1
  fi
  verify_model_from_tags "$TAGS_FILE"
elif [ "$verify_status" -ne 0 ]; then
  exit "$verify_status"
fi

echo "Qwen runtime is ready"
