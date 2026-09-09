"""Unit coverage for Phase 8 connector-token/liveness health diagnostics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.connectors import discord as discord_connector
from app.connectors.github import InstallationToken
from app.core.config import DISCORD_API_BASE_URL, Settings
from app.core.errors import ErrorCode, authorization_error, transient_error
from app.health.checks import (
    HealthState,
    check_connector_liveness,
    check_discord_authentication,
    check_github_installation_token,
    check_notion_authentication,
    check_queue,
)
from app.queue import visibility
from app.queue.visibility import QueueJobMetadata

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

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
        queue_name="code_review",
        task_name="lifeagent.code_review",
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
