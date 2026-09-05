#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/Users/richardliu/.config/lifeagent/backup.env"
DEFAULT_BACKUP_DIR="/Users/richardliu/Backups/LifeAgent"
BACKUP_DIR="$DEFAULT_BACKUP_DIR"
BACKUP_DIR_FLAG=""
RETENTION_DAYS="30"
DATABASE_URL_VALUE="${DATABASE_URL:-}"
AGE_RECIPIENT_VALUE="${AGE_RECIPIENT:-}"

usage() {
  cat <<'USAGE'
Usage: scripts/backup_database.sh [options]

Creates an encrypted PostgreSQL custom-format dump:
  pg_dump --format=custom DATABASE_URL | age -r AGE_RECIPIENT

Options:
  --env-file PATH        Env file to source (default: /Users/richardliu/.config/lifeagent/backup.env)
  --database-url URL     PostgreSQL URL to dump (overrides DATABASE_URL)
  --output-dir PATH      Backup directory (default: /Users/richardliu/Backups/LifeAgent)
  --recipient RECIPIENT  age public recipient (overrides AGE_RECIPIENT)
  --retention-days DAYS  Delete encrypted backups older than DAYS (default: 30)
  --dry-run              Validate configuration and print the destination path only
  -h, --help             Show this help
USAGE
}

DRY_RUN="0"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      ENV_FILE="${2:?missing --env-file value}"
      shift 2
      ;;
    --database-url)
      DATABASE_URL_VALUE="${2:?missing --database-url value}"
      shift 2
      ;;
    --output-dir)
      BACKUP_DIR_FLAG="${2:?missing --output-dir value}"
      shift 2
      ;;
    --recipient)
      AGE_RECIPIENT_VALUE="${2:?missing --recipient value}"
      shift 2
      ;;
    --retention-days)
      RETENTION_DAYS="${2:?missing --retention-days value}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="1"
      shift
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

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

DATABASE_URL_VALUE="${DATABASE_URL_VALUE:-${DATABASE_URL:-}}"
AGE_RECIPIENT_VALUE="${AGE_RECIPIENT_VALUE:-${AGE_RECIPIENT:-}}"
AGE_RECIPIENT_VALUE="${AGE_RECIPIENT_VALUE:-${LIFEAGENT_AGE_RECIPIENT:-}}"
BACKUP_DIR="${BACKUP_DIR_FLAG:-${LIFEAGENT_BACKUP_DIR:-${BACKUP_DIR:-$DEFAULT_BACKUP_DIR}}}"

if [[ -z "$DATABASE_URL_VALUE" ]]; then
  echo "DATABASE_URL is required via --database-url, environment, or $ENV_FILE" >&2
  exit 2
fi
if [[ -z "$AGE_RECIPIENT_VALUE" ]]; then
  echo "AGE_RECIPIENT or LIFEAGENT_AGE_RECIPIENT is required" >&2
  exit 2
fi
if ! [[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || [[ "$RETENTION_DAYS" -lt 1 ]]; then
  echo "--retention-days must be a positive integer" >&2
  exit 2
fi

umask 077
mkdir -p "$BACKUP_DIR"
timestamp="$(TZ=America/Toronto date +%Y%m%dT%H%M%S%z)"
destination="$BACKUP_DIR/lifeagent-$timestamp.dump.age"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "would write encrypted backup to $destination"
  exit 0
fi

command -v pg_dump >/dev/null || {
  echo "pg_dump is required on the host PATH" >&2
  exit 127
}
command -v age >/dev/null || {
  echo "age is required on the host PATH" >&2
  exit 127
}

temporary="$destination.tmp"
cleanup() {
  rm -f "$temporary"
}
trap cleanup EXIT

pg_dump --format=custom --no-owner --no-acl "$DATABASE_URL_VALUE" |
  age -r "$AGE_RECIPIENT_VALUE" -o "$temporary"
mv "$temporary" "$destination"

find "$BACKUP_DIR" -maxdepth 1 -type f -name 'lifeagent-*.dump.age' \
  -mtime +"$RETENTION_DAYS" -print -delete

echo "wrote encrypted backup to $destination"
