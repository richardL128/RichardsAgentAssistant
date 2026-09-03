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
        connection.execute(text("CREATE TABLE procrastinate_jobs (id INTEGER PRIMARY KEY)"))
    settings = Settings(database_url=database_url, artifact_root=tmp_path / "artifacts")
    database = Database(settings)

    async def run() -> object:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"models": []}))
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
