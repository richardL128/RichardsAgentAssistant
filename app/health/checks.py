"""Health probes for the local platform dependencies."""

from __future__ import annotations

import asyncio
import tempfile
from enum import StrEnum

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.db.session import Database


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


async def readiness(
    settings: Settings,
    database: Database,
    *,
    ollama_client: httpx.AsyncClient | None = None,
    version: str,
) -> HealthResponse:
    db_checks = await asyncio.to_thread(check_database, database)
    checks = [*db_checks, check_artifact_root(settings)]
    checks.append(await check_ollama(settings, ollama_client))
    states = {check.state for check in checks}
    if HealthState.FAILED in states:
        status = HealthState.FAILED
    elif HealthState.ATTENTION in states:
        status = HealthState.ATTENTION
    else:
        status = HealthState.HEALTHY
    return HealthResponse(status=status, checks=checks, version=version)
