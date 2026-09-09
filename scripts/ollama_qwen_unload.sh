#!/usr/bin/env bash
set -eu

DEFAULT_OLLAMA_MODEL="qwen3-32gb:latest"

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_DIR="$(dirname -- "$SCRIPT_DIR")"
ENV_FILE="$REPO_DIR/.env"

usage() {
  cat <<'USAGE'
Usage: scripts/ollama_qwen_unload.sh [--env-file PATH]

Unloads only the configured Qwen model from Ollama. Model files are not deleted
and the Ollama server is not stopped.
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
OLLAMA_MODEL_VALUE="${OLLAMA_MODEL:-}"

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
    esac
  done < "$ENV_FILE"
}

command_required() {
  command -v "$1" >/dev/null || {
    echo "$1 is required on the host PATH" >&2
    exit 127
  }
}

load_env_file
OLLAMA_MODEL_VALUE="${OLLAMA_MODEL_VALUE:-$DEFAULT_OLLAMA_MODEL}"

command_required ollama

if ollama stop "$OLLAMA_MODEL_VALUE" >/dev/null 2>&1; then
  echo "unloaded configured Qwen model: $OLLAMA_MODEL_VALUE"
else
  echo "failed to unload configured Qwen model: $OLLAMA_MODEL_VALUE" >&2
  exit 1
fi
