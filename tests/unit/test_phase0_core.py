"""Unit coverage for the Phase 0 application core."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.db.session import Database
from app.health.checks import (
    HealthState,
    check_academic_discord_handoff,
    check_academic_discord_host_ingress,
    check_academic_embeddings,
    check_academic_notion_status,
    check_connector_configuration,
    check_ollama,
    readiness,
)
from app.main import create_app

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_settings_diagnostics_redact_credentials(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
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
    assert diagnostics["finance_source_allowlist_version"] == "finance-sources-2026.09-v2"
    assert diagnostics["finance_source_credentials_configured"] == 2
    assert diagnostics["discord_finance_channel_configured"] is True
    assert diagnostics["notion_token_configured"] is False
    assert diagnostics["notion_courses_database_configured"] is False
    assert diagnostics["notion_deprecated_database_metadata_count"] == 0
    assert diagnostics["discord_academic_authorized_user_count"] == 0
    assert diagnostics["discord_host_handoff_configured"] is False
    assert diagnostics["discord_academic_message_content_enabled"] is False
    assert diagnostics["ops_console_auth_configured"] is True
    assert diagnostics["ollama_context_reserve_tokens"] == 4_096
    assert diagnostics["user_memory_enabled"] is True
    assert diagnostics["user_memory_retrieval_limit"] == 8
    assert diagnostics["user_memory_context_max_chars"] == 3_000
    assert diagnostics["conversation_summary_enabled"] is True
    assert diagnostics["conversation_compaction_trigger_tokens"] == 19_968
    assert diagnostics["conversation_compaction_target_tokens"] == 14_336
    assert diagnostics["conversation_recent_tail_max_tokens"] == 8_192
    assert diagnostics["conversation_compaction_max_output_tokens"] == 2_048
    assert diagnostics["conversation_context_manifest_retention_days"] == 30


def test_learn_bridge_settings_are_local_secret_gated_and_redacted() -> None:
    with pytest.raises(ValidationError, match="LEARN_BRIDGE_HMAC_SECRET"):
        Settings(_env_file=None, learn_bridge_enabled=True)
    with pytest.raises(ValidationError, match="local host"):
        Settings(_env_file=None, learn_bridge_url="http://example.com:8765")

    settings = Settings(
        _env_file=None,
        learn_bridge_enabled=True,
        learn_bridge_hmac_secret="learn-secret-value-that-must-not-leak",
    )
    diagnostics = settings.safe_diagnostics()

    assert diagnostics["learn_bridge_enabled"] is True
    assert diagnostics["learn_bridge_hmac_configured"] is True
    assert "learn-secret-value" not in str(diagnostics)


def test_empty_finance_credentials_are_normalized() -> None:
    settings = Settings(
        _env_file=None,
        dvids_api_key="",
        eia_api_key="",
        alpha_vantage_api_key="",
        benzinga_api_token="",
        fmp_api_key="",
        ops_console_username="",
        ops_console_password="",
    )

    assert settings.finance_source_allowlist_version == "finance-sources-2026.09-v2"
    assert settings.safe_diagnostics()["finance_source_credentials_configured"] == 0
    assert settings.safe_diagnostics()["ops_console_auth_configured"] is False


def test_qwen_runtime_settings_are_closed_and_bounded() -> None:
    settings = Settings(_env_file=None)

    assert settings.ollama_model == "qwen3:14b"
    assert (
        settings.ollama_model_digest
        == "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8"
    )
    assert settings.model_trigger_mode == "authorized_discord_channel"
    assert settings.ollama_num_ctx == 32_768
    assert settings.ollama_num_batch == 32
    assert settings.ollama_max_input_tokens == 26_624
    assert settings.ollama_max_output_tokens == 2_048
    assert settings.ollama_context_reserve_tokens == 4_096
    assert settings.ollama_max_concurrency == 1
    assert settings.ollama_timeout_seconds == 300
    assert settings.ollama_model_keep_alive_seconds == 300
    assert settings.ollama_reasoning is False
    assert settings.ollama_structured_output_transport == "json_schema"
    assert settings.conversation_compaction_trigger_tokens == 19_968
    assert settings.conversation_compaction_target_tokens == 14_336
    assert settings.conversation_recent_tail_max_tokens == 8_192
    assert settings.conversation_compaction_max_output_tokens == 2_048
    assert (
        settings.ollama_max_input_tokens
        + settings.ollama_max_output_tokens
        + settings.ollama_context_reserve_tokens
        <= settings.ollama_num_ctx
    )
    assert settings.ollama_startup_timeout_seconds == 30
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_trigger_mode="scheduled")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ollama_model_keep_alive_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ollama_startup_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ollama_max_concurrency=2)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            ollama_num_ctx=4096,
            ollama_max_input_tokens=3000,
            ollama_max_output_tokens=1024,
            ollama_context_reserve_tokens=1536,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            conversation_compaction_target_tokens=19_968,
            conversation_compaction_trigger_tokens=14_336,
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, conversation_recent_tail_max_tokens=26_624)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, conversation_compaction_max_output_tokens=4_096)


def test_fixed_pdf_page_limit_parses_from_container_environment(monkeypatch) -> None:
    monkeypatch.setenv("ACADEMIC_MATERIAL_PDF_MAX_PAGES", "15")

    settings = Settings(_env_file=None)

    assert settings.academic_material_pdf_max_pages == 15
    with pytest.raises(ValidationError):
        Settings(_env_file=None, academic_material_pdf_max_pages=16)


def test_default_compose_enables_only_discord_mention_model_triggers() -> None:
    compose = (REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "MODEL_TRIGGER_MODE: ${MODEL_TRIGGER_MODE:-authorized_discord_channel}" in compose
    assert "OLLAMA_MODEL: ${OLLAMA_MODEL:-qwen3:14b}" in compose
    assert "OLLAMA_NUM_CTX: ${OLLAMA_NUM_CTX:-32768}" in compose
    assert "OLLAMA_MAX_INPUT_TOKENS: ${OLLAMA_MAX_INPUT_TOKENS:-26624}" in compose
    assert "OLLAMA_MAX_OUTPUT_TOKENS: ${OLLAMA_MAX_OUTPUT_TOKENS:-2048}" in compose
    assert "OLLAMA_CONTEXT_RESERVE_TOKENS: ${OLLAMA_CONTEXT_RESERVE_TOKENS:-4096}" in compose
    assert (
        "OLLAMA_STRUCTURED_OUTPUT_TRANSPORT: "
        "${OLLAMA_STRUCTURED_OUTPUT_TRANSPORT:-json_schema}" in compose
    )
    assert "USER_MEMORY_ENABLED: ${USER_MEMORY_ENABLED:-true}" in compose
    assert "USER_MEMORY_RETRIEVAL_LIMIT: ${USER_MEMORY_RETRIEVAL_LIMIT:-8}" in compose
    assert "CONVERSATION_SUMMARY_ENABLED: ${CONVERSATION_SUMMARY_ENABLED:-true}" in compose
    assert (
        "CONVERSATION_COMPACTION_TRIGGER_TOKENS: "
        "${CONVERSATION_COMPACTION_TRIGGER_TOKENS:-19968}" in compose
    )
    assert (
        "CONVERSATION_COMPACTION_TARGET_TOKENS: "
        "${CONVERSATION_COMPACTION_TARGET_TOKENS:-14336}" in compose
    )
    assert "OLLAMA_MODEL_KEEP_ALIVE_SECONDS" in compose
    assert "OLLAMA_STARTUP_TIMEOUT_SECONDS" in compose
    assert "worker-code-review:" not in compose
    assert "worker-academic-planner:" in compose
    assert "worker-finance:" not in compose


def test_default_api_does_not_mount_legacy_model_queue_ingress(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'model-ingress.db'}",
        artifact_root=tmp_path / "artifacts",
    )
    application = create_app(settings)
    try:
        with TestClient(application) as client:
            handoff = client.post("/internal/discord/academic/handoff")
            legacy_webhook = client.post("/webhooks/github")
        assert handoff.status_code != 404
        assert legacy_webhook.status_code == 404
    finally:
        application.state.database.dispose()


def test_academic_notion_settings_are_setup_not_startup_requirements() -> None:
    token_only = Settings(_env_file=None, notion_token="notion-secret")
    deprecated_only = Settings(
        _env_file=None,
        notion_token="",
        notion_assessments_database_id="old-assessments",
    )
    configured = Settings(
        _env_file=None,
        notion_token="notion-secret",
        notion_courses_database_id="courses",
        discord_academic_authorized_user_ids=[123456789],
        discord_academic_message_content_enabled=True,
        discord_host_handoff_secret="handoff-secret",
    )

    assert token_only.notion_courses_database_id is None
    assert deprecated_only.notion_token is None
    assert deprecated_only.safe_diagnostics()["notion_deprecated_database_metadata_count"] == 1
    assert configured.safe_diagnostics()["notion_deprecated_database_metadata_count"] == 0
    assert configured.safe_diagnostics()["discord_academic_authorized_user_count"] == 1
    assert configured.safe_diagnostics()["discord_host_handoff_configured"] is True
    assert "notion-secret" not in str(configured.safe_diagnostics())


def test_connector_configuration_fails_when_a_target_has_no_credential() -> None:
    incomplete = check_connector_configuration(
        Settings(_env_file=None, discord_finance_channel_id="123456789")
    )
    complete = check_connector_configuration(
        Settings(
            _env_file=None,
            discord_finance_channel_id="123456789",
            discord_bot_token="discord-secret",
        )
    )
    notion_setup = check_connector_configuration(
        Settings(_env_file=None, notion_token="", notion_courses_database_id="courses")
    )

    assert incomplete.state is HealthState.FAILED
    assert incomplete.diagnostic.endswith("discord")
    assert complete.state is HealthState.HEALTHY
    assert "discord-secret" not in complete.diagnostic
    assert notion_setup.state is HealthState.HEALTHY


def test_academic_notion_missing_config_is_attention() -> None:
    missing = check_academic_notion_status(
        Settings(_env_file=None, notion_token="", notion_courses_database_id="")
    )
    configured = check_academic_notion_status(
        Settings(
            _env_file=None,
            notion_token="notion-secret",
            notion_courses_database_id="courses",
        )
    )
    invalid = check_academic_notion_status(
        Settings(
            _env_file=None,
            notion_token="notion-secret",
            notion_courses_database_id="not valid",
        )
    )

    assert missing.state is HealthState.ATTENTION
    assert "token configured=False" in missing.diagnostic
    assert "no Notion changes were made" in missing.diagnostic
    assert configured.state is HealthState.HEALTHY
    assert invalid.state is HealthState.ATTENTION
    assert "invalid" in invalid.diagnostic
    assert "notion-secret" not in configured.diagnostic


def test_academic_discord_host_handoff_health_is_non_secret_and_actionable() -> None:
    ingress = check_academic_discord_host_ingress()
    incomplete = check_academic_discord_handoff(Settings(_env_file=None))
    configured = check_academic_discord_handoff(
        Settings(
            _env_file=None,
            discord_bot_token="discord-secret",
            discord_application_id="111111111111111111",
            discord_academic_channel_id="123456789",
            discord_academic_authorized_user_ids=[987654321],
            discord_academic_message_content_enabled=True,
            discord_host_handoff_secret="handoff-secret",
        ),
    )

    assert ingress.state is HealthState.ATTENTION
    assert "cannot be verified" in ingress.diagnostic
    assert incomplete.state is HealthState.ATTENTION
    assert configured.state is HealthState.HEALTHY
    assert "secret" not in configured.diagnostic


def test_readiness_reports_all_phase0_dependencies(tmp_path: Path, monkeypatch) -> None:
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
            "native_conversation_inbound_events",
            "native_conversation_sessions",
            "native_conversation_compactions",
            "user_memory_events",
            "user_memory_facts",
            "project_profiles",
            "repositories",
            "review_findings",
            "reviewed_commits",
        ):
            connection.execute(text(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY)"))
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        artifact_root=tmp_path / "artifacts",
    )
    database = Database(settings)
    from app.db.academic import AcademicRepository

    monkeypatch.setattr(
        AcademicRepository,
        "count_active_assessment_material_chunks",
        staticmethod(lambda _session: 0),
    )
    monkeypatch.setattr(
        AcademicRepository,
        "count_material_embedding_backfill_candidates",
        staticmethod(lambda _session, *, embedding_model: 0),
    )
    monkeypatch.setattr(
        AcademicRepository,
        "count_reflection_memories",
        staticmethod(lambda _session: 0),
    )
    monkeypatch.setattr(
        AcademicRepository,
        "count_reflection_embedding_backfill_candidates",
        staticmethod(lambda _session, *, embedding_model: 0),
    )

    async def run() -> object:
        requests: list[tuple[str, str]] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.url.path == "/api/show":
                return httpx.Response(200, json={"capabilities": ["embedding"]})
            if request.url.path == "/api/embed":
                return httpx.Response(200, json={"embeddings": [[0.0] * 1024]})
            if request.url.path == "/api/ps":
                return httpx.Response(200, json={"models": []})
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "qwen3:14b",
                            "digest": (
                                "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8"
                            ),
                        },
                        {
                            "name": "qwen3-embedding:4b",
                            "digest": "embedding-digest",
                        },
                    ]
                },
            )

        transport = httpx.MockTransport(respond)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await readiness(
                settings,
                database,
                ollama_client=client,
                version="test",
            )
        assert requests == [
            ("GET", "/api/tags"),
            ("GET", "/api/ps"),
            ("GET", "/api/tags"),
            ("POST", "/api/show"),
            ("POST", "/api/embed"),
        ]
        return result

    result = asyncio.run(run())
    assert result.status is HealthState.ATTENTION
    assert {check.name for check in result.checks} == {
        "database",
        "procrastinate",
        "shared_schema",
        "checkpoints",
        "native_conversations",
        "code_review_schema",
        "artifacts",
        "academic_notion",
        "academic_end_of_day",
        "ollama",
        "academic_embeddings",
    }


def test_academic_embeddings_readiness_fails_when_material_vectors_are_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'lifeagent.db'}",
        artifact_root=tmp_path / "artifacts",
        ollama_base_url="http://ollama.test:11434",
    )
    database = Database(settings)
    from app.db.academic import AcademicRepository

    monkeypatch.setattr(
        AcademicRepository,
        "count_active_assessment_material_chunks",
        staticmethod(lambda _session: 7),
    )
    monkeypatch.setattr(
        AcademicRepository,
        "count_material_embedding_backfill_candidates",
        staticmethod(lambda _session, *, embedding_model: 2),
    )
    monkeypatch.setattr(
        AcademicRepository,
        "count_reflection_memories",
        staticmethod(lambda _session: 5),
    )
    monkeypatch.setattr(
        AcademicRepository,
        "count_reflection_embedding_backfill_candidates",
        staticmethod(lambda _session, *, embedding_model: 0),
    )

    async def run() -> object:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/show":
                return httpx.Response(200, json={"capabilities": ["embedding"]})
            if request.url.path == "/api/embed":
                return httpx.Response(200, json={"embeddings": [[0.0] * 1024]})
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "qwen3-embedding:4b",
                            "digest": "embedding-digest",
                        },
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await check_academic_embeddings(settings, client, database=database)

    result = asyncio.run(run())
    assert result.state is HealthState.FAILED
    assert "pending material embeddings 2" in result.diagnostic


def test_academic_embeddings_readiness_fails_when_reflection_vectors_are_stale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+pysqlite:///{tmp_path / 'lifeagent.db'}",
        artifact_root=tmp_path / "artifacts",
        ollama_base_url="http://ollama.test:11434",
    )
    database = Database(settings)
    from app.db.academic import AcademicRepository

    monkeypatch.setattr(
        AcademicRepository,
        "count_active_assessment_material_chunks",
        staticmethod(lambda _session: 3),
    )
    monkeypatch.setattr(
        AcademicRepository,
        "count_material_embedding_backfill_candidates",
        staticmethod(lambda _session, *, embedding_model: 0),
    )
    monkeypatch.setattr(
        AcademicRepository,
        "count_reflection_memories",
        staticmethod(lambda _session: 4),
    )
    monkeypatch.setattr(
        AcademicRepository,
        "count_reflection_embedding_backfill_candidates",
        staticmethod(lambda _session, *, embedding_model: 2),
    )

    async def run() -> object:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/show":
                return httpx.Response(200, json={"capabilities": ["embedding"]})
            if request.url.path == "/api/embed":
                return httpx.Response(200, json={"embeddings": [[0.0] * 1024]})
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "qwen3-embedding:4b",
                            "digest": "embedding-digest",
                        },
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await check_academic_embeddings(settings, client, database=database)

    result = asyncio.run(run())
    assert result.state is HealthState.FAILED
    assert "reflection memories 4" in result.diagnostic
    assert "pending reflection embeddings 2" in result.diagnostic


def test_ollama_absence_is_degraded_attention(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        artifact_root=tmp_path,
    )

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
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        artifact_root=tmp_path,
        ollama_model="qwen3:14b",
        ollama_model_digest="expected-digest",
    )

    async def run(digest: str) -> object:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen3:14b", "digest": digest, "size": 123},
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


def test_ollama_diagnostic_reports_resident_context_and_vram(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite:///:memory:",
        artifact_root=tmp_path,
        ollama_model="qwen3:14b",
        ollama_model_digest="expected-digest",
        ollama_num_ctx=32_768,
    )

    async def run(context_length: int) -> object:
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/ps":
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {
                                "name": "qwen3:14b",
                                "context_length": context_length,
                                "size_vram": 25_000_000_000,
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen3:14b", "digest": "expected-digest"},
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await check_ollama(settings, client)

    matching = asyncio.run(run(32_768))
    mismatch = asyncio.run(run(16_384))
    assert matching.state is HealthState.HEALTHY
    assert "resident context_length=32768" in matching.diagnostic
    assert "size_vram=25000000000" in matching.diagnostic
    assert "host_memory_free_percent=" in matching.diagnostic
    assert mismatch.state is HealthState.ATTENTION
    assert "expected context_length=32768" in mismatch.diagnostic
