"""Phase 8 reliability acceptance coverage.

The default tests exercise the durable contracts behind the acceptance
criteria with disposable databases and stubbed providers.  The host-destructive
checks are opt-in through LIFEAGENT_RUN_DOCKER_ACCEPTANCE=1 because they invoke
Docker Compose and backup/restore tooling.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from app.agents.finance.contracts import (
    BriefingPayload,
    EventCard,
    ExposureMapping,
    ImpactLabel,
    QuantValue,
)
from app.connectors.discord import (
    DiscordFailureAlertAdapter,
    DiscordFinanceBriefingAdapter,
    FailureAlert,
    deliver_finance_briefing,
)
from app.core.config import Settings
from app.core.errors import ErrorCode, LifeAgentError, transient_error
from app.db.models import (
    AgentRun,
    AuditEvent,
    Delivery,
    DeliveryStatus,
    HealthCheck,
    RunStatus,
    RunStep,
    StepStatus,
)
from app.db.repositories import AuditRepository, DeliveryRepository, RunRepository
from app.health.checks import HealthCheck as ProbeHealthCheck
from app.health.checks import HealthState
from app.queue import tasks
from app.queue.execution import execute_recorded_attempt
from app.queue.periodic import PeriodicOccurrence, stable_period_key
from app.queue.retry import RetryPolicy
from app.queue.visibility import QueueJobMetadata, QueueVisibility, queue_visibility

RUN_DOCKER_ACCEPTANCE = os.environ.get("LIFEAGENT_RUN_DOCKER_ACCEPTANCE") == "1"
CHANNEL_ID = "987654321012345678"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _upgrade(database_url: str) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


@pytest.fixture(scope="module")
def postgres_engine() -> Generator[Engine, None, None]:
    with PostgresContainer(
        "pgvector/pgvector:0.8.6-pg16-bookworm",
        driver="psycopg",
    ) as postgres:
        database_url = postgres.get_connection_url()
        _upgrade(database_url)
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            yield engine
        finally:
            engine.dispose()


@pytest.fixture
def db_session(postgres_engine: Engine) -> Generator[Session, None, None]:
    with Session(postgres_engine) as session:
        yield session
        session.rollback()


def _require_docker_acceptance() -> None:
    if not RUN_DOCKER_ACCEPTANCE:
        pytest.skip("set LIFEAGENT_RUN_DOCKER_ACCEPTANCE=1 to run Docker acceptance checks")


def _require_commands(names: Iterable[str]) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        pytest.fail(
            "Phase 8 Docker acceptance requires host command(s): " + ", ".join(sorted(missing))
        )


def _docker_compose_command() -> tuple[str, ...]:
    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("Phase 8 Docker acceptance requires docker")
    docker_compose = subprocess.run(  # noqa: S603
        (docker, "compose", "version"),
        check=False,
        text=True,
        capture_output=True,
    )
    if docker_compose.returncode == 0:
        return (docker, "compose")
    legacy_compose_path = shutil.which("docker-compose")
    if legacy_compose_path is not None:
        legacy_compose = subprocess.run(  # noqa: S603
            (legacy_compose_path, "version"),
            check=False,
            text=True,
            capture_output=True,
        )
        if legacy_compose.returncode == 0:
            return (legacy_compose_path,)
    pytest.fail("Phase 8 Docker acceptance requires Docker Compose")


def _run(
    command_line: Sequence[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603
        command_line,
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = "\n".join(
            (
                f"command failed: {' '.join(command_line)}",
                f"exit code: {result.returncode}",
                result.stdout,
                result.stderr,
            )
        )
        pytest.fail(detail)
    return result


def _plain_postgres_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    scheme = parsed.scheme.removesuffix("+psycopg")
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _database_url_for(database_url: str, database: str) -> str:
    url = make_url(database_url).set(database=database)
    return str(url.render_as_string(hide_password=False))


def _public_age_recipient(identity_file: Path) -> str:
    for line in identity_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("# public key: "):
            return line.removeprefix("# public key: ").strip()
    pytest.fail(f"age identity did not contain a public recipient: {identity_file}")


def _seed_backup_records(engine: Engine) -> dict[str, str]:
    artifact_key = "a" * 64
    receipt_artifact_key = "b" * 64
    with Session(engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=f"phase8-backup:{uuid.uuid4()}:v1",
            agent_name="phase8_backup",
            trigger="integration",
            artifact_key=artifact_key,
        )
        RunRepository.set_status(
            session,
            run.id,
            status=RunStatus.SUCCEEDED,
            artifact_key=artifact_key,
            summary="backup restore acceptance seed",
        )
        step = RunRepository.create_step_attempt(
            session,
            run_id=run.id,
            node_name="phase8.seed",
            attempt=1,
            artifact_key=artifact_key,
        )
        RunRepository.update_step(
            session,
            step.id,
            status=StepStatus.SUCCEEDED,
            artifact_key=artifact_key,
        )
        delivery = DeliveryRepository.create_or_get_intent(
            session,
            channel="discord",
            target=CHANNEL_ID,
            idempotency_key=f"phase8-delivery:{uuid.uuid4()}:v1",
            run_id=run.id,
        )
        DeliveryRepository.record_attempt(
            session,
            delivery.id,
            DeliveryStatus.SENT,
            external_url="https://discord.com/channels/@me/987654321012345678/123",
            receipt_artifact_key=receipt_artifact_key,
        )
        audit = AuditRepository.append(
            session,
            actor="phase8-test",
            action="backup.seed",
            target_type="phase8",
            target_id=str(run.id),
            result="ok",
            run_id=run.id,
            artifact_key=artifact_key,
        )
        return {
            "run_key": run.idempotency_key,
            "audit_id": str(audit.id),
            "artifact_key": artifact_key,
            "receipt_artifact_key": receipt_artifact_key,
        }


def test_backup_restore_round_trip_preserves_durable_records(tmp_path: Path) -> None:
    _require_docker_acceptance()
    _require_commands(("age", "age-keygen", "pg_dump", "pg_restore"))

    with PostgresContainer(
        "pgvector/pgvector:0.8.6-pg16-bookworm",
        driver="psycopg",
    ) as postgres:
        source_url = postgres.get_connection_url()
        _upgrade(source_url)
        source_engine = create_engine(source_url, pool_pre_ping=True)
        try:
            seeded = _seed_backup_records(source_engine)
            with source_engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
                conn.execute(text("CREATE DATABASE lifeagent_restore"))
        finally:
            source_engine.dispose()

        identity_file = tmp_path / "age-identity.txt"
        backup_dir = tmp_path / "backups"
        _run(("age-keygen", "-o", str(identity_file)))
        recipient = _public_age_recipient(identity_file)

        _run(
            (
                "scripts/backup_database.sh",
                "--env-file",
                str(tmp_path / "missing.env"),
                "--database-url",
                _plain_postgres_url(source_url),
                "--output-dir",
                str(backup_dir),
                "--recipient",
                recipient,
                "--retention-days",
                "30",
            ),
        )
        backup_file = next(backup_dir.glob("lifeagent-*.dump.age"))
        restore_url = _database_url_for(source_url, "lifeagent_restore")
        _run(
            (
                "scripts/restore_database.sh",
                "--env-file",
                str(tmp_path / "missing.env"),
                "--backup-file",
                str(backup_file),
                "--target-database-url",
                _plain_postgres_url(restore_url),
                "--confirm-target-db",
                "lifeagent_restore",
                "--identity-file",
                str(identity_file),
            )
        )

        restored_engine = create_engine(restore_url, pool_pre_ping=True)
        try:
            with Session(restored_engine) as session:
                run = session.scalar(
                    select(AgentRun).where(AgentRun.idempotency_key == seeded["run_key"])
                )
                audit = session.get(AuditEvent, uuid.UUID(seeded["audit_id"]))
                delivery = session.scalar(
                    select(Delivery).where(
                        Delivery.receipt_artifact_key == seeded["receipt_artifact_key"]
                    )
                )
                step = session.scalar(
                    select(RunStep).where(RunStep.artifact_key == seeded["artifact_key"])
                )
            assert run is not None
            assert run.artifact_key == seeded["artifact_key"]
            assert audit is not None
            assert audit.artifact_key == seeded["artifact_key"]
            assert delivery is not None
            assert delivery.status == DeliveryStatus.SENT
            assert step is not None
        finally:
            restored_engine.dispose()


@dataclass
class _Recorder:
    responder: Callable[[httpx.Request], httpx.Response]
    requests: list[httpx.Request]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def _event_card() -> EventCard:
    return EventCard(
        event_id="event-1",
        title="ACME update",
        verified_facts=("ACME published a redacted finance update.",),
        uncertainty="Timing remains uncertain.",
        counter_case="The event may already be priced in.",
        impact_label=ImpactLabel.MONITOR,
        exposure=ExposureMapping(event_id="event-1", holding_symbols=("ACME",)),
        citations=("fixture",),
        numbers=(
            QuantValue(
                label="Exposure weight",
                value=2.5,
                unit="percent",
                as_of=date(2026, 9, 4),
                source_ids=("fixture",),
            ),
        ),
    )


def _briefing(run_id: uuid.UUID) -> BriefingPayload:
    return BriefingPayload(
        run_id=run_id,
        source_allowlist_version="finance-sources-2026.09",
        generated_at=NOW,
        status="succeeded",
        cards=(_event_card(),),
        tickers=("ACME",),
        themes=("phase8",),
    )


async def test_sigterm_contract_and_delivery_intent_retry_are_idempotent(
    postgres_engine: Engine,
) -> None:
    assert _idle_worker_entrypoint_uses_exec()

    key = "finance:2026-09-04:market-open:v1"
    with Session(postgres_engine) as session, session.begin():
        run = RunRepository.create_or_get(
            session,
            idempotency_key=f"phase8-kill:{uuid.uuid4()}:v1",
            agent_name="finance",
            trigger="integration",
        )
        run_id = run.id

    first_call = True

    async def flaky_delivery() -> dict[str, object]:
        nonlocal first_call
        if first_call:
            first_call = False
            with Session(postgres_engine) as session, session.begin():
                DeliveryRepository.create_or_get_intent(
                    session,
                    channel="discord",
                    target=CHANNEL_ID,
                    idempotency_key=key,
                    run_id=run_id,
                )
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "simulated worker termination")

        recorder = _Recorder(
            responder=lambda _: httpx.Response(200, json={"id": "123456789012345678"}),
            requests=[],
        )
        async with httpx.AsyncClient(
            base_url="https://discord.com/api/v10",
            transport=httpx.MockTransport(recorder),
        ) as client:
            adapter = DiscordFinanceBriefingAdapter(
                token=SecretStr("never-print-this-token"),
                allowed_channel_ids={CHANNEL_ID},
                client=client,
            )
            delivery = await deliver_finance_briefing(
                engine=postgres_engine,
                run_id=run_id,
                channel_id=CHANNEL_ID,
                payload=_briefing(run_id),
                idempotency_key=key,
                adapter=adapter,
            )
        assert len(recorder.requests) == 1
        body = json.loads(recorder.requests[0].content)
        assert body["nonce"] == delivery.id.hex[:25]
        assert body["enforce_nonce"] is True
        return {"status": "succeeded", "delivered": True, "delivery_count": 1}

    policy = RetryPolicy(max_attempts=3, base_delay_seconds=1, max_delay_seconds=1, jitter_ratio=0)
    with pytest.raises(LifeAgentError):
        await execute_recorded_attempt(
            flaky_delivery,
            engine=postgres_engine,
            run_id=run_id,
            node_name="phase8.delivery",
            attempt=1,
            retry_policy=policy,
        )
    await execute_recorded_attempt(
        flaky_delivery,
        engine=postgres_engine,
        run_id=run_id,
        node_name="phase8.delivery",
        attempt=2,
        retry_policy=policy,
    )

    with Session(postgres_engine) as session:
        deliveries = list(session.scalars(select(Delivery).where(Delivery.idempotency_key == key)))
        steps = list(
            session.scalars(
                select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.attempt)
            )
        )
    assert len(deliveries) == 1
    assert deliveries[0].status == DeliveryStatus.SENT
    assert deliveries[0].attempt_count == 1
    assert [(step.attempt, step.status) for step in steps] == [
        (1, "attention"),
        (2, "succeeded"),
    ]


@pytest.mark.asyncio
async def test_academic_morning_schedule_persists_once_across_duplicate_and_replay(
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occurrence = PeriodicOccurrence(
        local_time=datetime(2026, 9, 10, 8, 0, tzinfo=ZoneInfo("America/Toronto")),
        scheduled_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    period_key = stable_period_key("academic-morning", occurrence)
    assert period_key == "academic-morning:2026-09-10:0800:v1"
    delivery_key = period_key.replace("academic-morning:", "academic-morning-delivery:", 1)
    queued_arguments: dict[str, str] = {}
    defer_calls = 0
    real_task = tasks.academic_morning_notification_task

    class FakeTask:
        def configure(self, **kwargs: str) -> FakeTask:
            self.configuration = kwargs
            return self

        async def defer_async(self, **kwargs: str) -> int:
            nonlocal defer_calls
            defer_calls += 1
            queued_arguments.update(kwargs)
            if defer_calls > 1:
                from procrastinate.exceptions import AlreadyEnqueued

                raise AlreadyEnqueued("duplicate morning period")
            return 801

    def status_text(value: object) -> str:
        return getattr(value, "value", str(value))

    async def scheduled_handler(
        occurrence_at_iso: str,
        run_id: str,
        queued_period_key: str,
        attempt: int,
        attempt_limit: int,
    ) -> dict[str, object]:
        assert occurrence_at_iso == "2026-09-10T12:00:00+00:00"
        assert queued_period_key == period_key
        assert attempt <= attempt_limit
        with Session(postgres_engine) as session, session.begin():
            delivery = DeliveryRepository.create_or_get_intent(
                session,
                channel="discord",
                target=CHANNEL_ID,
                idempotency_key=delivery_key,
                run_id=uuid.UUID(run_id),
            )
            if status_text(delivery.status) not in {"sent", "acknowledged"}:
                DeliveryRepository.record_attempt(
                    session,
                    delivery.id,
                    DeliveryStatus.SENT,
                    external_url=(
                        "https://discord.com/channels/@me/987654321012345678/111111111111111111"
                    ),
                )
        return {"status": "succeeded", "delivery_count": 1}

    monkeypatch.setattr(tasks, "_database", SimpleNamespace(engine=postgres_engine))
    monkeypatch.setattr(tasks, "academic_morning_notification_task", FakeTask())
    monkeypatch.setattr(tasks, "_academic_morning_notification_handler", scheduled_handler)

    first = await tasks.defer_academic_morning_notification(occurrence)
    duplicate = await tasks.defer_academic_morning_notification(occurrence)

    assert first["status"] == "enqueued"
    assert first["period_key"] == period_key
    assert duplicate == {
        "status": "already_enqueued",
        "run_id": first["run_id"],
        "period_key": period_key,
    }
    assert queued_arguments == {
        "occurrence_at_iso": "2026-09-10T12:00:00+00:00",
        "run_id": first["run_id"],
        "period_key": period_key,
    }

    context = SimpleNamespace(job=SimpleNamespace(attempts=0))
    result = await real_task.func(context, **queued_arguments)
    replay_context = SimpleNamespace(job=SimpleNamespace(attempts=1))
    replay = await real_task.func(replay_context, **queued_arguments)

    assert result["status"] == "succeeded"
    assert replay["status"] == "succeeded"
    with Session(postgres_engine) as session:
        run = session.scalar(select(AgentRun).where(AgentRun.idempotency_key == period_key))
        deliveries = list(
            session.scalars(select(Delivery).where(Delivery.idempotency_key == delivery_key))
        )
        health = session.scalar(
            select(HealthCheck).where(HealthCheck.check_name == "academic_morning")
        )
    assert run is not None
    assert run.schedule == "academic-morning"
    assert run.input_version == "2026-09-10T12:00:00+00:00"
    assert run.status == RunStatus.SUCCEEDED
    assert len(deliveries) == 1
    assert deliveries[0].run_id == run.id
    assert deliveries[0].status == DeliveryStatus.SENT
    assert deliveries[0].attempt_count == 1
    assert health is not None
    assert health.state == "healthy"
    assert health.rule == "processing_and_delivery_succeeded"


def _probe(name: str, state: HealthState, diagnostic: str | None = None) -> ProbeHealthCheck:
    return ProbeHealthCheck(name=name, state=state, diagnostic=diagnostic or f"{name} {state}")


def _idle_worker_entrypoint_uses_exec() -> bool:
    entrypoint = Path("infra/idle-worker.sh").read_text(encoding="utf-8")
    executable_lines = [
        line.strip()
        for line in entrypoint.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return any(line == "exec python -m procrastinate \\" for line in executable_lines)


async def _run_shared_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database_checks: tuple[ProbeHealthCheck, ...],
    queue_check: ProbeHealthCheck,
    connector_configuration: ProbeHealthCheck,
    connector_liveness: tuple[ProbeHealthCheck, ...],
    ollama_check: ProbeHealthCheck,
) -> HealthCheck:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / uuid.uuid4().hex}.db")
    AgentRun.__table__.create(engine)
    Delivery.__table__.create(engine)
    HealthCheck.__table__.create(engine)
    monkeypatch.setattr(tasks, "_database", SimpleNamespace(engine=engine))
    monkeypatch.setattr(tasks, "_settings", Settings(_env_file=None))
    monkeypatch.setattr(tasks, "check_database", lambda _database: database_checks)
    monkeypatch.setattr(
        tasks,
        "_check_queue_with_settings",
        lambda _database, _settings, _now: queue_check,
    )
    monkeypatch.setattr(
        tasks,
        "check_artifact_root",
        lambda _settings: _probe("artifacts", HealthState.HEALTHY),
    )
    monkeypatch.setattr(
        tasks,
        "check_connector_configuration",
        lambda _settings: connector_configuration,
    )

    async def check_connector_liveness(
        _settings: Settings,
        _client: httpx.AsyncClient,
        *,
        now: datetime,
    ) -> tuple[ProbeHealthCheck, ...]:
        assert now == NOW
        return connector_liveness

    async def check_ollama(_settings: Settings, _client: httpx.AsyncClient) -> ProbeHealthCheck:
        return ollama_check

    monkeypatch.setattr(tasks, "check_connector_liveness", check_connector_liveness)
    monkeypatch.setattr(tasks, "check_ollama", check_ollama)
    try:
        await tasks.shared_services_periodic.func(timestamp=int(NOW.timestamp()))
        with Session(engine) as session:
            persisted = session.scalar(
                select(HealthCheck).where(HealthCheck.check_name == "shared_services")
            )
            assert persisted is not None
            session.expunge(persisted)
            return persisted
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("scenario", "checks", "expected_state", "expected_rule", "expected_fragment"),
    [
        (
            "ollama_unreachable",
            {
                "ollama_check": _probe("ollama", HealthState.ATTENTION, "Ollama unavailable"),
            },
            "attention",
            "processing_completed_with_attention",
            "ollama=attention",
        ),
        (
            "postgres_unreachable",
            {
                "database_checks": (_probe("database", HealthState.FAILED, "connection failed"),),
            },
            "failed",
            "required_processing_failed",
            "database=failed",
        ),
        (
            "stalled_worker",
            {
                "queue_check": _probe("queue", HealthState.ATTENTION, "stalled worker"),
            },
            "attention",
            "processing_completed_with_attention",
            "queue=attention",
        ),
        (
            "discord_unauthenticated",
            {
                "connector_liveness": (
                    _probe("discord_authentication", HealthState.FAILED, "invalid"),
                ),
            },
            "failed",
            "connector_unauthenticated",
            "discord_authentication=failed",
        ),
        (
            "github_unauthenticated",
            {
                "connector_liveness": (
                    _probe("github_installation_token", HealthState.FAILED, "invalid"),
                ),
            },
            "failed",
            "connector_unauthenticated",
            "github_installation_token=failed",
        ),
        (
            "notion_unauthenticated",
            {
                "connector_liveness": (
                    _probe("notion_authentication", HealthState.FAILED, "invalid"),
                ),
            },
            "failed",
            "connector_unauthenticated",
            "notion_authentication=failed",
        ),
    ],
)
async def test_shared_services_health_matches_documented_failure_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    checks: dict[str, object],
    expected_state: str,
    expected_rule: str,
    expected_fragment: str,
) -> None:
    persisted = await _run_shared_health(
        tmp_path,
        monkeypatch,
        database_checks=checks.get(
            "database_checks",
            (_probe("database", HealthState.HEALTHY),),
        ),
        queue_check=checks.get("queue_check", _probe("queue", HealthState.HEALTHY)),
        connector_configuration=checks.get(
            "connector_configuration",
            _probe("connector_configuration", HealthState.HEALTHY),
        ),
        connector_liveness=checks.get("connector_liveness", ()),
        ollama_check=checks.get("ollama_check", _probe("ollama", HealthState.HEALTHY)),
    )

    assert scenario
    assert persisted.check_name == "shared_services"
    assert persisted.state == expected_state
    assert persisted.rule == expected_rule
    assert persisted.diagnostic is not None
    assert expected_fragment in persisted.diagnostic


def test_stalled_worker_projection_uses_queue_stalled_after_seconds() -> None:
    record = QueueJobMetadata(
        job_id=1,
        status="doing",
        heartbeat_at=NOW - timedelta(seconds=121),
        queue_name="academic_planner",
        task_name="lifeagent.discord_academic",
    )
    assert (
        queue_visibility(record, now=NOW, stalled_after=timedelta(seconds=120))
        is QueueVisibility.STALLED
    )


async def test_discord_failure_alert_policy_allows_only_non_healthy_states() -> None:
    with pytest.raises(ValidationError, match="normal health"):
        FailureAlert(
            delivery_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            channel_id=CHANNEL_ID,
            component="shared_services",
            state=HealthState.HEALTHY,
            error_code=ErrorCode.INTERNAL,
            attempt=0,
            attempt_limit=0,
        )

    recorder = _Recorder(
        responder=lambda _: httpx.Response(200, json={"id": "123456789012345678"}),
        requests=[],
    )
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(recorder),
    ) as client:
        adapter = DiscordFailureAlertAdapter(
            token=SecretStr("never-print-this-token"),
            allowed_channel_ids={CHANNEL_ID},
            client=client,
        )
        alert = FailureAlert(
            delivery_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            channel_id=CHANNEL_ID,
            component="shared_services",
            state=HealthState.ATTENTION,
            error_code=ErrorCode.CONNECTOR_TRANSIENT,
            attempt=1,
            attempt_limit=3,
        )
        receipt = await adapter.send(alert)

    assert receipt.external_id == "123456789012345678"
    assert len(recorder.requests) == 1
    body = json.loads(recorder.requests[0].content)
    assert body["nonce"] == alert.delivery_id.hex[:25]
    assert body["enforce_nonce"] is True
    assert body["allowed_mentions"] == {"parse": []}
    assert "never-print-this-token" not in recorder.requests[0].content.decode()


def _write_compose_override(
    path: Path,
    project: str,
    *,
    app_environment: dict[str, str] | None = None,
) -> None:
    lines: list[str] = []
    if app_environment is not None:
        lines.extend(
            (
                "services:",
                "  api:",
                "    environment:",
            )
        )
        lines.extend(
            f"      {key}: {json.dumps(value)}" for key, value in sorted(app_environment.items())
        )
        lines.extend(
            (
                "  worker-academic-planner:",
                "    environment:",
            )
        )
        lines.extend(
            f"      {key}: {json.dumps(value)}" for key, value in sorted(app_environment.items())
        )
        lines.append("")

    lines.extend(
        (
            "volumes:",
            "  postgres_data:",
            f"    name: {project}_postgres_data",
            "  artifacts:",
            f"    name: {project}_artifacts",
            "",
        )
    )
    path.write_text("\n".join(lines), encoding="utf-8")


class _SlowOllamaHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/api/tags":
            self.send_response(404)
            self.end_headers()
            return
        time.sleep(20)
        payload = b'{"models":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _slow_ollama_server() -> Generator[str, None, None]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowOllamaHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class _DiscordStubState:
    def __init__(self) -> None:
        self.base_url = ""
        self.first_post_seen = threading.Event()
        self.release_first_post = threading.Event()
        self._lock = threading.Lock()
        self._posts: list[dict[str, object]] = []
        self._message_ids_by_nonce: dict[str, str] = {}

    def accept_message(self, body: dict[str, object]) -> tuple[str, bool]:
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("Discord message did not include a nonce")

        with self._lock:
            self._posts.append(body)
            is_first_post = len(self._posts) == 1
            message_id = self._message_ids_by_nonce.get(nonce)
            if message_id is None or body.get("enforce_nonce") is not True:
                message_id = str(123_456_789_012_345_678 + len(self._message_ids_by_nonce))
                self._message_ids_by_nonce[nonce] = message_id
            if is_first_post:
                self.first_post_seen.set()
            return message_id, is_first_post

    def posts(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(dict(post) for post in self._posts)

    def logical_message_count(self) -> int:
        with self._lock:
            return len(self._message_ids_by_nonce)


class _DiscordStubServer(ThreadingHTTPServer):
    state: _DiscordStubState

    def __init__(
        self,
        server_address: tuple[str, int],
        state: _DiscordStubState,
    ) -> None:
        self.state = state
        super().__init__(server_address, _DiscordStubHandler)


class _DiscordStubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/api/v10/users/@me":
            self._send_json(401, {"message": "401: Unauthorized", "code": 0})
            return
        self._send_json(404, {"message": "not found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/api/v10/channels/") or not self.path.endswith("/messages"):
            self._send_json(404, {"message": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        try:
            body = json.loads(raw_body)
        except ValueError:
            self._send_json(400, {"message": "invalid json"})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"message": "invalid message body"})
            return

        try:
            message_id, is_first_post = cast(_DiscordStubServer, self.server).state.accept_message(
                body
            )
        except ValueError as exc:
            self._send_json(400, {"message": str(exc)})
            return
        if is_first_post:
            cast(_DiscordStubServer, self.server).state.release_first_post.wait(timeout=60)
        self._send_json(
            200,
            {
                "id": message_id,
                "channel_id": CHANNEL_ID,
                "guild_id": "111111111111111111",
            },
        )

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            return

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextmanager
def _discord_stub_server() -> Generator[_DiscordStubState, None, None]:
    state = _DiscordStubState()
    server = _DiscordStubServer(("0.0.0.0", 0), state)  # noqa: S104
    _, port = server.server_address
    state.base_url = f"http://host.docker.internal:{port}/api/v10"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        state.release_first_post.set()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _write_enqueue_shared_health_script(path: Path) -> None:
    path.write_text(
        """
from __future__ import annotations

import asyncio
import sys

from app.queue.app import procrastinate_app
from app.queue.tasks import shared_services_periodic


async def main() -> None:
    lock = sys.argv[1]
    timestamp = int(sys.argv[2])
    async with procrastinate_app.open_async():
        await shared_services_periodic.configure(queueing_lock=lock).defer_async(
            timestamp=timestamp
        )


asyncio.run(main())
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _copy_compose_script(
    compose: Sequence[str],
    env: dict[str, str],
    *,
    path: Path,
    container_path: str,
    service: str = "worker-academic-planner",
) -> None:
    _run((*compose, "cp", str(path), f"{service}:{container_path}"), env=env)


def _postgres_scalar(compose: Sequence[str], env: dict[str, str], sql: str) -> str:
    return _run(
        (
            *compose,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "lifeagent",
            "-d",
            "lifeagent",
            "-tAc",
            sql,
        ),
        env=env,
    ).stdout.strip()


async def _wait_for_postgres_scalar(
    compose: Sequence[str],
    env: dict[str, str],
    sql: str,
    *,
    expected: str = "1",
    attempts: int = 30,
) -> None:
    for _ in range(attempts):
        if _postgres_scalar(compose, env, sql) == expected:
            return
        await asyncio.sleep(1)
    pytest.fail(f"timed out waiting for SQL result {expected}: {sql}")


async def _wait_for_event(event: threading.Event, description: str) -> None:
    for _ in range(300):
        if event.is_set():
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"timed out waiting for {description}")


async def _wait_for_worker_exec(compose: Sequence[str], env: dict[str, str]) -> str:
    for _ in range(30):
        container_id = _run(
            (*compose, "ps", "-q", "worker-academic-planner"), env=env
        ).stdout.strip()
        if not container_id:
            await asyncio.sleep(1)
            continue
        current = json.loads(_run(("docker", "inspect", container_id), env=env).stdout)[0]
        if current["State"]["Running"]:
            return container_id
        await asyncio.sleep(1)
    pytest.fail("worker-academic-planner was not running within 30 seconds")


def test_compose_worker_uses_exec_and_restarts_after_sigterm(tmp_path: Path) -> None:
    _require_docker_acceptance()
    _require_commands(("docker",))

    project = f"lifeagent_phase8_{uuid.uuid4().hex[:10]}"
    override = tmp_path / "compose.override.yaml"
    _write_compose_override(override, project)
    compose = (
        *_docker_compose_command(),
        "-p",
        project,
        "-f",
        "compose.yaml",
        "-f",
        str(override),
    )
    env = os.environ.copy()
    env.update(
        {
            "API_PORT": str(20_000 + uuid.uuid4().int % 20_000),
            "BUILDX_CONFIG": str(tmp_path / "buildx"),
            "OLLAMA_BASE_URL": "http://127.0.0.1:9",
            "OLLAMA_MODEL_DIGEST": "",
            "WORKER_CONCURRENCY": "1",
        }
    )
    slow_ollama = None
    try:
        _run(
            (
                *compose,
                "up",
                "-d",
                "--build",
                "postgres",
                "api",
                "worker-academic-planner",
            ),
            env=env,
        )
        asyncio.run(_wait_for_worker_exec(compose, env))
        process_cmdline = _run(
            (
                *compose,
                "exec",
                "-T",
                "worker-academic-planner",
                "sh",
                "-lc",
                "tr '\\0' ' ' < /proc/1/cmdline",
            ),
            env=env,
        ).stdout
        assert "python -m procrastinate" in process_cmdline
        assert "worker" in process_cmdline

        slow_ollama = _slow_ollama_server()
        ollama_url = slow_ollama.__enter__().replace("127.0.0.1", "host.docker.internal")
        env["OLLAMA_BASE_URL"] = ollama_url
        _run(
            (
                *compose,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "worker-academic-planner",
            ),
            env=env,
        )
        asyncio.run(_wait_for_worker_exec(compose, env))

        enqueue_script = tmp_path / "enqueue_shared_health.py"
        _write_enqueue_shared_health_script(enqueue_script)
        _copy_compose_script(
            compose,
            env,
            path=enqueue_script,
            container_path="/tmp/phase8_enqueue_shared_health.py",
            service="api",
        )
        _run(
            (
                *compose,
                "exec",
                "-T",
                "api",
                "env",
                "PYTHONPATH=/app",
                "python",
                "/tmp/phase8_enqueue_shared_health.py",
                f"phase8-shared-services-{uuid.uuid4()}",
                str(int(NOW.timestamp())),
            ),
            env=env,
        )
        doing_job_sql = (
            "SELECT count(*) FROM procrastinate_jobs "
            "WHERE task_name = 'lifeagent.health.shared_services' AND status = 'doing'"
        )
        asyncio.run(_wait_for_postgres_scalar(compose, env, doing_job_sql))
        _run((*compose, "kill", "-s", "SIGTERM", "worker-academic-planner"), env=env)
        _run((*compose, "up", "-d", "worker-academic-planner"), env=env)
        asyncio.run(_wait_for_worker_exec(compose, env))
        health_sql = (
            "SELECT state || ':' || rule FROM health_checks WHERE check_name = 'shared_services'"
        )
        asyncio.run(
            _wait_for_postgres_scalar(
                compose,
                env,
                health_sql,
                expected="attention:processing_completed_with_attention",
            )
        )
    finally:
        subprocess.run(  # noqa: S603
            (*compose, "down", "--volumes", "--remove-orphans"),
            check=False,
            env=env,
            text=True,
            capture_output=True,
        )
        if slow_ollama is not None:
            slow_ollama.__exit__(None, None, None)


def test_compose_delivery_intent_survives_sigkill_without_duplicate_discord_message(
    tmp_path: Path,
) -> None:
    _require_docker_acceptance()
    _require_commands(("docker",))

    project = f"lifeagent_phase8_{uuid.uuid4().hex[:10]}"
    override = tmp_path / "compose.override.yaml"
    queueing_lock = f"phase8-shared-services-discord-{uuid.uuid4()}"
    with _discord_stub_server() as discord:
        _write_compose_override(
            override,
            project,
            app_environment={
                "APP_ENVIRONMENT": "acceptance",
                "CONNECTOR_TIMEOUT_SECONDS": "2",
                "DISCORD_API_BASE_URL": discord.base_url,
                "DISCORD_BOT_TOKEN": "stub_discord_token",
                "DISCORD_CODE_REVIEW_CHANNEL_ID": CHANNEL_ID,
                "OLLAMA_BASE_URL": "http://127.0.0.1:9",
                "OLLAMA_MODEL_DIGEST": "",
                "RETRY_BASE_DELAY_SECONDS": "1",
                "RETRY_JITTER_RATIO": "0",
                "RETRY_MAX_DELAY_SECONDS": "1",
                "WORKER_CONCURRENCY": "1",
            },
        )
        compose = (
            *_docker_compose_command(),
            "-p",
            project,
            "-f",
            "compose.yaml",
            "-f",
            str(override),
        )
        env = os.environ.copy()
        env.update(
            {
                "API_PORT": str(20_000 + uuid.uuid4().int % 20_000),
                "BUILDX_CONFIG": str(tmp_path / "buildx"),
            }
        )
        try:
            _run(
                (
                    *compose,
                    "up",
                    "-d",
                    "--build",
                    "postgres",
                    "api",
                    "worker-academic-planner",
                ),
                env=env,
            )
            asyncio.run(_wait_for_worker_exec(compose, env))

            enqueue_script = tmp_path / "enqueue_shared_health.py"
            _write_enqueue_shared_health_script(enqueue_script)
            _copy_compose_script(
                compose,
                env,
                path=enqueue_script,
                container_path="/tmp/phase8_enqueue_shared_health.py",
                service="api",
            )
            _run(
                (
                    *compose,
                    "exec",
                    "-T",
                    "api",
                    "env",
                    "PYTHONPATH=/app",
                    "python",
                    "/tmp/phase8_enqueue_shared_health.py",
                    queueing_lock,
                    str(int(NOW.timestamp())),
                ),
                env=env,
            )
            asyncio.run(_wait_for_event(discord.first_post_seen, "first Discord message POST"))
            sending_sql = (
                "SELECT count(*) FROM deliveries "
                "WHERE channel = 'discord' "
                "AND target = '987654321012345678' "
                "AND status = 'sending'"
            )
            asyncio.run(_wait_for_postgres_scalar(compose, env, sending_sql))

            _run(
                (*compose, "kill", "-s", "SIGKILL", "worker-academic-planner"),
                env=env,
            )
            discord.release_first_post.set()
            _run((*compose, "up", "-d", "worker-academic-planner"), env=env)
            asyncio.run(_wait_for_worker_exec(compose, env))

            final_delivery_sql = (
                "SELECT concat(count(*), ':', coalesce(min(status::text), ''), ':', "
                "coalesce(min(attempt_count)::text, '')) "
                "FROM deliveries "
                "WHERE channel = 'discord' "
                "AND target = '987654321012345678'"
            )
            asyncio.run(
                _wait_for_postgres_scalar(
                    compose,
                    env,
                    final_delivery_sql,
                    expected="1:sent:1",
                    attempts=120,
                )
            )
        finally:
            discord.release_first_post.set()
            subprocess.run(  # noqa: S603
                (*compose, "down", "--volumes", "--remove-orphans"),
                check=False,
                env=env,
                text=True,
                capture_output=True,
            )

    posts = discord.posts()
    assert len(posts) >= 2
    nonces = {post.get("nonce") for post in posts}
    assert len(nonces) == 1
    assert all(post.get("enforce_nonce") is True for post in posts)
    assert all(post.get("allowed_mentions") == {"parse": []} for post in posts)
    assert discord.logical_message_count() == 1
