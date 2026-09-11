"""Unit coverage for Phase 8 connector-token/liveness health diagnostics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.connectors import discord as discord_connector
from app.connectors.github import InstallationToken
from app.core.config import DISCORD_API_BASE_URL, Settings
from app.core.errors import ErrorCode, authorization_error, transient_error
from app.db.models import AgentRun, Base, Delivery, RunStatus
from app.db.models import DeliveryStatus as DatabaseDeliveryStatus
from app.db.models import HealthCheck as PersistedHealthCheck
from app.health.checks import (
    HealthState,
    check_connector_liveness,
    check_discord_authentication,
    check_github_installation_token,
    check_notion_authentication,
    check_queue,
)
from app.health.service import evaluate_academic_morning_health
from app.queue import visibility
from app.queue.periodic import TorontoPeriodicSchedule, stable_period_key
from app.queue.visibility import QueueJobMetadata

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
TORONTO = ZoneInfo("America/Toronto")
ACADEMIC_CHANNEL_ID = "987654321012345678"

_AGENT_CHECK_NAMES = {
    "finance",
    "finance_briefing",
    "code_review",
    "code-review",
    "academic_planner",
    "academic-planner",
}


def _github_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "github_app_id": 1,
        "github_installation_id": 2,
        "github_private_key": "test-private-key",
        "github_webhook_secret": "test-webhook-secret",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _notion_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "notion_token": "notion-secret",
        "notion_courses_database_id": "courses",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def health_engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'health.db'}")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _academic_morning_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "app_timezone": "America/Toronto",
        "academic_morning_schedule": time(8),
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _academic_morning_key(settings: Settings, local_day: datetime) -> str:
    schedule = TorontoPeriodicSchedule.from_time(
        settings.academic_morning_schedule,
        timezone_name=settings.app_timezone,
    )
    occurrence = schedule.next_occurrence(local_day.astimezone(UTC) - timedelta(microseconds=1))
    return stable_period_key("academic-morning", occurrence)


def _seed_academic_morning_run(
    session: Session,
    *,
    settings: Settings,
    status: RunStatus,
    local_day: datetime = datetime(2026, 9, 10, tzinfo=TORONTO),
    error_code: str | None = None,
    delivery_status: DatabaseDeliveryStatus | None = None,
) -> AgentRun:
    run = AgentRun(
        idempotency_key=_academic_morning_key(settings, local_day),
        agent_name="academic_morning_notification",
        trigger="schedule",
        schedule="academic-morning",
        input_version="academic-morning:v1",
        status=status,
        error_code=error_code,
        started_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        finished_at=(
            datetime(2026, 9, 10, 12, 5, tzinfo=UTC)
            if status
            in {
                RunStatus.SUCCEEDED,
                RunStatus.ATTENTION,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }
            else None
        ),
    )
    session.add(run)
    session.flush()
    if delivery_status is not None:
        session.add(
            Delivery(
                run_id=run.id,
                channel="discord",
                target=ACADEMIC_CHANNEL_ID,
                idempotency_key=f"{run.idempotency_key}:delivery",
                status=delivery_status,
                attempt_count=1,
                last_attempt_at=datetime(2026, 9, 10, 12, 6, tzinfo=UTC),
                external_url=(
                    f"https://discord.com/channels/@me/{ACADEMIC_CHANNEL_ID}/123456789012345679"
                    if delivery_status
                    in {
                        DatabaseDeliveryStatus.SENT,
                        DatabaseDeliveryStatus.ACKNOWLEDGED,
                    }
                    else None
                ),
                error_code=(
                    "delivery_uncertain"
                    if delivery_status is DatabaseDeliveryStatus.UNCERTAIN
                    else (
                        "connector_transient"
                        if delivery_status is DatabaseDeliveryStatus.FAILED
                        else None
                    )
                ),
            )
        )
    return run


@pytest.mark.parametrize(
    "evaluated_at",
    [
        datetime(2026, 9, 10, 11, 59, tzinfo=UTC),
        datetime(2026, 9, 10, 12, 15, tzinfo=UTC),
    ],
)
def test_academic_morning_health_is_not_overdue_before_or_inside_grace(
    health_engine: Engine,
    evaluated_at: datetime,
) -> None:
    settings = _academic_morning_settings()

    with Session(health_engine) as session, session.begin():
        health = evaluate_academic_morning_health(
            session,
            settings=settings,
            evaluated_at=evaluated_at,
        )
        persisted = session.scalar(
            select(PersistedHealthCheck).where(
                PersistedHealthCheck.check_name == "academic_morning"
            )
        )
        persisted_name = persisted.check_name if persisted is not None else None
        persisted_rule = persisted.rule if persisted is not None else None

    assert health.state is HealthState.HEALTHY
    assert health.rule == "processing_and_delivery_succeeded"
    assert health.next_expected_at == datetime(2026, 9, 10, 12, 30, tzinfo=UTC)
    assert "academic-morning:2026-09-10:0800:v1" in health.diagnostic
    assert persisted_name == "academic_morning"
    assert persisted_rule == "processing_and_delivery_succeeded"


def test_academic_morning_health_reports_absent_run_after_grace(
    health_engine: Engine,
) -> None:
    settings = _academic_morning_settings()

    with Session(health_engine) as session, session.begin():
        health = evaluate_academic_morning_health(
            session,
            settings=settings,
            evaluated_at=datetime(2026, 9, 10, 12, 31, tzinfo=UTC),
        )

    assert health.state is HealthState.ATTENTION
    assert health.rule == "run_overdue"
    assert health.next_expected_at == datetime(2026, 9, 10, 12, 30, tzinfo=UTC)
    assert "academic_morning_missing:academic-morning:2026-09-10:0800:v1" in health.diagnostic


@pytest.mark.parametrize(
    ("run_status", "expected_state", "expected_rule"),
    [
        (RunStatus.ATTENTION, HealthState.ATTENTION, "processing_completed_with_attention"),
        (RunStatus.FAILED, HealthState.FAILED, "required_processing_failed"),
    ],
)
def test_academic_morning_health_reports_failed_or_attention_run(
    health_engine: Engine,
    run_status: RunStatus,
    expected_state: HealthState,
    expected_rule: str,
) -> None:
    settings = _academic_morning_settings()

    with Session(health_engine) as session, session.begin():
        _seed_academic_morning_run(
            session,
            settings=settings,
            status=run_status,
            error_code="notion_sync_failed",
        )
        health = evaluate_academic_morning_health(
            session,
            settings=settings,
            evaluated_at=datetime(2026, 9, 10, 12, 10, tzinfo=UTC),
        )

    assert health.state is expected_state
    assert health.rule == expected_rule
    assert health.diagnostic == "notion_sync_failed"
    assert health.next_expected_at == datetime(2026, 9, 10, 12, 30, tzinfo=UTC)


def test_academic_morning_health_reports_succeeded_run_without_delivery(
    health_engine: Engine,
) -> None:
    settings = _academic_morning_settings()

    with Session(health_engine) as session, session.begin():
        _seed_academic_morning_run(session, settings=settings, status=RunStatus.SUCCEEDED)
        health = evaluate_academic_morning_health(
            session,
            settings=settings,
            evaluated_at=datetime(2026, 9, 10, 12, 10, tzinfo=UTC),
        )

    assert health.state is HealthState.FAILED
    assert health.rule == "required_delivery_failed"
    assert health.next_expected_at == datetime(2026, 9, 10, 12, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("delivery_status", "expected_state", "expected_rule"),
    [
        (DatabaseDeliveryStatus.FAILED, HealthState.FAILED, "required_delivery_failed"),
        (DatabaseDeliveryStatus.UNCERTAIN, HealthState.ATTENTION, "delivery_incomplete"),
    ],
)
def test_academic_morning_health_reports_failed_or_uncertain_delivery(
    health_engine: Engine,
    delivery_status: DatabaseDeliveryStatus,
    expected_state: HealthState,
    expected_rule: str,
) -> None:
    settings = _academic_morning_settings()

    with Session(health_engine) as session, session.begin():
        _seed_academic_morning_run(
            session,
            settings=settings,
            status=RunStatus.SUCCEEDED,
            delivery_status=delivery_status,
        )
        health = evaluate_academic_morning_health(
            session,
            settings=settings,
            evaluated_at=datetime(2026, 9, 10, 12, 10, tzinfo=UTC),
        )

    assert health.state is expected_state
    assert health.rule == expected_rule
    assert health.next_expected_at == datetime(2026, 9, 10, 12, 30, tzinfo=UTC)


def test_academic_morning_health_advances_after_successful_delivery(
    health_engine: Engine,
) -> None:
    settings = _academic_morning_settings()

    with Session(health_engine) as session, session.begin():
        _seed_academic_morning_run(
            session,
            settings=settings,
            status=RunStatus.SUCCEEDED,
            delivery_status=DatabaseDeliveryStatus.SENT,
        )
        health = evaluate_academic_morning_health(
            session,
            settings=settings,
            evaluated_at=datetime(2026, 9, 10, 12, 10, tzinfo=UTC),
        )
        persisted = session.scalar(
            select(PersistedHealthCheck).where(
                PersistedHealthCheck.check_name == "academic_morning"
            )
        )
        persisted_next_due_at = persisted.next_due_at if persisted is not None else None
        if persisted_next_due_at is not None and persisted_next_due_at.tzinfo is None:
            persisted_next_due_at = persisted_next_due_at.replace(tzinfo=UTC)

    assert health.state is HealthState.HEALTHY
    assert health.rule == "processing_and_delivery_succeeded"
    assert health.last_success_at == datetime(2026, 9, 10, 12, 5, tzinfo=UTC)
    assert health.next_expected_at == datetime(2026, 9, 11, 12, 30, tzinfo=UTC)
    assert persisted_next_due_at == datetime(2026, 9, 11, 12, 30, tzinfo=UTC)


def test_github_token_check_name_is_not_an_agent_check_name() -> None:
    settings = _github_settings()

    async def fetcher() -> InstallationToken:
        return InstallationToken(
            token="github-token-secret",
            expires_at=NOW + timedelta(hours=1),
        )

    result = asyncio.run(check_github_installation_token(settings, token_fetcher=fetcher, now=NOW))
    assert result.name not in _AGENT_CHECK_NAMES


def test_github_token_not_configured_is_healthy() -> None:
    settings = Settings(_env_file=None)

    result = asyncio.run(check_github_installation_token(settings, now=NOW))

    assert result.state is HealthState.HEALTHY
    assert "not configured" in result.diagnostic


def test_github_token_incomplete_configuration_fails() -> None:
    settings = _github_settings(github_webhook_secret=None)

    result = asyncio.run(check_github_installation_token(settings, now=NOW))

    assert result.state is HealthState.FAILED
    assert "incomplete" in result.diagnostic


def test_github_token_healthy_when_far_from_expiry() -> None:
    settings = _github_settings()

    async def fetcher() -> InstallationToken:
        return InstallationToken(
            token="github-token-secret",
            expires_at=NOW + timedelta(hours=1),
        )

    result = asyncio.run(check_github_installation_token(settings, token_fetcher=fetcher, now=NOW))

    assert result.state is HealthState.HEALTHY
    assert "github-token-secret" not in result.diagnostic


def test_queue_check_uses_configured_stall_window(monkeypatch: pytest.MonkeyPatch) -> None:
    record = QueueJobMetadata(
        job_id=1,
        status="doing",
        heartbeat_at=NOW - timedelta(seconds=121),
        queue_name="academic_planner",
        task_name="lifeagent.discord_academic",
    )
    monkeypatch.setattr(visibility, "list_queue_jobs", lambda _engine: [record])

    result = check_queue(
        SimpleNamespace(engine=object()),
        now=NOW,
        stalled_after_seconds=120,
    )

    assert result.state is HealthState.ATTENTION
    assert "stalled workers 1" in result.diagnostic


def test_github_token_near_expiry_is_attention() -> None:
    settings = _github_settings()

    async def fetcher() -> InstallationToken:
        return InstallationToken(token="tok", expires_at=NOW + timedelta(seconds=30))

    result = asyncio.run(check_github_installation_token(settings, token_fetcher=fetcher, now=NOW))

    assert result.state is HealthState.ATTENTION
    assert "refresh window" in result.diagnostic


def test_github_token_expired_fails() -> None:
    settings = _github_settings()

    async def fetcher() -> InstallationToken:
        return InstallationToken(token="tok", expires_at=NOW - timedelta(minutes=1))

    result = asyncio.run(check_github_installation_token(settings, token_fetcher=fetcher, now=NOW))

    assert result.state is HealthState.FAILED
    assert "expired" in result.diagnostic


def test_github_token_authorization_failure_fails() -> None:
    settings = _github_settings()

    async def fetcher() -> InstallationToken:
        raise authorization_error("installation revoked")

    result = asyncio.run(check_github_installation_token(settings, token_fetcher=fetcher, now=NOW))

    assert result.state is HealthState.FAILED
    assert "installation revoked" not in result.diagnostic


def test_github_token_transient_failure_is_attention() -> None:
    settings = _github_settings()

    async def fetcher() -> InstallationToken:
        raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "rate limited")

    result = asyncio.run(check_github_installation_token(settings, token_fetcher=fetcher, now=NOW))

    assert result.state is HealthState.ATTENTION


def test_discord_authentication_not_configured_is_healthy() -> None:
    settings = Settings(_env_file=None)

    result = asyncio.run(check_discord_authentication(settings))

    assert result.state is HealthState.HEALTHY
    assert "not configured" in result.diagnostic


def test_discord_api_url_defaults_and_normalizes() -> None:
    default_settings = Settings()
    normalized_settings = Settings(discord_api_base_url=f"{DISCORD_API_BASE_URL}/")

    assert default_settings.discord_api_url == DISCORD_API_BASE_URL
    assert normalized_settings.discord_api_url == DISCORD_API_BASE_URL


def test_discord_api_url_override_requires_acceptance_environment() -> None:
    with pytest.raises(ValueError, match="non-default Discord API URL"):
        Settings(discord_api_base_url="http://127.0.0.1:8765/api/v10")

    settings = Settings(
        app_environment="acceptance",
        discord_api_base_url="http://127.0.0.1:8765/api/v10/",
    )

    assert settings.discord_api_url == "http://127.0.0.1:8765/api/v10"


def test_failure_alert_adapter_from_settings_uses_configured_acceptance_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "acceptance")
    monkeypatch.setenv("DISCORD_API_BASE_URL", "http://127.0.0.1:8765/api/v10/")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-secret")

    adapter = discord_connector._failure_alert_adapter_from_settings("987654321012345678")

    assert adapter._base_url == "http://127.0.0.1:8765/api/v10"


def test_discord_authentication_success() -> None:
    settings = Settings(discord_bot_token="discord-secret")

    async def run() -> object:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "bot-id"}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_discord_authentication(settings, client)

    result = asyncio.run(run())

    assert result.state is HealthState.HEALTHY
    assert "discord-secret" not in result.diagnostic


def test_discord_authentication_uses_configured_acceptance_endpoint() -> None:
    settings = Settings(
        app_environment="acceptance",
        discord_bot_token="discord-secret",
        discord_api_base_url="http://127.0.0.1:8765/api/v10/",
    )
    requests: list[httpx.Request] = []

    async def run() -> object:
        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"id": "bot-id"})

        transport = httpx.MockTransport(respond)
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_discord_authentication(settings, client)

    result = asyncio.run(run())

    assert result.state is HealthState.HEALTHY
    assert len(requests) == 1
    assert str(requests[0].url) == "http://127.0.0.1:8765/api/v10/users/@me"
    assert requests[0].headers["Authorization"] == "Bot discord-secret"


def test_discord_authentication_unauthenticated_fails() -> None:
    settings = Settings(discord_bot_token="discord-secret")

    async def run() -> object:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(401, json={"message": "401: Unauthorized"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_discord_authentication(settings, client)

    result = asyncio.run(run())

    assert result.state is HealthState.FAILED
    assert "discord-secret" not in result.diagnostic


def test_discord_authentication_server_error_is_attention() -> None:
    settings = Settings(discord_bot_token="discord-secret")

    async def run() -> object:
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_discord_authentication(settings, client)

    result = asyncio.run(run())

    assert result.state is HealthState.ATTENTION


def test_notion_authentication_not_configured_is_healthy() -> None:
    settings = Settings(notion_token="")

    result = asyncio.run(check_notion_authentication(settings))

    assert result.state is HealthState.HEALTHY
    assert "not configured" in result.diagnostic


def test_notion_authentication_accepts_token_without_deprecated_database_ids() -> None:
    settings = _notion_settings()

    async def run() -> object:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "bot-id"}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_notion_authentication(settings, client)

    result = asyncio.run(run())

    assert result.state is HealthState.HEALTHY


def test_notion_authentication_success() -> None:
    settings = _notion_settings()

    async def run() -> object:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "bot-id"}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_notion_authentication(settings, client)

    result = asyncio.run(run())

    assert result.state is HealthState.HEALTHY
    assert "notion-secret" not in result.diagnostic


def test_notion_authentication_unauthenticated_fails() -> None:
    settings = _notion_settings()

    async def run() -> object:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(401, json={"message": "unauthorized"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_notion_authentication(settings, client)

    result = asyncio.run(run())

    assert result.state is HealthState.FAILED
    assert "notion-secret" not in result.diagnostic


def test_connector_liveness_aggregates_all_three_checks() -> None:
    settings = _github_settings(
        discord_bot_token="discord-secret",
        notion_token="notion-secret",
        notion_courses_database_id="courses",
        notion_assessments_database_id="assessments",
        notion_study_blocks_database_id="study-blocks",
    )

    async def fetcher() -> InstallationToken:
        return InstallationToken(token="tok", expires_at=NOW + timedelta(hours=1))

    async def run() -> object:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"id": "bot-id"}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_connector_liveness(
                settings, client, github_token_fetcher=fetcher, now=NOW
            )

    checks = asyncio.run(run())

    names = {check.name for check in checks}
    assert names == {
        "github_installation_token",
        "discord_authentication",
        "notion_authentication",
    }
    assert names.isdisjoint(_AGENT_CHECK_NAMES)
    assert all(check.state is HealthState.HEALTHY for check in checks)
