from __future__ import annotations

import os
import subprocess
from pathlib import Path

BASH = "/bin/bash"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPOSITORY_ROOT / "scripts/backup_database.sh"
RESTORE_SCRIPT = REPOSITORY_ROOT / "scripts/restore_database.sh"


def test_backup_and_restore_scripts_parse_and_fail_closed(tmp_path: Path) -> None:
    backup = BACKUP_SCRIPT
    restore = RESTORE_SCRIPT
    subprocess.run([BASH, "-n", str(backup)], check=True)  # noqa: S603
    subprocess.run([BASH, "-n", str(restore)], check=True)  # noqa: S603

    output_dir = tmp_path / "backups"
    dry_run = subprocess.run(  # noqa: S603
        [
            BASH,
            str(backup),
            "--database-url",
            "postgresql://lifeagent:lifeagent@localhost:5432/lifeagent",
            "--recipient",
            "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
            "--output-dir",
            str(output_dir),
            "--dry-run",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "would write encrypted backup to" in dry_run.stdout

    backup_file = tmp_path / "sample.dump.age"
    backup_file.write_text("not a real age payload", encoding="utf-8")
    identity_file = tmp_path / "keys.txt"
    identity_file.write_text("not a real age identity", encoding="utf-8")
    target_url = "postgresql://lifeagent:lifeagent@localhost:5432/lifeagent"
    refused = subprocess.run(  # noqa: S603
        [
            BASH,
            str(restore),
            "--backup-file",
            str(backup_file),
            "--identity-file",
            str(identity_file),
            "--target-database-url",
            target_url,
            "--confirm-target-db",
            "lifeagent",
        ],
        text=True,
        capture_output=True,
        env={**os.environ, "DATABASE_URL": target_url},
        check=False,
    )
    assert refused.returncode == 2
    assert "refusing to restore into the current DATABASE_URL" in refused.stderr


def test_restore_script_refuses_equivalent_current_database_urls(tmp_path: Path) -> None:
    backup_file = tmp_path / "sample.dump.age"
    backup_file.write_text("not a real age payload", encoding="utf-8")
    identity_file = tmp_path / "keys.txt"
    identity_file.write_text("not a real age identity", encoding="utf-8")

    refused = subprocess.run(  # noqa: S603
        [
            BASH,
            str(RESTORE_SCRIPT),
            "--backup-file",
            str(backup_file),
            "--identity-file",
            str(identity_file),
            "--target-database-url",
            "postgres://lifeagent:target-secret@localhost/lifeagent?connect_timeout=10",
            "--confirm-target-db",
            "lifeagent",
        ],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "DATABASE_URL": (
                "postgresql+psycopg://lifeagent:current-secret@127.0.0.1:5432/"
                "lifeagent?sslmode=disable"
            ),
        },
        check=False,
    )

    assert refused.returncode == 2
    assert "refusing to restore into the current DATABASE_URL" in refused.stderr
    assert "target-secret" not in refused.stderr
    assert "current-secret" not in refused.stderr


def test_restore_script_allows_current_database_with_explicit_override(tmp_path: Path) -> None:
    backup_file = tmp_path / "sample.dump.age"
    backup_file.write_text("fake archive", encoding="utf-8")
    identity_file = tmp_path / "keys.txt"
    identity_file.write_text("not a real age identity", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "age").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
output=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o)
      output="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
cp "$FAKE_AGE_PAYLOAD" "$output"
""",
        encoding="utf-8",
    )
    (fake_bin / "pg_restore").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--list" ]]; then
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    (fake_bin / "age").chmod(0o755)
    (fake_bin / "pg_restore").chmod(0o755)

    allowed = subprocess.run(  # noqa: S603
        [
            BASH,
            str(RESTORE_SCRIPT),
            "--backup-file",
            str(backup_file),
            "--identity-file",
            str(identity_file),
            "--target-database-url",
            "postgresql://lifeagent:target-secret@localhost:5432/lifeagent",
            "--confirm-target-db",
            "lifeagent",
            "--allow-current-database",
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "DATABASE_URL": "postgresql://lifeagent:current-secret@127.0.0.1/lifeagent",
            "FAKE_AGE_PAYLOAD": str(backup_file),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
        check=False,
    )

    assert allowed.returncode == 0
    assert "dry run passed for target database lifeagent" in allowed.stdout


def test_restore_script_rejects_old_target_db_interface(tmp_path: Path) -> None:
    backup_file = tmp_path / "sample.dump.age"
    backup_file.write_text("not a real age payload", encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [
            BASH,
            str(RESTORE_SCRIPT),
            "--backup",
            str(backup_file),
            "--target-db",
            "lifeagent_restore",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "unknown option: --backup" in result.stderr
