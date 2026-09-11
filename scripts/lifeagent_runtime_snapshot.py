#!/usr/bin/env python3
"""Stage an installed LifeAgent host runtime outside protected checkout paths."""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

PRIVATE_MODE = 0o700
SECRET_MODE = 0o600
RUNTIME_SCRIPT_NAMES = frozenset(
    {
        "lifeagent_discord_wake_daemon.sh",
        "lifeagent_host_runtime.sh",
        "lifeagent_launchd_common.sh",
        "ollama_qwen_start.sh",
        "ollama_qwen_status.sh",
        "ollama_qwen_unload.sh",
    }
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-artifacts", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    args = parser.parse_args()

    source_root = args.source_root.expanduser().resolve()
    source_artifacts = args.source_artifacts.expanduser().resolve()
    env_file = args.env_file.expanduser().resolve()
    runtime_dir = args.runtime_dir.expanduser()
    if runtime_dir.is_symlink():
        raise SystemExit("installed runtime directory must not be a symlink")
    if runtime_dir.exists() and not runtime_dir.is_dir():
        raise SystemExit("installed runtime path exists but is not a directory")
    if not env_file.is_file():
        raise SystemExit(f"LifeAgent .env is missing: {env_file}")
    if not (source_root / "app").is_dir():
        raise SystemExit(f"LifeAgent source app directory is missing: {source_root / 'app'}")
    if runtime_dir.name != "runtime":
        raise SystemExit("installed runtime directory must be named 'runtime'")

    runtime_parent = runtime_dir.parent
    if runtime_parent.is_symlink():
        raise SystemExit("installed runtime parent directory must not be a symlink")
    runtime_parent.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    os.chmod(runtime_parent, PRIVATE_MODE)
    if runtime_dir.is_dir():
        os.chmod(runtime_dir, PRIVATE_MODE)

    staging = Path(tempfile.mkdtemp(prefix=".runtime.stage-", dir=runtime_parent))
    backup: Path | None = None
    try:
        _copy_runtime_source(source_root, env_file, staging)
        _stage_artifacts(
            source_artifacts=source_artifacts,
            existing_artifacts=runtime_dir / ".artifacts" / "discord-wake",
            staged_artifacts=staging / ".artifacts" / "discord-wake",
        )
        if runtime_dir.exists():
            backup = runtime_parent / ".runtime.previous"
            if backup.exists():
                shutil.rmtree(backup)
            shutil.move(str(runtime_dir), str(backup))
        shutil.move(str(staging), str(runtime_dir))
        os.chmod(runtime_dir, PRIVATE_MODE)
        print(f"installed runtime snapshot: {runtime_dir}")
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.exists() and not runtime_dir.exists():
            shutil.move(str(backup), str(runtime_dir))
        raise


def _copy_runtime_source(source_root: Path, env_file: Path, staging: Path) -> None:
    shutil.copytree(
        source_root / "app",
        staging / "app",
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    scripts_dir = staging / "scripts"
    scripts_dir.mkdir()
    for script in (source_root / "scripts").iterdir():
        if script.is_file() and (
            script.name in RUNTIME_SCRIPT_NAMES or script.name.endswith(".plist.template")
        ):
            shutil.copy2(script, scripts_dir / script.name, follow_symlinks=True)
    for name in ("compose.yaml", "pyproject.toml", "uv.lock", "alembic.ini"):
        source = source_root / name
        if source.is_file():
            shutil.copy2(source, staging / name, follow_symlinks=True)
    shutil.copy2(env_file, staging / ".env", follow_symlinks=True)
    os.chmod(staging / ".env", SECRET_MODE)


def _stage_artifacts(
    *,
    source_artifacts: Path,
    existing_artifacts: Path,
    staged_artifacts: Path,
) -> None:
    staged_artifacts.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    os.chmod(staged_artifacts.parent, PRIVATE_MODE)
    os.chmod(staged_artifacts, PRIVATE_MODE)
    for artifact_dir in (source_artifacts, existing_artifacts, existing_artifacts.parent):
        if artifact_dir.is_symlink():
            raise SystemExit(f"artifact directory must not be a symlink: {artifact_dir}")
    for existing_parent in (existing_artifacts.parent, existing_artifacts):
        if existing_parent.is_dir():
            os.chmod(existing_parent, PRIVATE_MODE)
    _copy_private_artifacts(existing_artifacts, staged_artifacts)
    if (
        source_artifacts.exists()
        and existing_artifacts.exists()
        and not _same_tree(
            source_artifacts,
            existing_artifacts,
        )
    ):
        pending = _pending_outbox_count(
            source_artifacts / "outbox.sqlite3",
            existing_artifacts / "outbox.sqlite3",
        )
        if pending:
            raise SystemExit(
                "refusing to replace installed outbox while checkout outbox has "
                f"{pending} pending wake row(s) not already present in the installed runtime"
            )
    _copy_private_artifacts(source_artifacts, staged_artifacts, preserve_existing=True)
    os.chmod(staged_artifacts, PRIVATE_MODE)


def _copy_private_artifacts(
    source_dir: Path,
    destination_dir: Path,
    *,
    preserve_existing: bool = False,
) -> None:
    if not source_dir.is_dir():
        return
    for name in ("discord-wake-hmac.key", "deployed-image-id"):
        source = source_dir / name
        destination = destination_dir / name
        if source.is_file() and not (preserve_existing and destination.exists()):
            shutil.copy2(source, destination, follow_symlinks=True)
            os.chmod(destination, SECRET_MODE)
    source_outbox = source_dir / "outbox.sqlite3"
    destination_outbox = destination_dir / "outbox.sqlite3"
    if source_outbox.is_file() and not (preserve_existing and destination_outbox.exists()):
        _backup_sqlite(source_outbox, destination_outbox)


def _backup_sqlite(source: Path, destination: Path) -> None:
    tmp = destination.with_name(f".{destination.name}.tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        with (
            sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_connection,
            sqlite3.connect(tmp) as destination_connection,
        ):
            source_connection.backup(destination_connection)
    except sqlite3.DatabaseError:
        if tmp.exists():
            tmp.unlink()
        raise SystemExit(f"could not back up SQLite outbox safely: {source}") from None
    os.replace(tmp, destination)
    os.chmod(destination, SECRET_MODE)


def _pending_outbox_count(source: Path, existing: Path | None = None) -> int:
    if not source.is_file():
        return 0
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as connection:
            existing_wake_ids, existing_interaction_ids = _outbox_id_sets(existing)
            total = 0
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "wake_events" in tables:
                rows = connection.execute(
                    "SELECT message_id FROM wake_events WHERE state IN ('pending', 'acknowledged')"
                ).fetchall()
                total += sum(1 for row in rows if str(row[0]) not in existing_wake_ids)
            if "interaction_events" in tables:
                rows = connection.execute(
                    "SELECT interaction_id FROM interaction_events WHERE state = 'pending'"
                ).fetchall()
                total += sum(1 for row in rows if str(row[0]) not in existing_interaction_ids)
            return total
    except sqlite3.DatabaseError:
        return 1


def _outbox_id_sets(path: Path | None) -> tuple[set[str], set[str]]:
    if path is None or not path.is_file():
        return set(), set()
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            wake_ids = (
                {str(row[0]) for row in connection.execute("SELECT message_id FROM wake_events")}
                if "wake_events" in tables
                else set()
            )
            interaction_ids = (
                {
                    str(row[0])
                    for row in connection.execute("SELECT interaction_id FROM interaction_events")
                }
                if "interaction_events" in tables
                else set()
            )
            return wake_ids, interaction_ids
    except sqlite3.DatabaseError:
        return set(), set()


def _same_tree(left: Path, right: Path) -> bool:
    for name in ("discord-wake-hmac.key", "deployed-image-id", "outbox.sqlite3"):
        left_file = left / name
        right_file = right / name
        if left_file.exists() != right_file.exists():
            return False
        if left_file.is_file() and not filecmp.cmp(left_file, right_file, shallow=False):
            return False
    return True


if __name__ == "__main__":
    main()
