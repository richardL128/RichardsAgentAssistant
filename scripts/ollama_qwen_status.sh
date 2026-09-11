#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$(dirname -- "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"

. "$SCRIPT_DIR/lifeagent_launchd_common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/ollama_qwen_status.sh [--env-file PATH]

Reports Ollama LaunchAgent state, local API reachability, configured Qwen
installation/digest state, and whether the configured model is resident.
USAGE
}

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

OLLAMA_MODEL_VALUE="${OLLAMA_MODEL:-}"
OLLAMA_MODEL_DIGEST_VALUE="${OLLAMA_MODEL_DIGEST:-}"
OLLAMA_BASE_URL_VALUE="${OLLAMA_BASE_URL:-}"
OLLAMA_LOCAL_BASE_URL_VALUE="${OLLAMA_LOCAL_BASE_URL:-}"

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
    print("model_installed=unknown")
    print("digest_match=unknown")
    sys.exit(4)
models = payload.get("models")
if not isinstance(models, list):
    print("model_installed=unknown")
    print("digest_match=unknown")
    sys.exit(4)
for model in models:
    if isinstance(model, dict) and model.get("name") == model_name:
        digest = str(model.get("digest") or "")
        print("model_installed=yes")
        if expected_digest:
            if digest == expected_digest:
                print("digest_match=yes")
                sys.exit(0)
            print("digest_match=no")
            sys.exit(3)
        print("digest_match=unchecked")
        sys.exit(0)
print("model_installed=no")
print("digest_match=unchecked")
sys.exit(2)
PY
}

model_residency_from_ps() {
  python3 - "$1" "$OLLAMA_MODEL_VALUE" <<'PY'
import json
import sys

ps_path = sys.argv[1]
model_name = sys.argv[2]
try:
    with open(ps_path, encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, json.JSONDecodeError):
    print("qwen_resident=unknown")
    sys.exit(4)
models = payload.get("models")
if not isinstance(models, list):
    print("qwen_resident=unknown")
    sys.exit(4)
for model in models:
    if isinstance(model, dict) and model.get("name") == model_name:
        print("qwen_resident=yes")
        sys.exit(0)
print("qwen_resident=no")
sys.exit(0)
PY
}

load_env_file
OLLAMA_MODEL_VALUE="${OLLAMA_MODEL_VALUE:-$DEFAULT_OLLAMA_MODEL}"
if [ "$OLLAMA_MODEL_DIGEST_CONFIGURED" = "0" ]; then
  OLLAMA_MODEL_DIGEST_VALUE="$DEFAULT_OLLAMA_MODEL_DIGEST"
fi
LOCAL_BASE_URL="$(normalize_local_base_url)"

command_required curl
command_required launchctl
command_required python3

OLLAMA_LAUNCHD_STATE="$(launchd_status "$LIFEAGENT_OLLAMA_LABEL")"
echo "ollama_launchd=$OLLAMA_LAUNCHD_STATE"
OLLAMA_LAUNCHD_STATUS=0
if [ "$OLLAMA_LAUNCHD_STATE" != "running" ]; then
  report_launch_agent_failure "$LIFEAGENT_OLLAMA_LABEL"
  OLLAMA_LAUNCHD_STATUS=1
fi

TAGS_FILE="$(mktemp -t lifeagent-ollama-tags.XXXXXX)"
PS_FILE="$(mktemp -t lifeagent-ollama-ps.XXXXXX)"
cleanup() {
  rm -f "$TAGS_FILE" "$PS_FILE"
}
trap cleanup EXIT

if ! fetch_json "$LOCAL_BASE_URL/api/tags" "$TAGS_FILE"; then
  echo "ollama_api=unreachable"
  exit 1
fi
echo "ollama_api=reachable"

set +e
verify_model_from_tags "$TAGS_FILE"
tags_status="$?"
set -e
if [ "$tags_status" -ne 0 ]; then
  exit "$tags_status"
fi

if ! fetch_json "$LOCAL_BASE_URL/api/ps" "$PS_FILE"; then
  echo "qwen_resident=unknown"
  exit 4
fi
set +e
model_residency_from_ps "$PS_FILE"
residency_status="$?"
set -e
if [ "$residency_status" -ne 0 ]; then
  exit "$residency_status"
fi
exit "$OLLAMA_LAUNCHD_STATUS"
