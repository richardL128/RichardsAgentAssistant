"""Health probes for the local platform dependencies."""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.connectors.github import GitHubAppConnector, InstallationToken
from app.connectors.notion import NOTION_API_BASE_URL, NOTION_API_VERSION
from app.core.config import Settings
from app.core.errors import ErrorCategory, LifeAgentError
from app.db.session import Database

GITHUB_TOKEN_REFRESH_WINDOW = timedelta(minutes=1)
_NOTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

GitHubTokenFetcher = Callable[[], Awaitable[InstallationToken]]


class HealthState(StrEnum):
    HEALTHY = "healthy"
    ATTENTION = "attention"
    FAILED = "failed"


class HealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    state: HealthState
    diagnostic: str


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: HealthState
    checks: list[HealthCheck] = Field(default_factory=lambda: list[HealthCheck]())
    version: str


class OllamaModelRecord(BaseModel):
    """The identity fields needed to verify the configured local model."""

    model_config = ConfigDict(extra="ignore")

    name: str
    digest: str


class OllamaTagsResponse(BaseModel):
    """Minimal allowlisted shape from Ollama's non-secret tags endpoint."""

    model_config = ConfigDict(extra="ignore")

    models: list[OllamaModelRecord] = Field(default_factory=lambda: list[OllamaModelRecord]())


def check_artifact_root(settings: Settings) -> HealthCheck:
    root = settings.artifact_root
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            return HealthCheck(
                name="artifacts",
                state=HealthState.FAILED,
                diagnostic="artifact root is not a directory",
            )
        if settings.artifact_write_probe:
            with tempfile.NamedTemporaryFile(prefix=".health-", dir=root, delete=True):
                pass
        return HealthCheck(
            name="artifacts",
            state=HealthState.HEALTHY,
            diagnostic="artifact root is writable",
        )
    except (OSError, PermissionError) as exc:
        return HealthCheck(
            name="artifacts",
            state=HealthState.FAILED,
            diagnostic=f"artifact root is not writable ({exc.__class__.__name__})",
        )


def check_database(database: Database) -> tuple[HealthCheck, ...]:
    connected, connection_detail = database.check_connection()
    connection_check = HealthCheck(
        name="database",
        state=HealthState.HEALTHY if connected else HealthState.FAILED,
        diagnostic=connection_detail,
    )
    if not connected:
        return (
            connection_check,
            *(
                HealthCheck(
                    name=name,
                    state=HealthState.FAILED,
                    diagnostic="not checked: database unavailable",
                )
                for name in (
                    "procrastinate",
                    "shared_schema",
                    "checkpoints",
                    "code_review_schema",
                )
            ),
        )
    schema_ok, schema_detail = database.check_procrastinate_schema()
    shared_ok, shared_detail = database.check_shared_schema()
    checkpoint_ok, checkpoint_detail = database.check_checkpoint_schema()
    code_review_ok, code_review_detail = database.check_code_review_schema()
    return (
        connection_check,
        HealthCheck(
            name="procrastinate",
            state=HealthState.HEALTHY if schema_ok else HealthState.FAILED,
            diagnostic=schema_detail,
        ),
        HealthCheck(
            name="shared_schema",
            state=HealthState.HEALTHY if shared_ok else HealthState.FAILED,
            diagnostic=shared_detail,
        ),
        HealthCheck(
            name="checkpoints",
            state=HealthState.HEALTHY if checkpoint_ok else HealthState.FAILED,
            diagnostic=checkpoint_detail,
        ),
        HealthCheck(
            name="code_review_schema",
            state=HealthState.HEALTHY if code_review_ok else HealthState.FAILED,
            diagnostic=code_review_detail,
        ),
    )


def check_queue(
    database: Database,
    *,
    now: datetime | None = None,
    stalled_after_seconds: int = 60,
) -> HealthCheck:
    """Report queue depth and terminal failures without exposing job arguments."""

    from app.queue.visibility import QueueVisibility, list_queue_jobs, queue_visibility

    try:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        stalled_after = timedelta(seconds=stalled_after_seconds)
        states = [
            queue_visibility(record, now=current, stalled_after=stalled_after)
            for record in list_queue_jobs(database.engine)
        ]
        active = sum(
            state
            in {
                QueueVisibility.QUEUED,
                QueueVisibility.RUNNING,
                QueueVisibility.RETRYING,
                QueueVisibility.STALLED,
            }
            for state in states
        )
        failed = states.count(QueueVisibility.FAILED)
        stalled = states.count(QueueVisibility.STALLED)
        retrying = states.count(QueueVisibility.RETRYING)
        return HealthCheck(
            name="queue",
            state=HealthState.ATTENTION if failed or stalled else HealthState.HEALTHY,
            diagnostic=(
                f"queue depth {active}; terminal failures {failed}; "
                f"stalled workers {stalled}; retrying {retrying}"
            ),
        )
    except (SQLAlchemyError, OSError, ValueError) as exc:
        return HealthCheck(
            name="queue",
            state=HealthState.FAILED,
            diagnostic=f"queue health unavailable ({exc.__class__.__name__})",
        )


def check_connector_configuration(settings: Settings) -> HealthCheck:
    """Fail when a configured connector target lacks its matching credential set."""

    missing: list[str] = []
    discord_targets = (
        *settings.discord_target_channels,
        settings.discord_code_review_channel_id,
        settings.discord_academic_channel_id,
        settings.discord_finance_channel_id,
    )
    if any(discord_targets) and settings.discord_bot_token is None:
        missing.append("discord")
    github_parts = (
        settings.github_app_id,
        settings.github_installation_id,
        settings.github_private_key,
        settings.github_webhook_secret,
    )
    if any(value is not None for value in github_parts) and not all(
        value is not None for value in github_parts
    ):
        missing.append("github")
    if missing:
        return HealthCheck(
            name="connector_configuration",
            state=HealthState.FAILED,
            diagnostic=f"configured connector credential set is incomplete: {', '.join(missing)}",
        )
    configured = sum(
        (
            settings.discord_bot_token is not None,
            settings.notion_token is not None,
            settings.github_private_key is not None,
        )
    )
    return HealthCheck(
        name="connector_configuration",
        state=HealthState.HEALTHY,
        diagnostic=f"configured connector credential sets are internally consistent ({configured})",
    )


def check_academic_notion_status(
    settings: Settings,
    database: Database | None = None,
) -> HealthCheck:
    """Report non-secret academic Notion setup and persisted sync state."""

    token_configured = settings.notion_token is not None
    courses_configured = settings.notion_courses_database_id is not None
    if not token_configured or not courses_configured:
        return HealthCheck(
            name="academic_notion",
            state=HealthState.ATTENTION,
            diagnostic=(
                "Academic Notion setup incomplete; "
                f"token configured={token_configured}; "
                f"courses database configured={courses_configured}; "
                "no Notion changes were made"
            ),
        )
    assert settings.notion_courses_database_id is not None
    if _NOTION_ID_PATTERN.fullmatch(settings.notion_courses_database_id) is None:
        return HealthCheck(
            name="academic_notion",
            state=HealthState.ATTENTION,
            diagnostic="Academic Notion Courses database configuration is invalid",
        )
    if database is None:
        return HealthCheck(
            name="academic_notion",
            state=HealthState.HEALTHY,
            diagnostic="Academic Notion configuration is present; persisted sync state not checked",
        )
    try:
        from app.db.academic import SQLAlchemyAcademicPlannerStore

        snapshot = SQLAlchemyAcademicPlannerStore(database.engine).academic_notion_health()
    except (SQLAlchemyError, OSError, ValueError) as exc:
        return HealthCheck(
            name="academic_notion",
            state=HealthState.ATTENTION,
            diagnostic=f"Academic Notion persistence unavailable ({exc.__class__.__name__})",
        )
    invalid = int(snapshot.get("invalid_calendar_count", 0))
    pending = int(snapshot.get("pending_clarification_count", 0))
    write_failures = int(snapshot.get("write_failure_count", 0))
    reminders = int(snapshot.get("setup_reminder_count", 0))
    setup_codes = snapshot.get("setup_condition_codes", [])
    setup_summary = ",".join(str(code) for code in setup_codes) if setup_codes else "none"
    state = (
        HealthState.ATTENTION
        if invalid or pending or write_failures or reminders
        else HealthState.HEALTHY
    )
    return HealthCheck(
        name="academic_notion",
        state=state,
        diagnostic=(
            f"courses {snapshot.get('course_count', 0)}; "
            f"calendars {snapshot.get('calendar_count', 0)}; "
            f"invalid calendars {invalid}; "
            f"active assessments {snapshot.get('active_assessment_count', 0)}; "
            f"pending clarifications {pending}; "
            f"write failures {write_failures}; "
            f"setup reminders {reminders}; "
            f"setup conditions {setup_summary}; "
            f"last sync {snapshot.get('last_sync_at') or 'never'}; "
            f"migration {snapshot.get('migration', 'unknown')}"
        ),
    )


def check_academic_discord_gateway(settings: Settings, runtime_state: str | None) -> HealthCheck:
    """Expose Gateway/message-content setup without account or credential details."""

    if not settings.discord_academic_gateway_enabled:
        if settings.discord_academic_message_content_enabled:
            return HealthCheck(
                name="academic_discord_gateway",
                state=HealthState.ATTENTION,
                diagnostic=(
                    "Academic Discord free-text is enabled but the Gateway listener is disabled; "
                    "enable the Gateway or disable free-text"
                ),
            )
        return HealthCheck(
            name="academic_discord_gateway",
            state=HealthState.HEALTHY,
            diagnostic="Academic Discord Gateway listener is disabled",
        )
    configured = (
        settings.discord_bot_token is not None
        and settings.discord_academic_channel_id is not None
        and bool(settings.discord_academic_authorized_user_ids)
    )
    if not configured or runtime_state == "setup_required":
        return HealthCheck(
            name="academic_discord_gateway",
            state=HealthState.ATTENTION,
            diagnostic="Academic Discord Gateway setup is incomplete",
        )
    state = runtime_state or "unknown"
    if state == "message_content_intent_unavailable":
        return HealthCheck(
            name="academic_discord_gateway",
            state=HealthState.FAILED,
            diagnostic=(
                "Discord rejected the Message Content privileged intent; enable it in the "
                "Developer Portal or disable academic free-text"
            ),
        )
    return HealthCheck(
        name="academic_discord_gateway",
        state=HealthState.HEALTHY if state == "running" else HealthState.ATTENTION,
        diagnostic=f"Academic Discord Gateway listener state: {state}",
    )


async def check_ollama(settings: Settings, client: httpx.AsyncClient | None = None) -> HealthCheck:
    """Probe only the non-secret Ollama tags endpoint.

    Ollama is optional during local bootstrap.  An unavailable endpoint is
    therefore ``attention`` rather than a failed platform health state.
    """

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=settings.ollama_timeout_seconds)
    assert client is not None
    try:
        response = await client.get(f"{settings.ollama_url}/api/tags")
        response.raise_for_status()
        payload = OllamaTagsResponse.model_validate(response.json())
        model_count = len(payload.models)
        if settings.ollama_model_digest:
            expected = next(
                (model for model in payload.models if model.name == settings.ollama_model),
                None,
            )
            if expected is None:
                return HealthCheck(
                    name="ollama",
                    state=HealthState.ATTENTION,
                    diagnostic=(
                        "configured Ollama model is not installed; model features are degraded"
                    ),
                )
            if expected.digest != settings.ollama_model_digest:
                return HealthCheck(
                    name="ollama",
                    state=HealthState.ATTENTION,
                    diagnostic=(
                        "configured Ollama model digest does not match; model features are degraded"
                    ),
                )
        return HealthCheck(
            name="ollama",
            state=HealthState.HEALTHY,
            diagnostic=(
                f"Ollama /api/tags responded ({model_count} model(s) advertised); "
                "configured model identity verified"
                if settings.ollama_model_digest
                else f"Ollama /api/tags responded ({model_count} model(s) advertised)"
            ),
        )
    except (httpx.HTTPError, ValidationError, ValueError, TypeError) as exc:
        return HealthCheck(
            name="ollama",
            state=HealthState.ATTENTION,
            diagnostic=(
                f"Ollama unavailable ({exc.__class__.__name__}); model features are degraded"
            ),
        )
    finally:
        if owns_client:
            await client.aclose()


async def check_github_installation_token(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    *,
    token_fetcher: GitHubTokenFetcher | None = None,
    now: datetime | None = None,
) -> HealthCheck:
    """Exchange a GitHub installation token and report only its expiry window."""

    parts = (
        settings.github_app_id,
        settings.github_installation_id,
        settings.github_private_key,
        settings.github_webhook_secret,
    )
    if all(value is None for value in parts):
        return HealthCheck(
            name="github_installation_token",
            state=HealthState.HEALTHY,
            diagnostic="GitHub connector is not configured",
        )
    if not all(value is not None for value in parts):
        return HealthCheck(
            name="github_installation_token",
            state=HealthState.FAILED,
            diagnostic="GitHub connector credential set is incomplete",
        )

    current = (now or datetime.now(UTC)).astimezone(UTC)
    owns_client = client is None and token_fetcher is None
    if client is None and token_fetcher is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(settings.connector_timeout_seconds))
    try:
        if token_fetcher is None:
            assert settings.github_app_id is not None
            assert settings.github_private_key is not None
            assert settings.github_installation_id is not None
            connector = GitHubAppConnector(
                app_id=settings.github_app_id,
                private_key=settings.github_private_key,
                repository_allowlist=settings.repository_allowlist,
                webhook_secret=settings.github_webhook_secret,
                client=client,
                timeout_seconds=settings.connector_timeout_seconds,
                clock=lambda: current,
            )
            token = await connector.exchange_installation_token(settings.github_installation_id)
        else:
            token = await token_fetcher()
        expiry = token.expires_at
        if expiry is None:
            return HealthCheck(
                name="github_installation_token",
                state=HealthState.ATTENTION,
                diagnostic="GitHub installation token expiry was not returned",
            )
        expires_at = expiry.astimezone(UTC)
        remaining = expires_at - current
        minutes = max(0, int(remaining.total_seconds() // 60))
        if remaining <= timedelta(0):
            return HealthCheck(
                name="github_installation_token",
                state=HealthState.FAILED,
                diagnostic="GitHub installation token is expired",
            )
        if remaining <= GITHUB_TOKEN_REFRESH_WINDOW:
            return HealthCheck(
                name="github_installation_token",
                state=HealthState.ATTENTION,
                diagnostic=(
                    "GitHub installation token expires within the connector refresh window "
                    f"({minutes} minute(s) remaining)"
                ),
            )
        return HealthCheck(
            name="github_installation_token",
            state=HealthState.HEALTHY,
            diagnostic=f"GitHub installation token expires in {minutes} minute(s)",
        )
    except LifeAgentError as exc:
        return _connector_exception_check("github_installation_token", "GitHub", exc)
    except (httpx.HTTPError, ValidationError, ValueError, TypeError) as exc:
        return HealthCheck(
            name="github_installation_token",
            state=HealthState.ATTENTION,
            diagnostic=f"GitHub token probe unavailable ({exc.__class__.__name__})",
        )
    finally:
        if owns_client and client is not None:
            await client.aclose()


async def check_discord_authentication(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> HealthCheck:
    """Validate that the configured Discord bot token still authenticates."""

    token = settings.discord_bot_token
    if token is None:
        return HealthCheck(
            name="discord_authentication",
            state=HealthState.HEALTHY,
            diagnostic="Discord connector is not configured",
        )
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(settings.connector_timeout_seconds))
    try:
        response = await client.get(
            f"{settings.discord_api_url}/users/@me",
            headers={"Authorization": f"Bot {token.get_secret_value()}"},
        )
        return _authentication_response_check("discord_authentication", "Discord", response)
    except httpx.TransportError as exc:
        return HealthCheck(
            name="discord_authentication",
            state=HealthState.ATTENTION,
            diagnostic=f"Discord authentication probe unavailable ({exc.__class__.__name__})",
        )
    finally:
        if owns_client:
            await client.aclose()


async def check_notion_authentication(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> HealthCheck:
    """Validate that the configured Notion token still authenticates."""

    token = settings.notion_token
    if token is None:
        return HealthCheck(
            name="notion_authentication",
            state=HealthState.HEALTHY,
            diagnostic="Notion connector is not configured",
        )
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(settings.connector_timeout_seconds))
    try:
        response = await client.get(
            f"{NOTION_API_BASE_URL}/users/me",
            headers={
                "Authorization": f"Bearer {token.get_secret_value()}",
                "Notion-Version": NOTION_API_VERSION,
            },
        )
        return _authentication_response_check("notion_authentication", "Notion", response)
    except httpx.TransportError as exc:
        return HealthCheck(
            name="notion_authentication",
            state=HealthState.ATTENTION,
            diagnostic=f"Notion authentication probe unavailable ({exc.__class__.__name__})",
        )
    finally:
        if owns_client:
            await client.aclose()


async def check_connector_liveness(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    *,
    github_token_fetcher: GitHubTokenFetcher | None = None,
    now: datetime | None = None,
) -> tuple[HealthCheck, ...]:
    """Run live connector authentication probes with injectable network access."""

    github = await check_github_installation_token(
        settings,
        client,
        token_fetcher=github_token_fetcher,
        now=now,
    )
    discord, notion = await asyncio.gather(
        check_discord_authentication(settings, client),
        check_notion_authentication(settings, client),
    )
    return (github, discord, notion)


def _authentication_response_check(
    name: str,
    connector: str,
    response: httpx.Response,
) -> HealthCheck:
    if response.status_code in {401, 403}:
        return HealthCheck(
            name=name,
            state=HealthState.FAILED,
            diagnostic=f"{connector} connector authentication is invalid",
        )
    if response.status_code == 429 or response.status_code >= 500:
        return HealthCheck(
            name=name,
            state=HealthState.ATTENTION,
            diagnostic=f"{connector} authentication endpoint is temporarily unavailable",
        )
    if response.status_code >= 400:
        return HealthCheck(
            name=name,
            state=HealthState.ATTENTION,
            diagnostic=f"{connector} authentication probe was rejected",
        )
    return HealthCheck(
        name=name,
        state=HealthState.HEALTHY,
        diagnostic=f"{connector} connector token authenticated",
    )


def _connector_exception_check(name: str, connector: str, exc: LifeAgentError) -> HealthCheck:
    if exc.record.category is ErrorCategory.AUTHORIZATION:
        return HealthCheck(
            name=name,
            state=HealthState.FAILED,
            diagnostic=f"{connector} connector authentication is invalid",
        )
    if exc.record.category is ErrorCategory.TRANSIENT:
        return HealthCheck(
            name=name,
            state=HealthState.ATTENTION,
            diagnostic=f"{connector} token probe unavailable ({exc.record.code.value})",
        )
    return HealthCheck(
        name=name,
        state=HealthState.ATTENTION,
        diagnostic=f"{connector} token probe was rejected ({exc.record.code.value})",
    )


async def readiness(
    settings: Settings,
    database: Database,
    *,
    ollama_client: httpx.AsyncClient | None = None,
    version: str,
) -> HealthResponse:
    db_checks = await asyncio.to_thread(check_database, database)
    checks = [*db_checks, check_artifact_root(settings)]
    checks.append(check_academic_notion_status(settings, database))
    checks.append(await check_ollama(settings, ollama_client))
    states = {check.state for check in checks}
    if HealthState.FAILED in states:
        status = HealthState.FAILED
    elif HealthState.ATTENTION in states:
        status = HealthState.ATTENTION
    else:
        status = HealthState.HEALTHY
    return HealthResponse(status=status, checks=checks, version=version)
