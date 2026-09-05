#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/Users/richardliu/.config/lifeagent/backup.env"
AGE_IDENTITY_FILE="/Users/richardliu/.config/age/keys.txt"
AGE_IDENTITY_FILE_FLAG=""
BACKUP_FILE=""
TARGET_DATABASE_URL=""
CONFIRM_TARGET_DB=""
DRY_RUN="0"
ALLOW_CURRENT_DATABASE="0"

usage() {
  cat <<'USAGE'
Usage: scripts/restore_database.sh --backup-file FILE --target-database-url URL --confirm-target-db NAME [options]

Decrypts an encrypted LifeAgent dump with age and restores it with pg_restore.
The target database name must be explicitly confirmed. The script refuses to
restore into the current DATABASE_URL unless --allow-current-database is passed.

Options:
  --backup-file FILE          Encrypted .dump.age file to restore
  --target-database-url URL   PostgreSQL URL for the restore target
  --confirm-target-db NAME    Required database name confirmation
  --identity-file PATH        age private identity file (default: /Users/richardliu/.config/age/keys.txt)
  --env-file PATH             Env file to source for DATABASE_URL comparison
  --dry-run                   Decrypt and list archive contents without restoring
  --allow-current-database    Permit target URL equal to current DATABASE_URL
  -h, --help                  Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-file)
      BACKUP_FILE="${2:?missing --backup-file value}"
      shift 2
      ;;
    --target-database-url)
      TARGET_DATABASE_URL="${2:?missing --target-database-url value}"
      shift 2
      ;;
    --confirm-target-db)
      CONFIRM_TARGET_DB="${2:?missing --confirm-target-db value}"
      shift 2
      ;;
    --identity-file)
      AGE_IDENTITY_FILE_FLAG="${2:?missing --identity-file value}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:?missing --env-file value}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="1"
      shift
      ;;
    --allow-current-database)
      ALLOW_CURRENT_DATABASE="1"
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

AGE_IDENTITY_FILE="${AGE_IDENTITY_FILE_FLAG:-${AGE_IDENTITY_FILE:-/Users/richardliu/.config/age/keys.txt}}"

database_name_from_url() {
  local value="${1%%\?*}"
  value="${value%/}"
  printf '%s\n' "${value##*/}"
}

canonical_database_identity() {
  python3 - "$1" <<'PY'
import sys
from urllib.parse import unquote, urlparse

raw = sys.argv[1]
parsed = urlparse(raw)
scheme = parsed.scheme.lower().split("+", 1)[0]
if scheme == "postgres":
    scheme = "postgresql"

host = (parsed.hostname or "").lower().strip("[]")
if host in {"", "localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}:
    host = "loopback"

try:
    port = parsed.port or 5432
except ValueError:
    print("invalid PostgreSQL URL port", file=sys.stderr)
    sys.exit(2)

database = unquote(parsed.path.lstrip("/")).rstrip("/")
user = unquote(parsed.username or "")
print("\t".join([scheme, host, str(port), database, user]))
PY
}

if [[ -z "$BACKUP_FILE" || -z "$TARGET_DATABASE_URL" || -z "$CONFIRM_TARGET_DB" ]]; then
  echo "--backup-file, --target-database-url, and --confirm-target-db are required" >&2
  exit 2
fi
if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "backup file does not exist: $BACKUP_FILE" >&2
  exit 2
fi
if [[ ! -f "$AGE_IDENTITY_FILE" ]]; then
  echo "age identity file does not exist: $AGE_IDENTITY_FILE" >&2
  exit 2
fi

target_db="$(database_name_from_url "$TARGET_DATABASE_URL")"
if [[ -z "$target_db" || "$target_db" != "$CONFIRM_TARGET_DB" ]]; then
  echo "target database name '$target_db' does not match --confirm-target-db '$CONFIRM_TARGET_DB'" >&2
  exit 2
fi

current_database_url="${DATABASE_URL:-}"
if [[ "$ALLOW_CURRENT_DATABASE" != "1" && -n "$current_database_url" ]]; then
  command -v python3 >/dev/null || {
    echo "python3 is required to validate restore target safety" >&2
    exit 127
  }
  target_identity="$(canonical_database_identity "$TARGET_DATABASE_URL")"
  current_identity="$(canonical_database_identity "$current_database_url")"
  if [[ "$target_identity" == "$current_identity" ]]; then
    echo "refusing to restore into the current DATABASE_URL without --allow-current-database" >&2
    exit 2
  fi
fi

command -v age >/dev/null || {
  echo "age is required on the host PATH" >&2
  exit 127
}
command -v pg_restore >/dev/null || {
  echo "pg_restore is required on the host PATH" >&2
  exit 127
}

temporary="$(mktemp -t lifeagent-restore.XXXXXX)"
cleanup() {
  rm -f "$temporary"
}
trap cleanup EXIT

age -d -i "$AGE_IDENTITY_FILE" -o "$temporary" "$BACKUP_FILE"

if [[ "$DRY_RUN" == "1" ]]; then
  pg_restore --list "$temporary" >/dev/null
  echo "dry run passed for target database $target_db"
  exit 0
fi

pg_restore --clean --if-exists --no-owner --no-acl --dbname "$TARGET_DATABASE_URL" "$temporary"
echo "restored $BACKUP_FILE into database $target_db"
