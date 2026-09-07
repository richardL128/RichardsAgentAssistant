"""PostgreSQL acceptance tests for Phase 2 persistence primitives."""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from app.core.errors import ErrorCode, LifeAgentError, authorization_error, transient_error
from app.db.finance import FinanceApprovedSource, FinanceRepository, FinanceSourceEndpoint
from app.db.models import (
    AgentRun,
    ApprovalState,
    AuditEvent,
    Delivery,
    HealthCheck,
    RunStatus,
    RunStep,
)
from app.db.repositories import (
    ApprovalRepository,
    AuditRepository,
    DeliveryRepository,
    RunRepository,
)
from app.health.evaluator import DeliveryStatus as HealthDeliveryStatus
from app.health.evaluator import OperationalFacts, ProcessingStatus
from app.health.service import evaluate_and_persist
from app.queue.execution import execute_recorded_attempt
from app.queue.retry import RetryPolicy
from app.queue.visibility import QueueVisibility, list_queue_jobs, queue_visibility

DATABASE_URL = os.environ.get("LIFEAGENT_TEST_DATABASE_URL")


def _attempt_audit_update(session: Session, event_id: uuid.UUID) -> None:
    with session.begin_nested():
        session.execute(
            update(AuditEvent).where(AuditEvent.id == event_id).values(result="tampered")
        )
        session.flush()


def _attempt_audit_delete(session: Session, event_id: uuid.UUID) -> None:
    with session.begin_nested():
        session.execute(delete(AuditEvent).where(AuditEvent.id == event_id))
        session.flush()


def _upgrade(database_url: str) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


@pytest.fixture(scope="module")
def postgres_engine() -> Generator[Engine, None, None]:
    """Use an explicit database or create a disposable PostgreSQL 16."""

    container: PostgresContainer | None = None
    database_url = DATABASE_URL
    if database_url is None:
        container = PostgresContainer(
            "pgvector/pgvector:0.8.6-pg16-bookworm",
            driver="psycopg",
        )
        container.start()
        database_url = container.get_connection_url()
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        if container is not None:
            container.stop()
        pytest.fail("LIFEAGENT_TEST_DATABASE_URL must point to PostgreSQL")
    _upgrade(database_url)
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(select(1))
            tables = connection.exec_driver_sql(
                "SELECT to_regclass('public.agent_runs'), to_regclass('public.audit_events')"
            ).one()
    except DBAPIError:
        engine.dispose()
        if container is not None:
            container.stop()
        raise
    if tables[0] is None or tables[1] is None:
        engine.dispose()
        pytest.fail("Phase 2 migration is not applied; run `alembic upgrade head` first")
    yield engine
    engine.dispose()
    if container is not None:
        container.stop()


@pytest.fixture
def db_session(postgres_engine) -> Generator[Session, None, None]:
    with Session(postgres_engine) as session:
        yield session
        session.rollback()


def test_duplicate_run_idempotency_returns_one_row(db_session: Session) -> None:
    key = f"integration-run-{uuid.uuid4()}"
    first = RunRepository.create_or_get(
        db_session,
        idempotency_key=key,
        agent_name="integration",
        trigger="test",
    )
    second = RunRepository.create_or_get(
        db_session,
        idempotency_key=key,
        agent_name="different-input",
        trigger="replay",
    )
    assert first.id == second.id
    assert (
        db_session.scalar(
            select(func.count()).select_from(AgentRun).where(AgentRun.idempotency_key == key)
        )
        == 1
    )


def test_duplicate_delivery_intent_is_suppressed(db_session: Session) -> None:
    key = f"integration-delivery-{uuid.uuid4()}"
    first = DeliveryRepository.create_or_get_intent(
        db_session,
        channel="discord",
        target="test-channel",
        idempotency_key=key,
    )
    second = DeliveryRepository.create_or_get_intent(
        db_session,
        channel="discord",
        target="different-target",
        idempotency_key=key,
    )
    assert first.id == second.id
    assert second.target == "test-channel"
    assert (
        db_session.scalar(
            select(func.count()).select_from(Delivery).where(Delivery.idempotency_key == key)
        )
        == 1
    )


def test_audit_events_are_append_only(db_session: Session) -> None:
    event = AuditRepository.append(
        db_session,
        actor="integration-test",
        action="test.created",
        target_type="test",
        target_id=str(uuid.uuid4()),
        result="ok",
    )
    with pytest.raises(SQLAlchemyError):
        _attempt_audit_update(db_session, event.id)

    with pytest.raises(SQLAlchemyError):
        _attempt_audit_delete(db_session, event.id)

    assert db_session.scalar(select(AuditEvent.result).where(AuditEvent.id == event.id)) == "ok"


def test_phase6_finance_allowlist_is_seeded_disabled_and_audited(
    db_session: Session,
) -> None:
    allowlist_version = "finance-sources-2026.09"
    rows = tuple(
        db_session.scalars(
            select(FinanceApprovedSource)
            .where(FinanceApprovedSource.allowlist_version == allowlist_version)
            .order_by(FinanceApprovedSource.source_id)
        )
    )

    assert {
        row.source_id: (
            row.source_version,
            row.classification,
            row.license_allows_excerpt,
            row.excerpt_max_chars,
            row.excerpt_max_words,
        )
        for row in rows
    } == {
        "alpha_vantage_news": ("news-sentiment-v1", "reported", True, 500, None),
        "alpha_vantage_etf": ("etf-profile-v1", "secondary", False, None, None),
        "benzinga_news": ("benzinga-news-v2", "reported", True, 500, None),
        "breaking_defense": ("wp-rest-v2", "reported", True, 200, None),
        "dvids": ("dvids-search-v1", "primary", True, 300, None),
        "eia_open_data": ("eia-api-v2", "primary", True, None, None),
        "federal_register_energy": (
            "federal-register-api-v1",
            "primary",
            True,
            500,
            None,
        ),
        "fmp_etf": ("fmp-api-v3", "secondary", False, None, None),
    }
    assert all(not row.enabled for row in rows)
    assert all(row.approved_at is None for row in rows)
    assert all(row.approval_audit_id is None for row in rows)
    assert (
        FinanceRepository.source_approval_gate(
            db_session,
            allowlist_version=allowlist_version,
        )
        is False
    )
    audit_id = uuid.uuid5(uuid.NAMESPACE_URL, f"lifeagent:{allowlist_version}:recorded")
    audit = db_session.get(AuditEvent, audit_id)
    assert audit is not None
    assert audit.actor == "richard"
    assert audit.action == "record_finance_source_allowlist"
    assert audit.target_id == allowlist_version


def test_public_finance_v2_coexists_disabled_with_reviewed_endpoints(
    db_session: Session,
) -> None:
    allowlist_version = "finance-sources-2026.09-v2"
    rows = tuple(
        db_session.scalars(
            select(FinanceApprovedSource)
            .where(FinanceApprovedSource.allowlist_version == allowlist_version)
            .order_by(FinanceApprovedSource.source_id)
        )
    )
    endpoints = tuple(
        db_session.scalars(
            select(FinanceSourceEndpoint).where(
                FinanceSourceEndpoint.allowlist_version == allowlist_version
            )
        )
    )

    assert {row.source_id for row in rows} == {
        "defense_gov_rss",
        "breaking_defense_public",
        "eia_public_data",
        "federal_register_energy",
        "sec_edgar",
        "company_ir_registry",
        "issuer_etf_holdings",
        "technology_official_feeds",
    }
    assert all(not row.enabled for row in rows)
    assert all(row.approved_at is None and row.approval_audit_id is None for row in rows)
    assert len(endpoints) == 9
    assert len({(row.source_id, row.endpoint_id) for row in endpoints}) == 9
    assert db_session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.target_type == "finance_source_allowlist",
            AuditEvent.target_id == allowlist_version,
            AuditEvent.action == "record_finance_source_allowlist",
        )
    ) == 1
    assert FinanceRepository.source_approval_gate(
        db_session,
        allowlist_version=allowlist_version,
    ) is False

    audit_id = uuid.uuid5(uuid.NAMESPACE_URL, f"lifeagent:{allowlist_version}:recorded")
    audit = db_session.get(AuditEvent, audit_id)
    assert audit is not None
    assert audit.actor == "richard"
    assert audit.action == "record_finance_source_allowlist"
    assert audit.target_id == allowlist_version


def test_run_lifecycle_status_is_durable(db_session: Session) -> None:
    run = RunRepository.create_or_get(
        db_session,
        idempotency_key=f"integration-lifecycle-{uuid.uuid4()}",
        agent_name="integration",
        trigger="test",
    )
    RunRepository.set_status(db_session, run.id, RunStatus.RUNNING)
    run_id = run.id
    db_session.commit()
    db_session.expunge_all()
    reloaded = db_session.get(AgentRun, run_id)
    assert reloaded is not None
    assert reloaded.status == RunStatus.RUNNING
    assert reloaded.started_at.tzinfo is not None


def test_approval_creation_and_terminal_transition_are_idempotent(db_session: Session) -> None:
    key = f"approval:{uuid.uuid4()}:v1"
    first = ApprovalRepository.create_or_get(
        db_session,
        idempotency_key=key,
        operation="fixture.publish",
        requester="integration-test",
        redacted_preview="safe preview",
    )
    duplicate = ApprovalRepository.create_or_get(
        db_session,
        idempotency_key=key,
        operation="different.operation",
        requester="different-requester",
    )
    assert first.id == duplicate.id

    request, event = ApprovalRepository.transition(
        db_session,
        first.id,
        ApprovalState.APPROVED,
        actor="integration-test",
    )
    replayed, replayed_event = ApprovalRepository.transition(
        db_session,
        first.id,
        ApprovalState.APPROVED,
        actor="integration-test",
    )
    assert request.id == replayed.id
    assert event.id == replayed_event.id
    with pytest.raises(ValueError, match="terminal state"):
        ApprovalRepository.transition(
            db_session,
            first.id,
            ApprovalState.REJECTED,
            actor="integration-test",
        )


async def test_transient_retries_record_attempts_but_invalid_token_does_not(
    postgres_engine: Engine,
) -> None:
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_seconds=1,
        max_delay_seconds=2,
        jitter_ratio=0,
    )
    with Session(postgres_engine) as session, session.begin():
        transient_run = RunRepository.create_or_get(
            session,
            idempotency_key=f"retry:{uuid.uuid4()}:v1",
            agent_name="integration",
            trigger="test",
        )
        auth_run = RunRepository.create_or_get(
            session,
            idempotency_key=f"auth:{uuid.uuid4()}:v1",
            agent_name="integration",
            trigger="test",
        )
        transient_run_id = transient_run.id
        auth_run_id = auth_run.id

    calls = 0

    async def succeeds_after_transient() -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "provider unavailable")
        return {"ok": True}

    with pytest.raises(LifeAgentError, match=ErrorCode.CONNECTOR_TRANSIENT.value):
        await execute_recorded_attempt(
            succeeds_after_transient,
            engine=postgres_engine,
            run_id=transient_run_id,
            node_name="fixture.transient",
            attempt=1,
            retry_policy=policy,
        )
    result = await execute_recorded_attempt(
        succeeds_after_transient,
        engine=postgres_engine,
        run_id=transient_run_id,
        node_name="fixture.transient",
        attempt=2,
        retry_policy=policy,
    )
    assert result == {"ok": True}

    async def invalid_token() -> dict[str, object]:
        raise authorization_error()

    with pytest.raises(LifeAgentError, match=ErrorCode.AUTHORIZATION_INVALID.value):
        await execute_recorded_attempt(
            invalid_token,
            engine=postgres_engine,
            run_id=auth_run_id,
            node_name="fixture.auth",
            attempt=1,
            retry_policy=policy,
        )

    with Session(postgres_engine) as session:
        transient_steps = list(
            session.scalars(
                select(RunStep).where(RunStep.run_id == transient_run_id).order_by(RunStep.attempt)
            )
        )
        auth_steps = list(session.scalars(select(RunStep).where(RunStep.run_id == auth_run_id)))
        transient_status = session.get(AgentRun, transient_run_id)
        auth_status = session.get(AgentRun, auth_run_id)
        operational_health = session.scalar(
            select(HealthCheck).where(HealthCheck.check_name == "integration")
        )

    assert [step.attempt for step in transient_steps] == [1, 2]
    assert [step.status for step in transient_steps] == ["attention", "succeeded"]
    assert transient_status is not None
    assert transient_status.status == RunStatus.SUCCEEDED
    assert len(auth_steps) == 1
    assert auth_status is not None
    assert auth_status.status == RunStatus.FAILED
    assert auth_status.error_code == ErrorCode.AUTHORIZATION_INVALID.value
    assert operational_health is not None
    assert operational_health.state == "failed"
    assert operational_health.rule == "connector_unauthenticated"


async def test_finance_approval_gate_persists_attention_health(
    postgres_engine: Engine,
) -> None:
    key = f"finance:{uuid.uuid4()}:v1"
    with Session(postgres_engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=key,
            agent_name="finance",
            trigger="schedule",
            schedule="finance-market-open",
        )
        run_id = run.id

    async def approval_required() -> dict[str, object]:
        return {"status": "approval_required", "delivered": False}

    result = await execute_recorded_attempt(
        approval_required,
        engine=postgres_engine,
        run_id=run_id,
        node_name="queue.finance",
        attempt=1,
        retry_policy=RetryPolicy(max_attempts=3),
    )

    with Session(postgres_engine) as session:
        run = session.get(AgentRun, run_id)
        health = session.scalar(select(HealthCheck).where(HealthCheck.check_name == "finance"))

    assert result["status"] == "approval_required"
    assert run is not None
    assert run.status == RunStatus.ATTENTION
    assert health is not None
    assert health.state == "attention"
    assert health.rule == "waiting_for_approval"
    assert health.next_due_at is not None


def test_failed_and_stalled_jobs_are_visible_without_job_arguments(
    postgres_engine: Engine,
) -> None:
    with postgres_engine.begin() as connection:
        worker_id = connection.execute(
            text(
                "INSERT INTO procrastinate_workers(last_heartbeat) VALUES (:heartbeat) RETURNING id"
            ),
            {"heartbeat": datetime.now(UTC) - timedelta(minutes=10)},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO procrastinate_jobs "
                "(queue_name, task_name, status, attempts, worker_id, args) "
                "VALUES "
                "('code_review', 'fixture.stalled', 'doing', 1, :worker_id, :args), "
                "('code_review', 'fixture.failed', 'failed', 3, NULL, :args)"
            ),
            {"worker_id": worker_id, "args": '{"private":"must-not-return"}'},
        )

    records = list_queue_jobs(
        postgres_engine,
        queues=frozenset({"code_review"}),
        max_attempts=3,
    )
    fixtures = {
        record.task_name: record for record in records if record.task_name.startswith("fixture")
    }

    assert queue_visibility(fixtures["fixture.stalled"]) is QueueVisibility.STALLED
    assert queue_visibility(fixtures["fixture.failed"]) is QueueVisibility.FAILED
    assert not hasattr(fixtures["fixture.failed"], "args")


def test_model_free_health_evaluation_is_persisted(db_session: Session) -> None:
    now = datetime.now(UTC)
    result = evaluate_and_persist(
        db_session,
        OperationalFacts(
            component=f"integration-{uuid.uuid4()}",
            processing=ProcessingStatus.SUCCEEDED,
            delivery=HealthDeliveryStatus.SUCCEEDED,
            evaluated_at=now,
            last_success_at=now,
            next_expected_at=now + timedelta(days=1),
            diagnostic_code="fixture_succeeded",
        ),
    )

    assert result.state.value == "healthy"
