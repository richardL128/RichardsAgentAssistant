"""Unit coverage for the Phase 0 application core."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.db.session import Database
from app.health.checks import HealthState, check_ollama, readiness


def test_settings_diagnostics_redact_credentials(tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://user:password@example.test:5432/lifeagent",
        artifact_root=tmp_path,
        github_webhook_secret="webhook-secret",
    )

    diagnostics = settings.safe_diagnostics()

    assert "password" not in str(diagnostics)
    assert "webhook-secret" not in str(diagnostics)
    assert diagnostics["database"] == "postgresql+psycopg://example.test:5432/lifeagent"


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
    assert result.status is HealthState.HEALTHY
    assert {check.name for check in result.checks} == {
        "database",
        "procrastinate",
        "shared_schema",
        "checkpoints",
        "code_review_schema",
        "artifacts",
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
