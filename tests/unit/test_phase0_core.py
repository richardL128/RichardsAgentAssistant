"""Unit coverage for the Phase 0 application core."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.db.session import Database
from app.health.checks import (
    HealthState,
    check_academic_discord_gateway,
    check_academic_notion_status,
    check_connector_configuration,
    check_ollama,
    readiness,
)


def test_settings_diagnostics_redact_credentials(tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:password@example.test:5432/lifeagent",
        artifact_root=tmp_path,
        github_webhook_secret="webhook-secret",
        dvids_api_key="dvids-secret",
        eia_api_key="eia-secret",
        discord_finance_channel_id="123456789",
        ops_console_username="ops-user-never-print",
        ops_console_password="ops-password",
        notion_token="",
        notion_courses_database_id="",
    )

    diagnostics = settings.safe_diagnostics()

    assert "password" not in str(diagnostics)
    assert "webhook-secret" not in str(diagnostics)
    assert "dvids-secret" not in str(diagnostics)
    assert "eia-secret" not in str(diagnostics)
    assert "ops-password" not in str(diagnostics)
    assert "ops-user-never-print" not in str(diagnostics)
    assert diagnostics["database"] == "postgresql+psycopg://example.test:5432/lifeagent"
    assert diagnostics["finance_source_allowlist_version"] == "finance-sources-2026.09"
    assert diagnostics["finance_source_credentials_configured"] == 2
    assert diagnostics["discord_finance_channel_configured"] is True
    assert diagnostics["notion_token_configured"] is False
    assert diagnostics["notion_courses_database_configured"] is False
    assert diagnostics["notion_deprecated_database_metadata_count"] == 0
    assert diagnostics["discord_academic_authorized_user_count"] == 0
    assert diagnostics["discord_academic_gateway_enabled"] is False
    assert diagnostics["ops_console_auth_configured"] is True


def test_empty_finance_credentials_are_normalized() -> None:
    settings = Settings(
        dvids_api_key="",
        eia_api_key="",
        alpha_vantage_api_key="",
        benzinga_api_token="",
        fmp_api_key="",
        ops_console_username="",
        ops_console_password="",
    )

    assert settings.finance_source_allowlist_version == "finance-sources-2026.09"
    assert settings.safe_diagnostics()["finance_source_credentials_configured"] == 0
    assert settings.safe_diagnostics()["ops_console_auth_configured"] is False


def test_academic_notion_settings_are_setup_not_startup_requirements() -> None:
    token_only = Settings(notion_token="notion-secret")
    deprecated_only = Settings(
        notion_token="",
        notion_assessments_database_id="old-assessments",
        notion_study_blocks_database_id="old-study-blocks",
    )
    configured = Settings(
        notion_token="notion-secret",
        notion_courses_database_id="courses",
        discord_academic_authorized_user_ids=[123456789],
        discord_academic_gateway_enabled=True,
    )

    assert token_only.notion_courses_database_id is None
    assert deprecated_only.notion_token is None
    assert configured.safe_diagnostics()["notion_deprecated_database_metadata_count"] == 0
    assert configured.safe_diagnostics()["discord_academic_authorized_user_count"] == 1
    assert configured.safe_diagnostics()["discord_academic_gateway_enabled"] is True
    assert "notion-secret" not in str(configured.safe_diagnostics())


def test_connector_configuration_fails_when_a_target_has_no_credential() -> None:
    incomplete = check_connector_configuration(Settings(discord_finance_channel_id="123456789"))
    complete = check_connector_configuration(
        Settings(
            discord_finance_channel_id="123456789",
            discord_bot_token="discord-secret",
        )
    )
    notion_setup = check_connector_configuration(
        Settings(notion_token="", notion_courses_database_id="courses")
    )

    assert incomplete.state is HealthState.FAILED
    assert incomplete.diagnostic.endswith("discord")
    assert complete.state is HealthState.HEALTHY
    assert "discord-secret" not in complete.diagnostic
    assert notion_setup.state is HealthState.HEALTHY


def test_academic_notion_missing_config_is_attention() -> None:
    missing = check_academic_notion_status(Settings(notion_token="", notion_courses_database_id=""))
    configured = check_academic_notion_status(
        Settings(notion_token="notion-secret", notion_courses_database_id="courses")
    )
    invalid = check_academic_notion_status(
        Settings(notion_token="notion-secret", notion_courses_database_id="not valid")
    )

    assert missing.state is HealthState.ATTENTION
    assert "token configured=False" in missing.diagnostic
    assert "no Notion changes were made" in missing.diagnostic
    assert configured.state is HealthState.HEALTHY
    assert invalid.state is HealthState.ATTENTION
    assert "invalid" in invalid.diagnostic
    assert "notion-secret" not in configured.diagnostic


def test_academic_discord_gateway_health_is_non_secret_and_actionable() -> None:
    disabled = check_academic_discord_gateway(Settings(), "disabled")
    incomplete = check_academic_discord_gateway(
        Settings(discord_academic_gateway_enabled=True),
        "setup_required",
    )
    running = check_academic_discord_gateway(
        Settings(
            discord_bot_token="discord-secret",
            discord_academic_channel_id="123456789",
            discord_academic_authorized_user_ids=[987654321],
            discord_academic_gateway_enabled=True,
            notion_token="notion-secret",
            notion_courses_database_id="courses",
        ),
        "running",
    )

    assert disabled.state is HealthState.HEALTHY
    assert incomplete.state is HealthState.ATTENTION
    assert running.state is HealthState.HEALTHY
    assert "secret" not in running.diagnostic


def test_readiness_reports_all_phase0_dependencies(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'lifeagent.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for table_name in (
            "procrastinate_jobs",
            "agent_runs",
            "approval_requests",
            "audit_events",
            "deliveries",
            "evidence_refs",
            "health_checks",
            "run_steps",
            "ui_acknowledgements",
            "checkpoint_blobs",
            "checkpoint_migrations",
            "checkpoint_writes",
            "checkpoints",
            "project_profiles",
            "repositories",
            "review_findings",
            "reviewed_commits",
        ):
            connection.execute(text(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY)"))
    settings = Settings(database_url=database_url, artifact_root=tmp_path / "artifacts")
    database = Database(settings)

    async def run() -> object:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "qwen3-32gb:latest",
                            "digest": (
                                "d039cde69ac1f5a43d5134182adfefa65bdb533362a625b936e6171a53296eb3"
                            ),
                        }
                    ]
                },
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await readiness(
                settings,
                database,
                ollama_client=client,
                version="test",
            )

    result = asyncio.run(run())
    assert result.status is HealthState.ATTENTION
    assert {check.name for check in result.checks} == {
        "database",
        "procrastinate",
        "shared_schema",
        "checkpoints",
        "code_review_schema",
        "artifacts",
        "academic_notion",
        "ollama",
    }


def test_ollama_absence_is_degraded_attention(tmp_path: Path) -> None:
    settings = Settings(database_url="sqlite+pysqlite:///:memory:", artifact_root=tmp_path)

    async def run() -> object:
        def fail(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        transport = httpx.MockTransport(fail)
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_ollama(settings, client)

    result = asyncio.run(run())
    assert result.state is HealthState.ATTENTION
    assert "model features are degraded" in result.diagnostic


def test_ollama_pinned_identity_must_match(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        artifact_root=tmp_path,
        ollama_model="qwen3-32gb:latest",
        ollama_model_digest="expected-digest",
    )

    async def run(digest: str) -> object:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen3-32gb:latest", "digest": digest, "size": 123},
                    ]
                },
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await check_ollama(settings, client)

    mismatch = asyncio.run(run("different-digest"))
    matching = asyncio.run(run("expected-digest"))
    assert mismatch.state is HealthState.ATTENTION
    assert "digest does not match" in mismatch.diagnostic
    assert matching.state is HealthState.HEALTHY
    assert "identity verified" in matching.diagnostic
