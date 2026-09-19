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
if [ "${EMBEDDING_MODEL+x}" = x ]; then EMBEDDING_MODEL_ENV_SET="1"; else EMBEDDING_MODEL_ENV_SET="0"; fi
if [ "${EMBEDDING_MODEL_DIGEST+x}" = x ]; then EMBEDDING_MODEL_DIGEST_ENV_SET="1"; else EMBEDDING_MODEL_DIGEST_ENV_SET="0"; fi
if [ "${EMBEDDING_DIMENSIONS+x}" = x ]; then EMBEDDING_DIMENSIONS_ENV_SET="1"; else EMBEDDING_DIMENSIONS_ENV_SET="0"; fi

OLLAMA_MODEL_VALUE="${OLLAMA_MODEL:-}"
OLLAMA_MODEL_DIGEST_VALUE="${OLLAMA_MODEL_DIGEST:-}"
OLLAMA_BASE_URL_VALUE="${OLLAMA_BASE_URL:-}"
OLLAMA_LOCAL_BASE_URL_VALUE="${OLLAMA_LOCAL_BASE_URL:-}"
EMBEDDING_MODEL_VALUE="${EMBEDDING_MODEL:-}"
EMBEDDING_MODEL_DIGEST_VALUE="${EMBEDDING_MODEL_DIGEST:-}"
EMBEDDING_DIMENSIONS_VALUE="${EMBEDDING_DIMENSIONS:-}"

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
      EMBEDDING_MODEL)
        [ "$EMBEDDING_MODEL_ENV_SET" = "1" ] || EMBEDDING_MODEL_VALUE="$value"
        ;;
      EMBEDDING_MODEL_DIGEST)
        [ "$EMBEDDING_MODEL_DIGEST_ENV_SET" = "1" ] || EMBEDDING_MODEL_DIGEST_VALUE="$value"
        ;;
      EMBEDDING_DIMENSIONS)
        [ "$EMBEDDING_DIMENSIONS_ENV_SET" = "1" ] || EMBEDDING_DIMENSIONS_VALUE="$value"
        ;;
    esac
  done < "$ENV_FILE"
}

verify_model_from_tags() {
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import json
import sys

tags_path = sys.argv[1]
prefix = sys.argv[2]
model_name = sys.argv[3]
expected_digest = sys.argv[4]
try:
    with open(tags_path, encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, json.JSONDecodeError):
    print(f"{prefix}_model_installed=unknown")
    print(f"{prefix}_digest_match=unknown")
    sys.exit(4)
models = payload.get("models")
if not isinstance(models, list):
    print(f"{prefix}_model_installed=unknown")
    print(f"{prefix}_digest_match=unknown")
    sys.exit(4)
for model in models:
    if isinstance(model, dict) and model.get("name") == model_name:
        digest = str(model.get("digest") or "")
        print(f"{prefix}_model_installed=yes")
        if expected_digest:
            if digest == expected_digest:
                print(f"{prefix}_digest_match=yes")
                sys.exit(0)
            print(f"{prefix}_digest_match=no")
            sys.exit(3)
        print(f"{prefix}_digest_match=unchecked")
        sys.exit(0)
print(f"{prefix}_model_installed=no")
print(f"{prefix}_digest_match=unchecked")
sys.exit(2)
PY
}

model_residency_from_ps() {
  python3 - "$1" "$2" "$3" <<'PY'
import json
import sys

ps_path = sys.argv[1]
prefix = sys.argv[2]
model_name = sys.argv[3]
try:
    with open(ps_path, encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, json.JSONDecodeError):
    print(f"{prefix}_resident=unknown")
    sys.exit(4)
models = payload.get("models")
if not isinstance(models, list):
    print(f"{prefix}_resident=unknown")
    sys.exit(4)
for model in models:
    if isinstance(model, dict) and model.get("name") == model_name:
        print(f"{prefix}_resident=yes")
        context_length = model.get("context_length")
        size_vram = model.get("size_vram")
        print(
            f"{prefix}_context_length="
            f"{context_length if isinstance(context_length, int) else 'unknown'}"
        )
        print(
            f"{prefix}_size_vram="
            f"{size_vram if isinstance(size_vram, int) else 'unknown'}"
        )
        sys.exit(0)
print(f"{prefix}_resident=no")
print(f"{prefix}_context_length=not_resident")
print(f"{prefix}_size_vram=not_resident")
sys.exit(0)
PY
}

host_memory_status() {
  python3 - <<'PY'
import re
import subprocess

free_percent = "unknown"
swapouts = "unknown"
swapout_bytes = "unknown"
try:
    pressure = subprocess.run(
        ["/usr/bin/memory_pressure", "-Q"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    match = re.search(r"memory free percentage: (\d+)%", pressure)
    if match:
        free_percent = match.group(1)
except (OSError, subprocess.SubprocessError):
    pass
try:
    vm_stat = subprocess.run(
        ["/usr/bin/vm_stat"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    swapout_match = re.search(r"Swapouts:\s+(\d+)\.", vm_stat)
    page_size_match = re.search(r"page size of (\d+) bytes", vm_stat)
    if swapout_match:
        swapouts = swapout_match.group(1)
    if swapout_match and page_size_match:
        swapout_bytes = str(int(swapout_match.group(1)) * int(page_size_match.group(1)))
except (OSError, subprocess.SubprocessError):
    pass
print(f"host_memory_free_percent={free_percent}")
print(f"host_swapouts={swapouts}")
print(f"host_swapout_bytes={swapout_bytes}")
PY
}

verify_embedding_capability() {
  python3 - "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, json.JSONDecodeError):
    print("embedding_capability=unknown")
    sys.exit(4)
capabilities = payload.get("capabilities") if isinstance(payload, dict) else None
if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
    print("embedding_capability=unknown")
    sys.exit(4)
if "embedding" in {item.casefold() for item in capabilities}:
    print("embedding_capability=yes")
    sys.exit(0)
print("embedding_capability=no")
sys.exit(5)
PY
}

load_env_file
OLLAMA_MODEL_VALUE="${OLLAMA_MODEL_VALUE:-$DEFAULT_OLLAMA_MODEL}"
if [ "$OLLAMA_MODEL_DIGEST_CONFIGURED" = "0" ]; then
  OLLAMA_MODEL_DIGEST_VALUE="$DEFAULT_OLLAMA_MODEL_DIGEST"
fi
EMBEDDING_MODEL_VALUE="${EMBEDDING_MODEL_VALUE:-$DEFAULT_EMBEDDING_MODEL}"
EMBEDDING_DIMENSIONS_VALUE="${EMBEDDING_DIMENSIONS_VALUE:-$DEFAULT_EMBEDDING_DIMENSIONS}"
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
SHOW_FILE="$(mktemp -t lifeagent-ollama-show.XXXXXX)"
cleanup() {
  rm -f "$TAGS_FILE" "$PS_FILE" "$SHOW_FILE"
}
trap cleanup EXIT

if ! fetch_json "$LOCAL_BASE_URL/api/tags" "$TAGS_FILE"; then
  echo "ollama_api=unreachable"
  exit 1
fi
echo "ollama_api=reachable"

set +e
verify_model_from_tags "$TAGS_FILE" "reasoning" "$OLLAMA_MODEL_VALUE" "$OLLAMA_MODEL_DIGEST_VALUE"
reasoning_tags_status="$?"
verify_model_from_tags "$TAGS_FILE" "embedding" "$EMBEDDING_MODEL_VALUE" "$EMBEDDING_MODEL_DIGEST_VALUE"
embedding_tags_status="$?"
set -e
if [ "$reasoning_tags_status" -ne 0 ]; then
  exit "$reasoning_tags_status"
fi
if [ "$embedding_tags_status" -ne 0 ]; then
  exit "$embedding_tags_status"
fi

show_body="$(python3 - "$EMBEDDING_MODEL_VALUE" <<'PY'
import json
import sys
print(json.dumps({"name": sys.argv[1]}))
PY
)"
if ! post_json "$LOCAL_BASE_URL/api/show" "$show_body" "$SHOW_FILE"; then
  echo "embedding_capability=unknown"
  exit 1
fi
set +e
verify_embedding_capability "$SHOW_FILE"
capability_status="$?"
set -e
if [ "$capability_status" -ne 0 ]; then
  exit "$capability_status"
fi

if ! fetch_json "$LOCAL_BASE_URL/api/ps" "$PS_FILE"; then
  echo "reasoning_resident=unknown"
  echo "embedding_resident=unknown"
  exit 4
fi
set +e
model_residency_from_ps "$PS_FILE" "reasoning" "$OLLAMA_MODEL_VALUE"
reasoning_residency_status="$?"
model_residency_from_ps "$PS_FILE" "embedding" "$EMBEDDING_MODEL_VALUE"
embedding_residency_status="$?"
set -e
if [ "$reasoning_residency_status" -ne 0 ]; then
  exit "$reasoning_residency_status"
fi
if [ "$embedding_residency_status" -ne 0 ]; then
  exit "$embedding_residency_status"
fi
host_memory_status
exit "$OLLAMA_LAUNCHD_STATUS"
