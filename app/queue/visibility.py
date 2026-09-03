"""Read-only mapping of explicit job metadata to queue dashboard states."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Engine, text


class QueueVisibility(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    FAILED = "failed"
    STALLED = "stalled"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class QueueJobMetadata:
    """Minimal explicit record needed to render queue status safely."""

    job_id: int | str
    status: str
    attempts: int = 0
    max_attempts: int = 1
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    worker_id: int | str | None = None
    queue_name: str | None = None
    task_name: str | None = None


def queue_visibility(
    record: QueueJobMetadata,
    *,
    now: datetime | None = None,
    stalled_after: timedelta = timedelta(seconds=60),
) -> QueueVisibility:
    """Map durable status and heartbeat metadata to a dashboard state."""

    if stalled_after <= timedelta(0):
        raise ValueError("stalled_after must be positive")
    status = record.status.lower()
    if status in {"succeeded", "successful", "completed", "done"}:
        return QueueVisibility.SUCCEEDED
    if status in {"failed", "error", "aborted"}:
        return QueueVisibility.FAILED
    if status in {"retrying", "retry"}:
        return QueueVisibility.RETRYING
    if status == "deferred" and record.attempts > 0:
        return QueueVisibility.RETRYING
    if status in {"doing", "running", "executing", "active"}:
        reference = record.heartbeat_at or record.started_at
        if reference is not None:
            current = (now or datetime.now(UTC)).astimezone(UTC)
            heartbeat = _aware(reference).astimezone(UTC)
            if current - heartbeat > stalled_after:
                return QueueVisibility.STALLED
        return QueueVisibility.RUNNING
    return QueueVisibility.QUEUED


def list_queue_jobs(
    engine: Engine,
    *,
    queues: frozenset[str] | None = None,
    max_attempts: int = 3,
) -> list[QueueJobMetadata]:
    """Read Procrastinate job/heartbeat metadata without exposing job arguments."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    statement = text(
        """
        SELECT
            jobs.id,
            jobs.status::text AS status,
            jobs.attempts,
            jobs.scheduled_at,
            jobs.worker_id,
            jobs.queue_name,
            jobs.task_name,
            workers.last_heartbeat AS heartbeat_at,
            events.started_at,
            events.finished_at
        FROM procrastinate_jobs AS jobs
        LEFT JOIN procrastinate_workers AS workers ON workers.id = jobs.worker_id
        LEFT JOIN LATERAL (
            SELECT
                max(event.at) FILTER (WHERE event.type = 'started') AS started_at,
                max(event.at) FILTER (
                    WHERE event.type IN ('failed', 'succeeded', 'cancelled', 'aborted')
                ) AS finished_at
            FROM procrastinate_events AS event
            WHERE event.job_id = jobs.id
        ) AS events ON true
        ORDER BY jobs.id
        """
    )
    with engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()

    records: list[QueueJobMetadata] = []
    for row in rows:
        queue_name = str(row["queue_name"])
        if queues is not None and queue_name not in queues:
            continue
        records.append(
            QueueJobMetadata(
                job_id=int(row["id"]),
                status=str(row["status"]),
                attempts=int(row["attempts"]),
                max_attempts=max_attempts,
                scheduled_at=_optional_datetime(row["scheduled_at"]),
                started_at=_optional_datetime(row["started_at"]),
                heartbeat_at=_optional_datetime(row["heartbeat_at"]),
                finished_at=_optional_datetime(row["finished_at"]),
                worker_id=int(row["worker_id"]) if row["worker_id"] is not None else None,
                queue_name=queue_name,
                task_name=str(row["task_name"]),
            )
        )
    return records


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("queue timestamps must be timezone-aware")
    return value


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError("queue database returned an invalid timestamp")
    return _aware(value)


__all__ = [
    "QueueJobMetadata",
    "QueueVisibility",
    "list_queue_jobs",
    "queue_visibility",
]
