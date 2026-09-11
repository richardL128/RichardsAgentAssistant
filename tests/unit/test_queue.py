from __future__ import annotations

import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import ErrorCode, authorization_error, transient_error
from app.db.models import HealthCheck as PersistedHealthCheck
from app.health.checks import HealthCheck, HealthState
from app.queue import tasks
from app.queue.app import (
    QUEUE_NAMES,
    create_procrastinate_app,
    postgres_conninfo,
    procrastinate_app,
)
from app.queue.idempotency import (
    IdempotencyKeyError,
    build_idempotency_key,
    validate_idempotency_key,
)
from app.queue.periodic import PeriodicOccurrence, TorontoPeriodicSchedule, stable_period_key
from app.queue.retry import (
    RetryClassification,
    RetryPolicy,
    TransientRetryStrategy,
    classify_retry_error,
)
from app.queue.visibility import QueueJobMetadata, QueueVisibility, queue_visibility


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("upstream timeout"), RetryClassification.TRANSIENT),
        (httpx.ConnectError("connection reset"), RetryClassification.TRANSIENT),
        (
            httpx.HTTPStatusError("too many requests", request=None, response=httpx.Response(429)),
            RetryClassification.TRANSIENT,
        ),  # type: ignore[arg-type]
        (
            httpx.HTTPStatusError("server error", request=None, response=httpx.Response(503)),
            RetryClassification.TRANSIENT,
        ),  # type: ignore[arg-type]
        (
            httpx.HTTPStatusError("bad token", request=None, response=httpx.Response(401)),
            RetryClassification.AUTHORIZATION,
        ),  # type: ignore[arg-type]
        (ValueError("invalid token"), RetryClassification.AUTHORIZATION),
        (ValueError("schema validation failed"), RetryClassification.PERMANENT),
        (
            transient_error(ErrorCode.MODEL_TRANSIENT, "model unavailable"),
            RetryClassification.TRANSIENT,
        ),
        (authorization_error(), RetryClassification.AUTHORIZATION),
    ],
)
def test_retry_classification(error: BaseException, expected: RetryClassification) -> None:
    assert classify_retry_error(error) is expected


def test_retry_policy_delay_is_capped_and_jitter_is_injectable() -> None:
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_seconds=10,
        max_delay_seconds=30,
        jitter_ratio=0.2,
        random_fn=lambda: 1.0,
    )
    assert policy.delay_seconds(0) == 12
    assert policy.delay_seconds(1) == 24
    assert policy.delay_seconds(2) == 30
    assert policy.delay_seconds(9) == 30


def test_procrastinate_strategy_only_retries_transient_and_honors_attempt_cap() -> None:
    strategy = TransientRetryStrategy(
        RetryPolicy(max_attempts=2, base_delay_seconds=1, jitter_ratio=0, random_fn=lambda: 0)
    )
    transient = strategy.get_retry_decision(
        exception=TimeoutError(), job=SimpleNamespace(attempts=0)
    )
    assert transient is not None
    assert transient.retry_at is not None
    assert transient.retry_at - datetime.now(UTC) <= timedelta(seconds=2)
    assert (
        strategy.get_retry_decision(
            exception=ValueError("invalid token"), job=SimpleNamespace(attempts=0)
        )
        is None
    )
    assert (
        strategy.get_retry_decision(exception=TimeoutError(), job=SimpleNamespace(attempts=1))
        is None
    )


def test_idempotency_keys_are_deterministic_and_validated() -> None:
    first = build_idempotency_key("finance", "2026-09-02", "market-open")
    assert first == "finance:2026-09-02:market-open:v1"
    assert first == build_idempotency_key("finance", "2026-09-02", "market-open")
    assert validate_idempotency_key("review:repo-id:sha") == "review:repo-id:sha"
    for invalid in ("", "Finance:date:v1", "finance::v1", "finance:has space:v1"):
        with pytest.raises(IdempotencyKeyError):
            validate_idempotency_key(invalid)
    with pytest.raises(IdempotencyKeyError):
        build_idempotency_key("finance", "date", version="1")


def test_periodic_schedule_skips_nonexistent_spring_time() -> None:
    schedule = TorontoPeriodicSchedule(2, 30)
    result = schedule.next_occurrence(datetime(2025, 3, 9, 0, 0, tzinfo=UTC))
    assert result.local_time == datetime(2025, 3, 10, 2, 30, tzinfo=result.local_time.tzinfo)
    assert result.scheduled_at == datetime(2025, 3, 10, 6, 30, tzinfo=UTC)


def test_periodic_schedule_resolves_fall_back_once_and_key_uses_local_period() -> None:
    schedule = TorontoPeriodicSchedule(1, 30)
    result = schedule.next_occurrence(datetime(2025, 11, 2, 0, 0, tzinfo=UTC))
    assert result.local_time.fold == 0
    assert result.scheduled_at == datetime(2025, 11, 2, 5, 30, tzinfo=UTC)
    assert stable_period_key("finance", result) == "finance:2025-11-02:0130:v1"


def test_dynamic_schedule_matches_only_one_toronto_period() -> None:
    schedule = TorontoPeriodicSchedule.from_time(time(1, 30))
    first = datetime(2025, 11, 2, 5, 30, tzinfo=UTC).astimezone(schedule.zone)
    repeated = datetime(2025, 11, 2, 6, 30, tzinfo=UTC).astimezone(schedule.zone)

    assert schedule.matches(first)
    assert not schedule.matches(repeated)


def test_periodic_schedule_due_within_grace_catches_up_once() -> None:
    schedule = TorontoPeriodicSchedule.from_time(time(8, 0))
    exact = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    late = datetime(2026, 9, 10, 12, 20, tzinfo=UTC)
    stale = datetime(2026, 9, 10, 12, 31, tzinfo=UTC)

    exact_occurrence = schedule.due_within_grace(exact, grace=timedelta(minutes=30))
    late_occurrence = schedule.due_within_grace(late, grace=timedelta(minutes=30))

    assert exact_occurrence is not None
    assert exact_occurrence.scheduled_at == exact
    assert late_occurrence == exact_occurrence
    assert schedule.due_within_grace(stale, grace=timedelta(minutes=30)) is None


def test_periodic_schedule_due_within_grace_suppresses_repeated_fall_hour() -> None:
    schedule = TorontoPeriodicSchedule.from_time(time(1, 30))
    first = datetime(2025, 11, 2, 5, 30, tzinfo=UTC)
    repeated = datetime(2025, 11, 2, 6, 30, tzinfo=UTC)

    occurrence = schedule.due_within_grace(first, grace=timedelta(minutes=90))

    assert occurrence is not None
    assert occurrence.local_time.fold == 0
    assert schedule.due_within_grace(repeated, grace=timedelta(minutes=90)) is None


def test_queue_visibility_uses_explicit_status_and_heartbeat() -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    assert queue_visibility(QueueJobMetadata(1, "pending")) is QueueVisibility.QUEUED
    assert (
        queue_visibility(QueueJobMetadata(2, "running", started_at=now), now=now)
        is QueueVisibility.RUNNING
    )
    assert (
        queue_visibility(
            QueueJobMetadata(3, "running", heartbeat_at=now - timedelta(minutes=2)), now=now
        )
        is QueueVisibility.STALLED
    )
    assert queue_visibility(QueueJobMetadata(4, "retrying", attempts=1)) is QueueVisibility.RETRYING
    assert (
        queue_visibility(QueueJobMetadata(5, "failed", error_code="invalid_token"))
        is QueueVisibility.FAILED
    )
    assert queue_visibility(QueueJobMetadata(6, "succeeded")) is QueueVisibility.SUCCEEDED
    with pytest.raises(ValueError, match="timezone-aware"):
        queue_visibility(
            QueueJobMetadata(7, "running", started_at=datetime(2026, 9, 3)),  # noqa: DTZ001
            now=now,
        )


def test_procrastinate_app_is_configured_without_opening_connections() -> None:
    app = create_procrastinate_app(Settings(worker_concurrency=2))
    assert app.worker_defaults["concurrency"] == 2
    assert (
        postgres_conninfo("postgresql+psycopg://u:p@db:5432/lifeagent")
        == "postgresql://u:p@db:5432/lifeagent"
    )
    assert QUEUE_NAMES == ("academic_planner",)
    assert {
        tasks.discord_wake_task.queue,
        tasks.academic_clarification_task.queue,
        tasks.academic_clarification_status_task.queue,
        tasks.academic_material_ingestion_task.queue,
        tasks.academic_morning_notification_periodic.queue,
        tasks.academic_morning_notification_task.queue,
        tasks.artifact_retention_periodic.queue,
        tasks.shared_services_periodic.queue,
    } == {"academic_planner"}


def _morning_settings(*, schedule: time = time(8, 0), grace_minutes: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        academic_morning_schedule=schedule,
        app_timezone="America/Toronto",
        academic_morning_catchup_grace_minutes=grace_minutes,
    )


def _occurrence(local_time: datetime) -> PeriodicOccurrence:
    return PeriodicOccurrence(
        local_time=local_time,
        scheduled_at=local_time.astimezone(UTC),
    )


@pytest.mark.asyncio
async def test_academic_morning_periodic_defers_at_exact_configured_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[PeriodicOccurrence] = []

    async def defer(occurrence: PeriodicOccurrence) -> dict[str, object]:
        calls.append(occurrence)
        return {
            "status": "enqueued",
            "period_key": stable_period_key("academic-morning", occurrence),
        }

    monkeypatch.setattr(tasks, "_settings", _morning_settings())
    monkeypatch.setattr(tasks, "defer_academic_morning_notification", defer)

    result = await tasks.academic_morning_notification_periodic.func(
        timestamp=int(datetime(2026, 9, 10, 12, 0, tzinfo=UTC).timestamp())
    )

    assert result["status"] == "enqueued"
    assert calls[0].local_time == datetime(2026, 9, 10, 8, 0, tzinfo=ZoneInfo("America/Toronto"))
    assert result["period_key"] == "academic-morning:2026-09-10:0800:v1"


@pytest.mark.asyncio
async def test_academic_morning_periodic_skips_non_matching_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def defer(occurrence: PeriodicOccurrence) -> dict[str, object]:
        raise AssertionError(f"unexpected deferral for {occurrence}")

    monkeypatch.setattr(tasks, "_settings", _morning_settings())
    monkeypatch.setattr(tasks, "defer_academic_morning_notification", defer)

    result = await tasks.academic_morning_notification_periodic.func(
        timestamp=int(datetime(2026, 9, 10, 11, 59, tzinfo=UTC).timestamp())
    )

    assert result == {"status": "not_due"}


@pytest.mark.asyncio
async def test_academic_morning_periodic_catches_up_inside_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[PeriodicOccurrence] = []

    async def defer(occurrence: PeriodicOccurrence) -> dict[str, object]:
        calls.append(occurrence)
        return {"status": "enqueued"}

    monkeypatch.setattr(tasks, "_settings", _morning_settings(grace_minutes=30))
    monkeypatch.setattr(tasks, "defer_academic_morning_notification", defer)

    result = await tasks.academic_morning_notification_periodic.func(
        timestamp=int(datetime(2026, 9, 10, 12, 20, tzinfo=UTC).timestamp())
    )

    assert result == {"status": "enqueued"}
    assert calls[0].scheduled_at == datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_academic_morning_periodic_skips_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def defer(occurrence: PeriodicOccurrence) -> dict[str, object]:
        raise AssertionError(f"unexpected stale deferral for {occurrence}")

    monkeypatch.setattr(tasks, "_settings", _morning_settings(grace_minutes=30))
    monkeypatch.setattr(tasks, "defer_academic_morning_notification", defer)

    result = await tasks.academic_morning_notification_periodic.func(
        timestamp=int(datetime(2026, 9, 10, 12, 31, tzinfo=UTC).timestamp())
    )

    assert result == {"status": "not_due"}


async def test_shared_services_periodic_persists_the_aggregate_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'health.db'}")
    PersistedHealthCheck.__table__.create(engine)
    healthy = lambda name: HealthCheck(  # noqa: E731
        name=name,
        state=HealthState.HEALTHY,
        diagnostic=f"{name} healthy",
    )
    monkeypatch.setattr(tasks, "_database", SimpleNamespace(engine=engine))
    monkeypatch.setattr(tasks, "check_database", lambda _database: (healthy("database"),))
    monkeypatch.setattr(
        tasks,
        "_check_queue_with_settings",
        lambda _database, _settings, _now: healthy("queue"),
    )
    monkeypatch.setattr(tasks, "check_artifact_root", lambda _settings: healthy("artifacts"))
    monkeypatch.setattr(
        tasks,
        "check_connector_configuration",
        lambda _settings: healthy("connector_configuration"),
    )
    monkeypatch.setattr(
        tasks,
        "evaluate_academic_morning_health",
        lambda *_args, **_kwargs: SimpleNamespace(
            state=HealthState.HEALTHY,
            diagnostic="academic morning schedule healthy",
        ),
    )

    async def healthy_ollama(_settings: Settings, _client: httpx.AsyncClient) -> HealthCheck:
        return healthy("ollama")

    monkeypatch.setattr(tasks, "check_ollama", healthy_ollama)
    timestamp = int(datetime(2026, 9, 4, 12, 0, tzinfo=UTC).timestamp())

    try:
        result = await tasks.shared_services_periodic.func(timestamp=timestamp)
        with Session(engine) as session:
            persisted = session.query(PersistedHealthCheck).one()
    finally:
        engine.dispose()

    assert result["status"] == "healthy"
    assert persisted.check_name == "shared_services"
    assert persisted.state == "healthy"
    assert persisted.next_due_at is not None


@pytest.mark.asyncio
async def test_discord_wake_deferral_uses_global_model_lock_and_per_wake_queueing_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTask:
        def __init__(self) -> None:
            self.configuration: dict[str, str] = {}
            self.arguments: dict[str, str] = {}

        def configure(self, **kwargs: str) -> FakeTask:
            self.configuration = kwargs
            return self

        async def defer_async(self, **kwargs: str) -> int:
            self.arguments = kwargs
            return 42

    task = FakeTask()
    wake_id = "77777777-7777-4777-8777-777777777777"
    monkeypatch.setattr(tasks, "discord_wake_task", task)

    result = await tasks.defer_discord_wake(wake_id)

    assert result == 42
    assert task.configuration == {
        "lock": "ollama:exclusive",
        "queueing_lock": f"discord-wake:{wake_id}",
    }
    assert task.arguments == {
        "wake_id": wake_id,
    }


@pytest.mark.asyncio
async def test_academic_clarification_deferral_uses_per_clarification_lock_without_model_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTask:
        def __init__(self) -> None:
            self.configuration: dict[str, str] = {}
            self.arguments: dict[str, str] = {}

        def configure(self, **kwargs: str) -> FakeTask:
            self.configuration = kwargs
            return self

        async def defer_async(self, **kwargs: str) -> int:
            self.arguments = kwargs
            return 43

    task = FakeTask()
    clarification_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(tasks, "academic_clarification_task", task)

    result = await tasks.defer_academic_clarification(
        clarification_id=clarification_id,
        action="studying_block",
        user_id="123456789012345678",
    )

    assert result == 43
    assert task.configuration == {
        "lock": f"academic-clarification:{clarification_id}",
        "queueing_lock": f"academic-clarification:{clarification_id}",
    }
    assert task.arguments == {
        "clarification_id": clarification_id,
        "action": "studying_block",
        "user_id": "123456789012345678",
    }


@pytest.mark.asyncio
async def test_academic_clarification_status_deferral_has_independent_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTask:
        def __init__(self) -> None:
            self.configuration: dict[str, str] = {}
            self.arguments: dict[str, str] = {}

        def configure(self, **kwargs: str) -> FakeTask:
            self.configuration = kwargs
            return self

        async def defer_async(self, **kwargs: str) -> int:
            self.arguments = kwargs
            return 44

    task = FakeTask()
    clarification_id = "22222222-2222-4222-8222-222222222222"
    monkeypatch.setattr(tasks, "academic_clarification_status_task", task)

    result = await tasks.defer_academic_clarification_status(
        clarification_id=clarification_id,
        action="tutorial",
    )

    assert result == 44
    assert task.configuration == {
        "lock": f"academic-clarification:status:{clarification_id}",
        "queueing_lock": f"academic-clarification:status:{clarification_id}",
    }
    assert task.arguments == {
        "clarification_id": clarification_id,
        "action": "tutorial",
    }


@pytest.mark.asyncio
async def test_academic_clarification_locks_are_per_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTask:
        def __init__(self) -> None:
            self.configurations: list[dict[str, str]] = []

        def configure(self, **kwargs: str) -> FakeTask:
            self.configurations.append(kwargs)
            return self

        async def defer_async(self, **kwargs: str) -> int:
            del kwargs
            return len(self.configurations)

    task = FakeTask()
    first_id = "33333333-3333-4333-8333-333333333333"
    second_id = "44444444-4444-4444-8444-444444444444"
    monkeypatch.setattr(tasks, "academic_clarification_task", task)

    await tasks.defer_academic_clarification(
        clarification_id=first_id,
        action="quiz",
        user_id="123456789012345678",
    )
    await tasks.defer_academic_clarification(
        clarification_id=second_id,
        action="quiz",
        user_id="123456789012345678",
    )

    assert task.configurations == [
        {
            "lock": f"academic-clarification:{first_id}",
            "queueing_lock": f"academic-clarification:{first_id}",
        },
        {
            "lock": f"academic-clarification:{second_id}",
            "queueing_lock": f"academic-clarification:{second_id}",
        },
    ]


@pytest.mark.asyncio
async def test_academic_clarification_duplicate_queueing_lock_is_idempotent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTask:
        def configure(self, **kwargs: str) -> FakeTask:
            self.configuration = kwargs
            return self

        async def defer_async(self, **kwargs: str) -> int:
            self.arguments = kwargs
            raise AlreadyEnqueued("duplicate queueing lock")

    task = FakeTask()
    clarification_id = "55555555-5555-4555-8555-555555555555"
    monkeypatch.setattr(tasks, "academic_clarification_task", task)

    result = await tasks.defer_academic_clarification(
        clarification_id=clarification_id,
        action="quiz",
        user_id="123456789012345678",
    )

    assert result == {
        "status": "already_enqueued",
        "clarification_id": clarification_id,
    }
    assert task.configuration == {
        "lock": f"academic-clarification:{clarification_id}",
        "queueing_lock": f"academic-clarification:{clarification_id}",
    }
    assert task.arguments == {
        "clarification_id": clarification_id,
        "action": "quiz",
        "user_id": "123456789012345678",
    }


@pytest.mark.asyncio
async def test_academic_clarification_deferral_still_rejects_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTask:
        def configure(self, **kwargs: str) -> FakeTask:
            raise AssertionError("invalid input must fail before task configuration")

    monkeypatch.setattr(tasks, "academic_clarification_task", FakeTask())

    with pytest.raises(ValueError, match="badly formed hexadecimal UUID string"):
        await tasks.defer_academic_clarification(
            clarification_id="not-a-uuid",
            action="quiz",
            user_id="123456789012345678",
        )
    with pytest.raises(ValueError, match="academic clarification action is invalid"):
        await tasks.defer_academic_clarification(
            clarification_id="66666666-6666-4666-8666-666666666666",
            action="paper",
            user_id="123456789012345678",
        )


@pytest.mark.asyncio
async def test_material_ingestion_queue_payload_is_identifier_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTask:
        def configure(self, **kwargs: str) -> FakeTask:
            self.configuration = kwargs
            return self

        async def defer_async(self, **kwargs: str) -> int:
            self.arguments = kwargs
            return 17

    task = FakeTask()
    fingerprint = "a" * 64
    monkeypatch.setattr(tasks, "academic_material_ingestion_task", task)

    result = await tasks.defer_academic_material_ingestion("assessment-page", fingerprint)

    assert result == 17
    assert task.configuration == {
        "lock": "ollama:exclusive",
        "queueing_lock": f"academic-material:assessment-page:{fingerprint}",
    }
    assert task.arguments == {
        "assessment_page_id": "assessment-page",
        "source_fingerprint": fingerprint,
    }


@pytest.mark.asyncio
async def test_academic_morning_deferral_uses_period_key_without_model_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTask:
        def configure(self, **kwargs: str) -> FakeTask:
            self.configuration = kwargs
            return self

        async def defer_async(self, **kwargs: str) -> int:
            self.arguments = kwargs
            return 51

    task = FakeTask()
    run_id = uuid4()
    occurrence = _occurrence(datetime(2026, 9, 10, 8, 0, tzinfo=ZoneInfo("America/Toronto")))
    monkeypatch.setattr(tasks, "academic_morning_notification_task", task)
    monkeypatch.setattr(
        tasks,
        "_create_academic_morning_run",
        lambda **_kwargs: (run_id, "queued"),
    )

    result = await tasks.defer_academic_morning_notification(occurrence)

    assert result == {
        "status": "enqueued",
        "run_id": str(run_id),
        "period_key": "academic-morning:2026-09-10:0800:v1",
        "job_id": 51,
    }
    assert task.configuration == {"queueing_lock": "academic-morning:2026-09-10:0800:v1"}
    assert task.arguments == {
        "occurrence_at_iso": "2026-09-10T12:00:00+00:00",
        "run_id": str(run_id),
        "period_key": "academic-morning:2026-09-10:0800:v1",
    }


@pytest.mark.asyncio
async def test_academic_morning_duplicate_queueing_lock_is_idempotent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTask:
        def configure(self, **kwargs: str) -> FakeTask:
            self.configuration = kwargs
            return self

        async def defer_async(self, **kwargs: str) -> int:
            self.arguments = kwargs
            raise AlreadyEnqueued("duplicate queueing lock")

    task = FakeTask()
    run_id = uuid4()
    occurrence = _occurrence(datetime(2026, 9, 10, 8, 0, tzinfo=ZoneInfo("America/Toronto")))
    monkeypatch.setattr(tasks, "academic_morning_notification_task", task)
    monkeypatch.setattr(
        tasks,
        "_create_academic_morning_run",
        lambda **_kwargs: (run_id, "queued"),
    )

    result = await tasks.defer_academic_morning_notification(occurrence)

    assert result == {
        "status": "already_enqueued",
        "run_id": str(run_id),
        "period_key": "academic-morning:2026-09-10:0800:v1",
    }
    assert task.configuration == {"queueing_lock": "academic-morning:2026-09-10:0800:v1"}


@pytest.mark.asyncio
async def test_academic_morning_terminal_run_does_not_reenqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTask:
        def configure(self, **kwargs: str) -> FakeTask:
            raise AssertionError(f"terminal run should not configure task: {kwargs}")

    run_id = uuid4()
    occurrence = _occurrence(datetime(2026, 9, 10, 8, 0, tzinfo=ZoneInfo("America/Toronto")))
    monkeypatch.setattr(tasks, "academic_morning_notification_task", FakeTask())
    monkeypatch.setattr(
        tasks,
        "_create_academic_morning_run",
        lambda **_kwargs: (run_id, "succeeded"),
    )

    result = await tasks.defer_academic_morning_notification(occurrence)

    assert result == {
        "status": "already_complete",
        "run_id": str(run_id),
        "period_key": "academic-morning:2026-09-10:0800:v1",
    }


@pytest.mark.asyncio
async def test_academic_morning_task_records_attempt_with_anchored_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    period_key = "academic-morning:2026-09-10:0800:v1"
    handler_calls: list[tuple[str, str, str, int, int]] = []
    execution: dict[str, object] = {}

    async def handler(
        occurrence_at_iso: str,
        queued_run_id: str,
        queued_period_key: str,
        attempt: int,
        attempt_limit: int,
    ) -> dict[str, Any]:
        handler_calls.append(
            (occurrence_at_iso, queued_run_id, queued_period_key, attempt, attempt_limit)
        )
        return {"status": "succeeded", "delivery_count": 1}

    async def execute(operation: Any, **kwargs: object) -> dict[str, object]:
        execution.update(kwargs)
        return await operation()

    monkeypatch.setattr(tasks, "_academic_morning_notification_handler", handler)
    monkeypatch.setattr(tasks, "execute_recorded_attempt", execute)
    context = SimpleNamespace(job=SimpleNamespace(attempts=0))

    result = await tasks.academic_morning_notification_task.func(
        context,
        occurrence_at_iso="2026-09-10T12:00:00+00:00",
        run_id=str(run_id),
        period_key=period_key,
    )

    assert handler_calls == [
        (
            "2026-09-10T12:00:00+00:00",
            str(run_id),
            period_key,
            1,
            tasks.default_retry_strategy.policy.max_attempts,
        )
    ]
    assert execution["node_name"] == "queue.academic_morning_notification"
    assert execution["run_id"] == run_id
    assert execution["attempt"] == 1
    assert result == {
        "status": "succeeded",
        "delivery_count": 1,
        "run_id": str(run_id),
        "period_key": period_key,
    }


def test_worker_registers_discord_wake_and_ingestion_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    async def run_scheduled_morning_notification(
        occurrence_at_iso: str,
        run_id: str,
        period_key: str,
        attempt: int,
        attempt_limit: int,
    ) -> dict[str, Any]:
        return {
            "occurrence_at_iso": occurrence_at_iso,
            "run_id": run_id,
            "period_key": period_key,
            "attempt": attempt,
            "attempt_limit": attempt_limit,
        }

    module = ModuleType("app.agents.academic_planner.morning_notification")
    morning_module: Any = module
    morning_module.run_scheduled_morning_notification = run_scheduled_morning_notification
    monkeypatch.setitem(
        sys.modules,
        "app.agents.academic_planner.morning_notification",
        module,
    )
    import app.queue as queue_package

    sys.modules.pop("app.queue.worker", None)
    if hasattr(queue_package, "worker"):
        delattr(queue_package, "worker")
    worker: ModuleType | None = None

    try:
        worker = importlib.import_module("app.queue.worker")

        assert tasks._academic_clarification_handler is not None
        assert tasks._academic_clarification_status_handler is not None
        assert tasks._academic_morning_notification_handler is run_scheduled_morning_notification
        assert tasks._academic_material_ingestion_handler is not None
        assert tasks._discord_wake_handler is not None
    finally:
        sys.modules.pop("app.queue.worker", None)
        if worker is not None and getattr(queue_package, "worker", None) is worker:
            delattr(queue_package, "worker")


def test_default_periodic_registry_has_no_academic_model_schedule() -> None:
    registered = {
        task.task.name for task in procrastinate_app.periodic_registry.periodic_tasks.values()
    }

    assert registered == {
        "lifeagent.schedule.academic_morning_notification",
        "lifeagent.artifacts.retention",
        "lifeagent.health.shared_services",
    }
