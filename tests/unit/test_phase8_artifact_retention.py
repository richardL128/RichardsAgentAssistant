"""Unit coverage for the Phase 8 artifact-retention periodic job.

Mirrors the pattern in ``tests/unit/test_queue.py`` for
``shared_services_periodic``: monkeypatch the module-level ``_database``
(and, here, ``_settings``) with lightweight stand-ins so the periodic task's
``.func`` coroutine can run against a real SQLite engine and a real
filesystem artifact store without any live PostgreSQL/Procrastinate
dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.artifacts import ArtifactStore
from app.core.config import Settings
from app.db.models import AuditEvent
from app.queue import tasks
from app.queue.periodic import TorontoPeriodicSchedule


def _settings(tmp_path: Path, *, retention_days: int = 1) -> Settings:
    return Settings(artifact_root=tmp_path, artifact_retention_days=retention_days)


def _audit_database(tmp_path: Path) -> SimpleNamespace:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'audit.db'}")
    AuditEvent.__table__.create(engine)
    return SimpleNamespace(engine=engine)


def test_prune_expired_artifacts_deletes_eligible_and_audits_each_key(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    settings = _settings(artifact_root)
    old_store = ArtifactStore(
        artifact_root,
        default_retention_days=settings.artifact_retention_days,
        clock=lambda: datetime.now(UTC) - timedelta(days=3),
    )
    expired = old_store.put("stale body", media_type="text/plain", data_class="tool_log")
    store = ArtifactStore(artifact_root, default_retention_days=settings.artifact_retention_days)
    fresh = store.put("fresh body", media_type="text/plain", data_class="tool_log")
    immutable = store.put("audit body", media_type="text/plain", data_class="audit")

    database = _audit_database(tmp_path)
    later = datetime.now(UTC)

    try:
        pruned = tasks._prune_expired_artifacts(settings, database, now=later)
        with Session(database.engine) as session:
            events = list(session.scalars(select(AuditEvent)))
    finally:
        database.engine.dispose()

    assert pruned == (expired.key,)
    with pytest.raises(FileNotFoundError):
        store.get(expired.key)
    # A freshly created artifact has not reached its retention window yet, and
    # audit/immutable classes are never treated as expired by ArtifactStore.
    assert store.get(fresh.key) == b"fresh body"
    assert store.get(immutable.key) == b"audit body"

    assert [event.result for event in events] == ["prune_requested", "pruned"]
    assert {event.actor for event in events} == {"system"}
    assert {event.action for event in events} == {"artifact.prune"}
    assert {event.target_type for event in events} == {"artifact"}
    assert {event.target_id for event in events} == {expired.key}


def test_prune_expired_artifacts_does_not_delete_when_initial_audit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    settings = _settings(artifact_root)
    store = ArtifactStore(
        artifact_root,
        default_retention_days=settings.artifact_retention_days,
        clock=lambda: datetime.now(UTC) - timedelta(days=3),
    )
    expired = store.put("stale body", media_type="text/plain", data_class="tool_log")
    database = _audit_database(tmp_path)

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(tasks, "_append_artifact_prune_audit", fail_audit)

    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            tasks._prune_expired_artifacts(settings, database, now=datetime.now(UTC))
    finally:
        database.engine.dispose()

    assert store.get(expired.key) == b"stale body"


def test_prune_expired_artifacts_is_a_noop_when_nothing_has_expired(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    settings = _settings(artifact_root, retention_days=30)
    store = ArtifactStore(artifact_root, default_retention_days=settings.artifact_retention_days)
    kept = store.put("body", media_type="text/plain", data_class="tool_log")
    database = _audit_database(tmp_path)

    try:
        pruned = tasks._prune_expired_artifacts(settings, database, now=datetime.now(UTC))
        with Session(database.engine) as session:
            events = list(session.scalars(select(AuditEvent)))
    finally:
        database.engine.dispose()

    assert pruned == ()
    assert events == []
    assert store.get(kept.key) == b"body"


async def test_artifact_retention_periodic_skips_when_not_due(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "artifacts")
    monkeypatch.setattr(tasks, "_settings", settings)

    def fail_if_called(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        raise AssertionError("pruning must not run outside the configured schedule window")

    monkeypatch.setattr(tasks, "_prune_expired_artifacts", fail_if_called)

    off_schedule = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    result = await tasks.artifact_retention_periodic.func(timestamp=int(off_schedule.timestamp()))

    assert result == {"status": "not_due"}


async def test_artifact_retention_periodic_prunes_on_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_root = tmp_path / "artifacts"
    settings = _settings(artifact_root)
    store = ArtifactStore(artifact_root, default_retention_days=settings.artifact_retention_days)
    store.put("stale body", media_type="text/plain", data_class="tool_log")

    database = _audit_database(tmp_path)
    monkeypatch.setattr(tasks, "_settings", settings)
    monkeypatch.setattr(tasks, "_database", database)

    # Resolve the next real occurrence of the configured local schedule that is
    # at least three days out, so the artifact (created "now", 1-day retention)
    # is unambiguously expired by the time the periodic job fires.
    schedule = TorontoPeriodicSchedule.from_time(settings.artifact_retention_schedule)
    occurrence = schedule.next_occurrence(after_utc=datetime.now(UTC) + timedelta(days=3))

    try:
        result = await tasks.artifact_retention_periodic.func(
            timestamp=int(occurrence.scheduled_at.timestamp())
        )
        with Session(database.engine) as session:
            events = list(session.scalars(select(AuditEvent)))
    finally:
        database.engine.dispose()

    assert result == {"status": "succeeded", "pruned": 1}
    assert [event.result for event in events] == ["prune_requested", "pruned"]
    assert {event.action for event in events} == {"artifact.prune"}
