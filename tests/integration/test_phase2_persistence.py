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
from app.db.academic import AcademicRepository, DocumentChunkInput, SourceCitation
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


def _upgrade(database_url: str, target: str = "head") -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, target)


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


def test_incomplete_temporal_assessment_query_uses_partial_index(
    postgres_engine: Engine,
) -> None:
    """Keep the default date-scoped model query off the historical-row scan path."""

    course_id = uuid.uuid4()
    calendar_id = uuid.uuid4()
    seed = uuid.uuid4().hex
    with postgres_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(
                text(
                    "INSERT INTO courses "
                    "(id, notion_id, course_code, title, term, timezone, active) "
                    "VALUES (:id, :notion_id, 'ECE 999', 'Contract Course', "
                    "'Fall 2026', 'America/Toronto', true)"
                ),
                {"id": course_id, "notion_id": f"contract-course-{seed}"},
            )
            connection.execute(
                text(
                    "INSERT INTO academic_course_calendars "
                    "(id, course_id, course_page_id, child_data_source_id, "
                    "title_property_id, date_property_id, discovery_status, last_synced_at) "
                    "VALUES (:id, :course_id, :page_id, :source_id, 'title', 'date', "
                    "'valid', '2026-09-20T00:00:00Z')"
                ),
                {
                    "id": calendar_id,
                    "course_id": course_id,
                    "page_id": f"contract-course-{seed}",
                    "source_id": f"contract-source-{seed}",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO assessments "
                    "(id, course_id, notion_id, title, assessment_type, due_at, "
                    "estimated_minutes, fact_state, confidence, completed, source_id, "
                    "notion_last_edited_at, title_property_id, active, archived, is_all_day) "
                    "SELECT (substr(md5(:seed || i::text),1,8) || '-' || "
                    "substr(md5(:seed || i::text),9,4) || '-' || "
                    "substr(md5(:seed || i::text),13,4) || '-' || "
                    "substr(md5(:seed || i::text),17,4) || '-' || "
                    "substr(md5(:seed || i::text),21,12))::uuid, "
                    ":course_id, :seed || '-assessment-' || i, 'Assessment ' || i, "
                    "'assignment', '2026-01-01T00:00:00Z'::timestamptz + "
                    "(i % 365) * interval '1 day' + (i % 24) * interval '1 hour', "
                    "60, 'confirmed', 1.0, false, :source_id, "
                    "'2026-09-20T00:00:00Z', 'title', true, false, (i % 10 = 0) "
                    "FROM generate_series(1, 100000) AS generated(i)"
                ),
                {
                    "seed": seed,
                    "course_id": course_id,
                    "source_id": f"contract-source-{seed}",
                },
            )
            connection.exec_driver_sql("ANALYZE assessments")
            plan = "\n".join(
                str(row[0])
                for row in connection.exec_driver_sql(
                    "EXPLAIN (ANALYZE, BUFFERS) "
                    "SELECT a.id, a.title, a.due_at FROM assessments a "
                    "JOIN courses c ON c.id = a.course_id "
                    "JOIN academic_course_calendars acc ON acc.course_id = c.id "
                    "WHERE c.active IS TRUE AND a.active IS TRUE "
                    "AND a.archived IS FALSE AND a.completed IS FALSE "
                    "AND a.notion_last_edited_at IS NOT NULL "
                    "AND acc.discovery_status = 'valid' AND "
                    "(((a.is_all_day IS FALSE) "
                    "AND a.due_at >= '2026-09-19T04:00:00Z' "
                    "AND a.due_at < '2026-09-20T04:00:00Z') OR "
                    "((a.is_all_day IS TRUE) "
                    "AND a.due_at >= '2026-09-19T00:00:00Z' "
                    "AND a.due_at < '2026-09-20T00:00:00Z')) "
                    "ORDER BY a.due_at, a.title, a.id LIMIT 21"
                )
            )
            assert "ix_assessments_active_incomplete_temporal" in plan
        finally:
            transaction.rollback()


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


def test_assessment_material_vectors_are_active_versioned_and_scope_isolated(
    db_session: Session,
) -> None:
    suffix = uuid.uuid4().hex
    first_course = AcademicRepository.upsert_course(
        db_session,
        notion_id=f"course-material-a-{suffix}",
        course_code=f"ECE-{suffix[:6]}",
        title="Circuits",
        term="2026F",
    )
    second_course = AcademicRepository.upsert_course(
        db_session,
        notion_id=f"course-material-b-{suffix}",
        course_code=f"HIST-{suffix[:6]}",
        title="History",
        term="2026F",
    )
    first_assessment = AcademicRepository.upsert_assessment(
        db_session,
        notion_id=f"assessment-material-a-{suffix}",
        course_id=first_course.id,
        title="Circuit assignment",
        assessment_type="assignment",
        due_at=datetime.now(UTC) + timedelta(days=1),
        grade_weight_percent=40,
        estimated_minutes=60,
        confidence=1.0,
        fact_state="confirmed",
        citation=SourceCitation(block="assessment-a"),
    )
    second_assessment = AcademicRepository.upsert_assessment(
        db_session,
        notion_id=f"assessment-material-b-{suffix}",
        course_id=second_course.id,
        title="History essay",
        assessment_type="essay",
        due_at=datetime.now(UTC) + timedelta(days=2),
        grade_weight_percent=30,
        estimated_minutes=90,
        confidence=1.0,
        fact_state="confirmed",
        citation=SourceCitation(block="assessment-b"),
    )
    first_document = AcademicRepository.upsert_document(
        db_session,
        notion_id=first_assessment.notion_id,
        document_version=f"v1-{suffix}",
        title="Circuit requirements",
        document_type="assessment_material",
        retrieved_at=datetime.now(UTC),
        artifact_key="a" * 64,
        content_hash="b" * 64,
        assessment_id=first_assessment.id,
        source_kind="notion_page_body",
        source_page_id=first_assessment.notion_id,
        source_key=f"{first_assessment.notion_id}:body",
        media_type="text/plain",
        extraction_status="extracted",
        active=False,
    )
    second_document = AcademicRepository.upsert_document(
        db_session,
        notion_id=second_assessment.notion_id,
        document_version=f"v1-{suffix}",
        title="Essay requirements",
        document_type="assessment_material",
        retrieved_at=datetime.now(UTC),
        artifact_key="c" * 64,
        content_hash="d" * 64,
        assessment_id=second_assessment.id,
        source_kind="notion_page_body",
        source_page_id=second_assessment.notion_id,
        source_key=f"{second_assessment.notion_id}:body",
        media_type="text/plain",
        extraction_status="extracted",
        active=False,
    )
    first_chunks = AcademicRepository.replace_document_chunks(
        db_session,
        document_id=first_document.id,
        chunks=(
            DocumentChunkInput(
                ordinal=0,
                heading="Requirements",
                content="Review linear circuits and compare AC with DC behavior.",
                citation=SourceCitation(block="circuit-requirements"),
                embedding=(1.0, *([0.0] * 1023)),
                embedding_model="integration-embedding:v1",
            ),
        ),
    )
    AcademicRepository.replace_document_chunks(
        db_session,
        document_id=second_document.id,
        chunks=(
            DocumentChunkInput(
                ordinal=0,
                heading="Requirements",
                content="Compare two primary historical sources.",
                citation=SourceCitation(block="essay-requirements"),
                embedding=(0.0, 1.0, *([0.0] * 1022)),
                embedding_model="integration-embedding:v1",
            ),
        ),
    )
    AcademicRepository.activate_document_version(db_session, document_id=first_document.id)
    AcademicRepository.activate_document_version(db_session, document_id=second_document.id)

    matches = AcademicRepository.search_semantic_document_chunks(
        db_session,
        assessment_id=first_assessment.id,
        query_embedding=(1.0, *([0.0] * 1023)),
        embedding_model="integration-embedding:v1",
        limit=8,
    )

    assert [(row.id, score) for row, score in matches] == [(first_chunks[0].id, 1.0)]

    query_vector = "[1," + ",".join("0" for _ in range(1023)) + "]"
    db_session.execute(
        text(
            """
            INSERT INTO academic_document_chunks (
                id, document_id, ordinal, content, content_hash,
                embedding, embedding_model, embedding_dimensions
            )
            SELECT
                md5(:seed || series::text)::uuid,
                :document_id,
                1000 + series,
                'indexed retrieval fixture',
                repeat('e', 64),
                CAST(:embedding AS vector(1024)),
                :embedding_model,
                1024
            FROM generate_series(1, 512) AS series
            """
        ),
        {
            "seed": suffix,
            "document_id": first_document.id,
            "embedding": query_vector,
            "embedding_model": "integration-embedding:v1",
        },
    )
    db_session.execute(text("ANALYZE academic_document_chunks"))
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = "\n".join(
        row[0]
        for row in db_session.execute(
            text(
                """
                EXPLAIN
                SELECT id
                FROM academic_document_chunks
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:embedding AS vector(1024))
                LIMIT 8
                """
            ),
            {"embedding": query_vector},
        )
    )
    assert "ix_academic_chunks_embedding_hnsw" in plan

    index_names = set(
        db_session.scalars(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename IN (
                    'academic_document_chunks',
                    'academic_reflection_memories'
                  )
                """
            )
        )
    )
    assert "ix_academic_chunks_embedding_hnsw" in index_names
    assert "ix_academic_reflections_embedding_hnsw" in index_names


def test_embedding_migration_preserves_raw_rows_and_clears_only_stale_vectors() -> None:
    container = PostgresContainer(
        "pgvector/pgvector:0.8.6-pg16-bookworm",
        driver="psycopg",
    )
    container.start()
    database_url = container.get_connection_url()
    engine: Engine | None = None
    try:
        _upgrade(database_url, "0024_career_link_schema_repair")
        engine = create_engine(database_url, pool_pre_ping=True)
        stale_id = uuid.uuid4()
        current_id = uuid.uuid4()
        null_id = uuid.uuid4()
        stale_memory_id = uuid.uuid4()
        current_memory_id = uuid.uuid4()
        null_memory_id = uuid.uuid4()
        document_id = uuid.uuid4()
        focus_id = uuid.uuid4()
        current_vector = "[1," + ",".join("0" for _ in range(1023)) + "]"
        with engine.begin() as connection:
            connection.exec_driver_sql("SET session_replication_role = replica")
            for ordinal, row_id, vector, model, dimensions in (
                (1, stale_id, "[1,0,0]", "qwen3-embedding:0.6b", 3),
                (2, current_id, current_vector, "qwen3-embedding:4b@current", 1024),
                (3, null_id, None, None, None),
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO academic_document_chunks (
                            id, document_id, ordinal, content, content_hash,
                            embedding, embedding_model, embedding_dimensions
                        ) VALUES (
                            :id, :document_id, :ordinal, :content, repeat('a', 64),
                            CAST(:embedding AS vector), :model, :dimensions
                        )
                        """
                    ),
                    {
                        "id": row_id,
                        "document_id": document_id,
                        "ordinal": ordinal,
                        "content": f"preserved chunk {ordinal}",
                        "embedding": vector,
                        "model": model,
                        "dimensions": dimensions,
                    },
                )
            for row_id, raw_text, vector, model, dimensions in (
                (
                    stale_memory_id,
                    "preserved stale reflection",
                    "[1,0,0]",
                    "qwen3-embedding:0.6b",
                    3,
                ),
                (
                    current_memory_id,
                    "preserved current reflection",
                    current_vector,
                    "qwen3-embedding:4b@current",
                    1024,
                ),
                (null_memory_id, "preserved null reflection", None, None, None),
            ):
                connection.execute(
                    text(
                        """
                        INSERT INTO academic_reflection_memories (
                            id, focus_id, raw_text, embedding, embedding_model,
                            embedding_dimensions, recorded_at
                        ) VALUES (
                            :id, :focus_id, :raw_text, CAST(:embedding AS vector),
                            :model, :dimensions, CURRENT_TIMESTAMP
                        )
                        """
                    ),
                    {
                        "id": row_id,
                        "focus_id": focus_id,
                        "raw_text": raw_text,
                        "embedding": vector,
                        "model": model,
                        "dimensions": dimensions,
                    },
                )
            connection.exec_driver_sql("SET session_replication_role = origin")
        engine.dispose()
        engine = None

        _upgrade(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            chunks = {
                row.id: row
                for row in connection.execute(
                    text(
                        """
                        SELECT id, content, vector_dims(embedding) AS dimensions, embedding_model
                        FROM academic_document_chunks
                        WHERE id IN (:stale_id, :current_id, :null_id)
                        """
                    ),
                    {
                        "stale_id": stale_id,
                        "current_id": current_id,
                        "null_id": null_id,
                    },
                )
            }
            memories = {
                row.id: row
                for row in connection.execute(
                    text(
                        """
                        SELECT id, raw_text, vector_dims(embedding) AS dimensions, embedding_model
                        FROM academic_reflection_memories
                        WHERE id IN (:stale_id, :current_id, :null_id)
                        """
                    ),
                    {
                        "stale_id": stale_memory_id,
                        "current_id": current_memory_id,
                        "null_id": null_memory_id,
                    },
                )
            }

        assert {row.content for row in chunks.values()} == {
            "preserved chunk 1",
            "preserved chunk 2",
            "preserved chunk 3",
        }
        assert chunks[stale_id].dimensions is None
        assert chunks[stale_id].embedding_model is None
        assert chunks[current_id].dimensions == 1024
        assert chunks[null_id].dimensions is None
        assert {row.raw_text for row in memories.values()} == {
            "preserved stale reflection",
            "preserved current reflection",
            "preserved null reflection",
        }
        assert memories[stale_memory_id].dimensions is None
        assert memories[stale_memory_id].embedding_model is None
        assert memories[current_memory_id].dimensions == 1024
        assert memories[null_memory_id].dimensions is None
    finally:
        if engine is not None:
            engine.dispose()
        container.stop()


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
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.target_type == "finance_source_allowlist",
                AuditEvent.target_id == allowlist_version,
                AuditEvent.action == "record_finance_source_allowlist",
            )
        )
        == 1
    )
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


async def test_historical_finance_approval_gate_persists_attention_without_schedule(
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
    # Historical runs remain auditable, but the event-driven runtime does not
    # schedule another finance model run.
    assert health.next_due_at is None


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
