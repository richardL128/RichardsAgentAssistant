from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

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
from app.queue.periodic import TorontoPeriodicSchedule, stable_period_key
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
    assert QUEUE_NAMES == ("code_review", "academic_planner", "finance")
    assert {
        tasks.code_review_task.queue,
        tasks.discord_wake_task.queue,
        tasks.finance_task.queue,
    } == set(QUEUE_NAMES)


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


def test_task_deferral_uses_global_model_lock_and_per_item_queueing_lock() -> None:
    class FakeTask:
        def __init__(self) -> None:
            self.configuration: dict[str, str] = {}
            self.arguments: dict[str, str] = {}

        def configure(self, **kwargs: str) -> FakeTask:
            self.configuration = kwargs
            return self

        def defer(self, **kwargs: str) -> int:
            self.arguments = kwargs
            return 42

    task = FakeTask()
    result = tasks.defer_idempotent(task, "run-id", "review:repo:sha")

    assert result == 42
    assert task.configuration == {
        "lock": "ollama:exclusive",
        "queueing_lock": "review:repo:sha",
    }
    assert task.arguments == {
        "run_id": "run-id",
        "idempotency_key": "review:repo:sha",
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


def test_worker_registers_discord_wake_and_ingestion_handlers() -> None:
    import importlib

    from app.queue import worker

    importlib.reload(worker)

    assert tasks._academic_clarification_handler is not None
    assert tasks._academic_clarification_status_handler is not None
    assert tasks._academic_material_ingestion_handler is not None
    assert tasks._discord_wake_handler is not None
    assert "academic_planner" not in tasks._handlers


def test_default_periodic_registry_has_no_academic_model_schedule() -> None:
    registered = {
        task.task.name for task in procrastinate_app.periodic_registry.periodic_tasks.values()
    }

    assert registered == {
        "lifeagent.artifacts.retention",
        "lifeagent.health.shared_services",
    }
