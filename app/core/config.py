"""Typed application settings.

The settings object deliberately keeps credentials as ``SecretStr`` values and
offers a redacted diagnostics view for health pages and startup logs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

SecretValue = SecretStr | None


class Settings(BaseSettings):
    """Validated configuration loaded from environment and ``.env``.

    Empty optional credentials are normalized to ``None``.  This is useful for
    local Phase 0 deployments where integrations are intentionally disabled.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "LifeAgent"
    app_version: str = "0.1.0"
    app_environment: str = "development"
    app_timezone: str = "America/Toronto"

    database_url: str = "postgresql+psycopg://lifeagent:lifeagent@localhost:5432/lifeagent"
    database_connect_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 3.0

    artifact_root: Path = Path("/var/lib/lifeagent/artifacts")
    artifact_write_probe: bool = True

    ollama_base_url: AnyHttpUrl = AnyHttpUrl("http://host.docker.internal:11434")
    ollama_model: str = "qwen3.8:27b"
    ollama_max_concurrency: Annotated[int, Field(gt=0, le=128)] = 1
    ollama_num_ctx: Annotated[int, Field(gt=0)] = 8192
    ollama_timeout_seconds: Annotated[float, Field(gt=0, le=1800)] = 300.0
    ollama_max_input_tokens: Annotated[int, Field(gt=0)] = 6000
    ollama_max_output_tokens: Annotated[int, Field(gt=0)] = 1024
    ollama_repair_attempts: Annotated[int, Field(ge=0, le=1)] = 1
    ollama_seed: int = 1729
    ollama_model_digest: str | None = None
    embedding_model: str = "qwen3-embedding:0.6b"

    # Connector settings are declared now so all deployment configuration has
    # one typed home, while later phases decide when to use each credential.
    github_app_id: int | None = None
    github_private_key: SecretValue = None
    github_webhook_secret: SecretValue = None
    discord_bot_token: SecretValue = None
    discord_webhook_secret: SecretValue = None
    notion_token: SecretValue = None
    finance_source_allowlist_version: str | None = None

    retry_max_attempts: Annotated[int, Field(gt=0, le=20)] = 3
    connector_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 10.0
    artifact_retention_days: Annotated[int, Field(gt=0)] = 30
    repository_allowlist: list[str] = Field(default_factory=list)
    discord_target_channels: list[str] = Field(default_factory=list)

    @field_validator(
        "github_private_key",
        "github_webhook_secret",
        "discord_bot_token",
        "discord_webhook_secret",
        "notion_token",
        mode="before",
    )
    @classmethod
    def empty_secret_is_none(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("app_timezone")
    @classmethod
    def timezone_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("app_timezone must not be empty")
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value

    @field_validator("ollama_model", "embedding_model")
    @classmethod
    def model_name_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model name must not be empty")
        return value

    @property
    def ollama_url(self) -> str:
        """Return the Ollama URL without a trailing slash."""

        return str(self.ollama_base_url).rstrip("/")

    def safe_diagnostics(self) -> dict[str, Any]:
        """Return settings suitable for logs or a health response.

        Secret fields are excluded and the database URL is reduced to its
        scheme/host/port/database components, omitting credentials and query
        parameters.  The URL can therefore be shown without leaking a password
        or an accidentally embedded token.
        """

        return {
            "app_name": self.app_name,
            "app_version": self.app_version,
            "app_environment": self.app_environment,
            "app_timezone": self.app_timezone,
            "database": self._safe_database_target(self.database_url),
            "artifact_root": str(self.artifact_root),
            "ollama_base_url": self._safe_url(str(self.ollama_base_url)),
            "ollama_model": self.ollama_model,
            "ollama_max_concurrency": self.ollama_max_concurrency,
            "ollama_num_ctx": self.ollama_num_ctx,
            "ollama_max_input_tokens": self.ollama_max_input_tokens,
            "ollama_max_output_tokens": self.ollama_max_output_tokens,
            "ollama_repair_attempts": self.ollama_repair_attempts,
            "ollama_seed": self.ollama_seed,
            "ollama_model_digest": self.ollama_model_digest,
            "embedding_model": self.embedding_model,
            "retry_max_attempts": self.retry_max_attempts,
            "connector_timeout_seconds": self.connector_timeout_seconds,
            "artifact_retention_days": self.artifact_retention_days,
            "repository_allowlist_count": len(self.repository_allowlist),
            "discord_target_count": len(self.discord_target_channels),
        }

    def diagnostics(self) -> dict[str, Any]:
        """Compatibility alias for callers that need redacted diagnostics."""

        return self.safe_diagnostics()

    @staticmethod
    def _safe_url(value: str) -> str:
        parsed = urlsplit(value)
        host = parsed.hostname or "unknown"
        netloc = host
        if parsed.port is not None:
            netloc = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))

    @classmethod
    def _safe_database_target(cls, value: str) -> str:
        # SQLAlchemy's URL parser handles driver names such as
        # ``postgresql+psycopg`` and lets us omit the password/user safely.
        try:
            parsed = make_url(value)
            host = parsed.host or "unknown"
            netloc = host
            if parsed.port is not None:
                netloc = f"{host}:{parsed.port}"
            database = parsed.database or ""
            return f"{parsed.drivername}://{netloc}/{database}".rstrip("/")
        except (ValueError, TypeError):
            return value.split(":", 1)[0]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process settings singleton."""

    return Settings()
