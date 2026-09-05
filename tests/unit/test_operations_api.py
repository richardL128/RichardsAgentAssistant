from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.operations import router
from app.core.config import Settings
from app.db.finance import FinanceRepository
from app.db.models import (
    AgentRun,
    AuditEvent,
    Base,
    Delivery,
    DeliveryStatus,
    EvidenceClassification,
    EvidenceRef,
    HealthCheck,
    HealthState,
    RunStatus,
    UIAcknowledgement,
)

NOW = datetime(2026, 9, 4, 13, tzinfo=UTC)
AUTH = ("richard", "private-password")


class DatabaseState:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine


class QueueTrap:
    called = False

    async def __call__(self, *_: Any, **__: Any) -> None:
        self.called = True
        raise AssertionError("operations API must not enqueue jobs")


def _app(
    engine: Engine | None,
    *,
    settings: Settings | None = None,
    queue_trap: QueueTrap | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings or Settings(
        ops_console_username=SecretStr(AUTH[0]),
        ops_console_password=SecretStr(AUTH[1]),
        finance_source_allowlist_version="finance-sources-test",
    )
    if engine is not None:
        app.state.database = DatabaseState(engine)
    app.state.enqueue_code_review = queue_trap or QueueTrap()
    app.include_router(router)
    return app


@contextmanager
def _client(tmp_path: Path) -> Generator[tuple[TestClient, Engine, QueueTrap]]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / f'{uuid4()}.db'}")
    Base.metadata.create_all(engine)
    queue_trap = QueueTrap()
    try:
        with TestClient(_app(engine, queue_trap=queue_trap)) as client:
            yield client, engine, queue_trap
    finally:
        engine.dispose()


def _run(
    *,
    agent_name: str = "finance",
    status: RunStatus = RunStatus.SUCCEEDED,
    summary: str = "Finance run completed",
    started_at: datetime = NOW,
    error_code: str | None = None,
) -> AgentRun:
    return AgentRun(
        id=uuid4(),
        idempotency_key=f"{agent_name}:{uuid4()}",
        agent_name=agent_name,
        trigger="schedule",
        schedule="daily",
        status=status,
        summary=summary,
        error_code=error_code,
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=5),
    )


def _seed_activity(engine: Engine) -> tuple[UUID, UUID]:
    with Session(engine) as session, session.begin():
        finance = _run(
            agent_name="finance",
            status=RunStatus.ATTENTION,
            summary="Ticker ACME needs attention",
            started_at=NOW,
            error_code="source_failed",
        )
        code = _run(
            agent_name="code_review",
            status=RunStatus.SUCCEEDED,
            summary="<script>alert('x')</script> repo scan completed",
            started_at=NOW - timedelta(days=1),
        )
        session.add_all((finance, code))
        session.flush()
        finance_id = finance.id
        code_id = code.id
        session.add_all(
            (
                Delivery(
                    run_id=finance.id,
                    channel="discord",
                    target="finance",
                    idempotency_key=f"delivery:{finance.id}",
                    status=DeliveryStatus.SENT,
                    attempt_count=1,
                    last_attempt_at=finance.finished_at,
                    external_url="https://discord.example/messages/1",
                ),
                EvidenceRef(
                    run_id=finance.id,
                    claim_id="claim-1",
                    title="Primary filing",
                    url="https://source.example/filing",
                    published_at=NOW - timedelta(hours=2),
                    retrieved_at=NOW,
                    classification=EvidenceClassification.PRIMARY,
                ),
                AuditEvent(
                    actor="finance",
                    action="record_theme",
                    target_type="ticker_theme",
                    target_id="defense backlog",
                    result="succeeded",
                    run_id=finance.id,
                ),
                AuditEvent(
                    actor="code",
                    action="record_repository",
                    target_type="repository",
                    target_id="richard/lifeagent",
                    result="succeeded",
                    run_id=code.id,
                ),
                AuditEvent(
                    actor="planner",
                    action="record_course",
                    target_type="course",
                    target_id="CS101",
                    result="succeeded",
                    run_id=code.id,
                ),
            )
        )
    return finance_id, code_id


def _seed_source_records(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        for index in range(1, 9):
            FinanceRepository.upsert_approved_source(
                session,
                source_id=f"source{index}",
                name=f"Source {index}",
                base_url=f"https://source{index}.example/api",
                source_version="v1",
                allowlist_version="finance-sources-test",
                license_note="Links only.",
                entitlement=f"Entitlement {index}",
                enabled=False,
            )


def _seed_health(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        session.add(
            HealthCheck(
                check_name="finance",
                rule="fixture",
                state=HealthState.HEALTHY,
                last_success_at=NOW,
                next_due_at=NOW + timedelta(days=1),
                checked_at=NOW,
                diagnostic="finance healthy",
            )
        )


def test_every_operations_route_requires_auth(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _, _):
        run_id = uuid4()
        responses = (
            client.get("/api/operations"),
            client.get("/api/operations/activity"),
            client.get(f"/api/operations/activity/{run_id}"),
            client.get("/api/operations/settings/sources"),
            client.post(f"/api/operations/activity/{run_id}/acknowledgements", json={}),
        )

    for response in responses:
        assert response.status_code == 401


def test_health_and_source_settings_success(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, engine, _):
        _seed_health(engine)
        _seed_source_records(engine)
        health = client.get("/api/operations", auth=AUTH)
        sources = client.get("/api/operations/settings/sources", auth=AUTH)

    assert health.status_code == 200
    cards = health.json()
    assert len(cards) == 4
    assert next(card for card in cards if card["component"] == "finance")["state"] == "healthy"
    assert sources.status_code == 200
    body = sources.json()
    assert body["allowlist_version"] == "finance-sources-test"
    assert body["schedule_enabled"] is False
    assert body["approval_complete"] is False
    assert len(body["sources"]) == 8
    assert body["sources"][0]["hostname"] == "source1.example"


def test_activity_filters_pass_through_to_repository(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, engine, _):
        finance_id, _ = _seed_activity(engine)
        response = client.get(
            "/api/operations/activity",
            params={
                "agent": "finance",
                "date_from": (NOW - timedelta(hours=1)).isoformat(),
                "date_to": (NOW + timedelta(hours=1)).isoformat(),
                "attention_only": "true",
                "ticker_theme": "defense",
                "page": "1",
                "page_size": "10",
            },
            auth=AUTH,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["run_id"] == str(finance_id)
    assert body["items"][0]["severity"] == "attention"
    assert body["items"][0]["delivery_links"][0]["url"] == "https://discord.example/messages/1"
    assert body["items"][0]["evidence_links"][0]["url"] == "https://source.example/filing"


def test_activity_search_filters_include_repository_and_course(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, engine, _):
        _, code_id = _seed_activity(engine)
        repository_response = client.get(
            "/api/operations/activity",
            params={"repository": "lifeagent"},
            auth=AUTH,
        )
        course_response = client.get(
            "/api/operations/activity",
            params={"course": "CS101"},
            auth=AUTH,
        )

    assert repository_response.status_code == 200
    assert course_response.status_code == 200
    assert repository_response.json()["items"][0]["run_id"] == str(code_id)
    assert course_response.json()["items"][0]["run_id"] == str(code_id)
    assert "<script>" in repository_response.json()["items"][0]["summary"]


def test_activity_detail_404_for_missing_run(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _, _):
        response = client.get(f"/api/operations/activity/{uuid4()}", auth=AUTH)

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


def test_acknowledgement_success_is_idempotent_and_only_writes_ui_metadata(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as (client, engine, queue_trap):
        run_id, _ = _seed_activity(engine)
        first = client.post(
            f"/api/operations/activity/{run_id}/acknowledgements",
            json={"run_id": str(run_id), "alert_key": "source_failed"},
            auth=AUTH,
        )
        second = client.post(
            f"/api/operations/activity/{run_id}/acknowledgements",
            json={"alert_key": "source_failed"},
            auth=AUTH,
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["acknowledgement_id"] == second.json()["acknowledgement_id"]
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(UIAcknowledgement)) == 1
            assert session.scalar(select(func.count()).select_from(Delivery)) == 1
        assert queue_trap.called is False


def test_acknowledgement_rejects_missing_or_mismatched_run(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _, _):
        path_run_id = uuid4()
        missing = client.post(
            f"/api/operations/activity/{path_run_id}/acknowledgements",
            json={"alert_key": "run"},
            auth=AUTH,
        )
        mismatched = client.post(
            f"/api/operations/activity/{path_run_id}/acknowledgements",
            json={"run_id": str(uuid4()), "alert_key": "run"},
            auth=AUTH,
        )

    assert missing.status_code == 404
    assert mismatched.status_code == 422


def test_unavailable_database_returns_503_without_raw_exception() -> None:
    with TestClient(_app(None)) as client:
        response = client.get("/api/operations", auth=AUTH)

    assert response.status_code == 503
    assert response.json() == {"detail": "operations database is unavailable"}


def test_invalid_date_range_or_naive_datetime_returns_422(tmp_path: Path) -> None:
    with _client(tmp_path) as (client, _, _):
        reversed_range = client.get(
            "/api/operations/activity",
            params={
                "date_from": NOW.isoformat(),
                "date_to": (NOW - timedelta(days=1)).isoformat(),
            },
            auth=AUTH,
        )
        naive = client.get(
            "/api/operations/activity",
            params={"date_from": NOW.replace(tzinfo=None).isoformat()},
            auth=AUTH,
        )

    assert reversed_range.status_code == 422
    assert reversed_range.json() == {"detail": "date_from must be before or equal to date_to"}
    assert naive.status_code == 422
    assert naive.json() == {"detail": "date filters must include a timezone"}
