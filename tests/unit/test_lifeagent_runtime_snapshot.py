from __future__ import annotations

import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lifeagent_runtime_snapshot.py"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _create_outbox(path: Path, *, state: str, message_id: str = "111111111111111111") -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE wake_events (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                event_timestamp TEXT NOT NULL,
                request_kind TEXT NOT NULL,
                acknowledgement_message_id TEXT,
                state TEXT NOT NULL,
                retry_count INTEGER NOT NULL,
                safe_error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO wake_events (
                message_id, channel_id, author_id, event_timestamp, request_kind,
                acknowledgement_message_id, state, retry_count, safe_error_code,
                created_at, updated_at
            ) VALUES (
                ?, '222222222222222222', '333333333333333333',
                '2026-01-01T00:00:00+00:00', 'mention', NULL, ?, 0, NULL,
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            )
            """,
            (message_id, state),
        )
        connection.commit()
    path.chmod(0o600)


def _source_tree(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "checkout"
    _write(source / "app" / "host" / "daemon.py", "print('daemon')\n")
    _write(source / "scripts" / "lifeagent_discord_wake_daemon.sh", "#!/usr/bin/env bash\n")
    _write(source / "scripts" / "ollama_qwen_status.sh", "#!/usr/bin/env bash\n")
    _write(source / "scripts" / "unrelated.sh", "#!/usr/bin/env bash\n")
    _write(source / "compose.yaml", "name: lifeagent\n")
    _write(source / "pyproject.toml", "[project]\nname = 'lifeagent'\n")
    _write(source / "uv.lock", "version = 1\n")
    env_file = tmp_path / "private.env"
    env_file.write_text("DISCORD_BOT_TOKEN=secret\n", encoding="utf-8")
    return source, env_file


def _run_snapshot(
    source: Path,
    env_file: Path,
    runtime: Path,
    source_artifacts: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    artifacts = source_artifacts or source / ".artifacts" / "discord-wake"
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(SCRIPT),
            "--source-root",
            str(source),
            "--source-artifacts",
            str(artifacts),
            "--env-file",
            str(env_file),
            "--runtime-dir",
            str(runtime),
        ],
        check=False,
        text=True,
        capture_output=True,
    )


def test_snapshot_copies_runtime_sources_config_and_sqlite_artifacts(tmp_path: Path) -> None:
    source, env_file = _source_tree(tmp_path)
    source_artifacts = source / ".artifacts" / "discord-wake"
    _write(source_artifacts / "discord-wake-hmac.key", "hmac\n")
    _write(source_artifacts / "deployed-image-id", "sha256:old\n")
    _create_outbox(source_artifacts / "outbox.sqlite3", state="pending")
    runtime = tmp_path / "Application Support" / "LifeAgent" / "runtime"

    result = _run_snapshot(source, env_file, runtime)

    assert result.returncode == 0, result.stderr
    daemon_source = (runtime / "app" / "host" / "daemon.py").read_text(encoding="utf-8")
    assert daemon_source == "print('daemon')\n"
    assert (runtime / "scripts" / "lifeagent_discord_wake_daemon.sh").is_file()
    assert (runtime / "scripts" / "ollama_qwen_status.sh").is_file()
    assert not (runtime / "scripts" / "unrelated.sh").exists()
    assert (runtime / "compose.yaml").read_text(encoding="utf-8") == "name: lifeagent\n"
    assert (runtime / ".env").read_text(encoding="utf-8") == "DISCORD_BOT_TOKEN=secret\n"
    artifacts = runtime / ".artifacts" / "discord-wake"
    assert (artifacts / "discord-wake-hmac.key").read_text(encoding="utf-8") == "hmac\n"
    assert (artifacts / "deployed-image-id").read_text(encoding="utf-8") == "sha256:old\n"
    with sqlite3.connect(artifacts / "outbox.sqlite3") as connection:
        assert connection.execute("SELECT state FROM wake_events").fetchone()[0] == "pending"
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    assert stat.S_IMODE((runtime / ".artifacts").stat().st_mode) == 0o700
    assert stat.S_IMODE(artifacts.stat().st_mode) == 0o700
    assert stat.S_IMODE((runtime / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((artifacts / "outbox.sqlite3").stat().st_mode) == 0o600


def test_snapshot_refuses_to_replace_installed_outbox_when_checkout_has_pending_rows(
    tmp_path: Path,
) -> None:
    source, env_file = _source_tree(tmp_path)
    source_artifacts = source / ".artifacts" / "discord-wake"
    _create_outbox(
        source_artifacts / "outbox.sqlite3",
        state="pending",
        message_id="444444444444444444",
    )
    runtime = tmp_path / "Application Support" / "LifeAgent" / "runtime"
    installed_artifacts = runtime / ".artifacts" / "discord-wake"
    _create_outbox(installed_artifacts / "outbox.sqlite3", state="accepted")
    _write(runtime / "app" / "host" / "daemon.py", "old runtime\n")

    result = _run_snapshot(source, env_file, runtime)

    assert result.returncode != 0
    assert "refusing to replace installed outbox" in result.stderr
    assert (runtime / "app" / "host" / "daemon.py").read_text(encoding="utf-8") == "old runtime\n"
    with sqlite3.connect(installed_artifacts / "outbox.sqlite3") as connection:
        assert connection.execute("SELECT state FROM wake_events").fetchone()[0] == "accepted"


def test_snapshot_allows_checkout_pending_rows_already_present_in_installed_outbox(
    tmp_path: Path,
) -> None:
    source, env_file = _source_tree(tmp_path)
    source_artifacts = source / ".artifacts" / "discord-wake"
    _create_outbox(source_artifacts / "outbox.sqlite3", state="pending")
    runtime = tmp_path / "Application Support" / "LifeAgent" / "runtime"
    installed_artifacts = runtime / ".artifacts" / "discord-wake"
    _create_outbox(installed_artifacts / "outbox.sqlite3", state="accepted")

    result = _run_snapshot(source, env_file, runtime)

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(runtime / ".artifacts" / "discord-wake" / "outbox.sqlite3") as connection:
        assert connection.execute("SELECT state FROM wake_events").fetchone()[0] == "accepted"


def test_snapshot_rejects_corrupt_source_outbox_instead_of_raw_copying(
    tmp_path: Path,
) -> None:
    source, env_file = _source_tree(tmp_path)
    source_artifacts = source / ".artifacts" / "discord-wake"
    _write(source_artifacts / "outbox.sqlite3", "not a sqlite database")
    runtime = tmp_path / "Application Support" / "LifeAgent" / "runtime"

    result = _run_snapshot(source, env_file, runtime)

    assert result.returncode != 0
    assert "could not back up SQLite outbox safely" in result.stderr
    assert not (runtime / ".artifacts" / "discord-wake" / "outbox.sqlite3").exists()


def test_snapshot_refuses_runtime_symlink(tmp_path: Path) -> None:
    source, env_file = _source_tree(tmp_path)
    real_runtime = tmp_path / "real-runtime"
    real_runtime.mkdir()
    runtime = tmp_path / "Application Support" / "LifeAgent" / "runtime"
    runtime.parent.mkdir(parents=True)
    runtime.symlink_to(real_runtime, target_is_directory=True)

    result = _run_snapshot(source, env_file, runtime)

    assert result.returncode != 0
    assert "must not be a symlink" in result.stderr


def test_snapshot_rejects_existing_runtime_regular_file(tmp_path: Path) -> None:
    source, env_file = _source_tree(tmp_path)
    runtime = tmp_path / "Application Support" / "LifeAgent" / "runtime"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("not a directory", encoding="utf-8")

    result = _run_snapshot(source, env_file, runtime)

    assert result.returncode != 0
    assert "not a directory" in result.stderr


def test_snapshot_rejects_symlinked_existing_artifact_directory(tmp_path: Path) -> None:
    source, env_file = _source_tree(tmp_path)
    runtime = tmp_path / "Application Support" / "LifeAgent" / "runtime"
    real_artifacts = tmp_path / "real-artifacts"
    real_artifacts.mkdir()
    artifact_parent = runtime / ".artifacts"
    artifact_parent.mkdir(parents=True)
    (artifact_parent / "discord-wake").symlink_to(real_artifacts, target_is_directory=True)

    result = _run_snapshot(source, env_file, runtime)

    assert result.returncode != 0
    assert "artifact directory must not be a symlink" in result.stderr
