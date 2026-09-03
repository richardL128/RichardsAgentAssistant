"""Typed application settings.

The settings object deliberately keeps credentials as ``SecretStr`` values and
offers a redacted diagnostics view for health pages and startup logs.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Self
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
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
    ollama_model: str = "qwen3-32gb:latest"
    ollama_max_concurrency: Annotated[int, Field(gt=0, le=128)] = 1
    ollama_num_ctx: Annotated[int, Field(gt=0)] = 2048
    ollama_num_batch: Annotated[int, Field(ge=32, le=512)] = 32
    ollama_timeout_seconds: Annotated[float, Field(gt=0, le=1800)] = 300.0
    ollama_max_input_tokens: Annotated[int, Field(gt=0)] = 6000
    ollama_max_output_tokens: Annotated[int, Field(gt=0)] = 384
    ollama_repair_attempts: Annotated[int, Field(ge=0, le=1)] = 1
    ollama_seed: int = 1729
    ollama_reasoning: bool = False
    ollama_model_digest: str | None = (
        "d039cde69ac1f5a43d5134182adfefa65bdb533362a625b936e6171a53296eb3"
    )
    embedding_model: str = "qwen3-embedding:0.6b"

    # Connector settings are declared now so all deployment configuration has
    # one typed home, while later phases decide when to use each credential.
    github_app_id: int | None = None
    github_installation_id: int | None = None
    github_private_key: SecretValue = None
    github_webhook_secret: SecretValue = None
    discord_bot_token: SecretValue = None
    discord_webhook_secret: SecretValue = None
    notion_token: SecretValue = None
    finance_source_allowlist_version: str | None = None
    repository_allowlist_version: str | None = None

    retry_max_attempts: Annotated[int, Field(gt=0, le=20)] = 3
    retry_base_delay_seconds: Annotated[float, Field(gt=0, le=3600)] = 5.0
    retry_max_delay_seconds: Annotated[float, Field(gt=0, le=86400)] = 300.0
    retry_jitter_ratio: Annotated[float, Field(ge=0, le=1)] = 0.2
    connector_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 10.0
    worker_concurrency: Annotated[int, Field(gt=0, le=32)] = 1
    queue_stalled_after_seconds: Annotated[int, Field(gt=0, le=86400)] = 120
    artifact_retention_days: Annotated[int, Field(gt=0)] = 30
    repository_allowlist: list[str] = Field(default_factory=list)
    discord_target_channels: list[str] = Field(default_factory=list)
    discord_code_review_channel_id: str | None = None
    github_webhook_max_body_bytes: Annotated[int, Field(gt=0, le=10_485_760)] = 1_048_576
    git_clone_timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 60.0
    code_command_timeout_seconds: Annotated[float, Field(gt=0, le=1800)] = 120.0
    code_command_output_bytes: Annotated[int, Field(gt=0, le=10_485_760)] = 262_144
    review_min_confidence: Annotated[float, Field(ge=0, le=1)] = 0.75
    code_review_schedule: time = time(hour=18)
    academic_morning_schedule: time = time(hour=8)
    academic_end_of_day_schedule: time = time(hour=21)
    finance_market_open_schedule: time = time(hour=9)

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

    @field_validator("github_app_id", "github_installation_id", mode="before")
    @classmethod
    def empty_optional_integer_is_none(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator(
        "finance_source_allowlist_version",
        "repository_allowlist_version",
        "discord_code_review_channel_id",
        mode="before",
    )
    @classmethod
    def empty_optional_string_is_none(cls, value: Any) -> Any:
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

    @field_validator(
        "code_review_schedule",
        "academic_morning_schedule",
        "academic_end_of_day_schedule",
        "finance_market_open_schedule",
    )
    @classmethod
    def schedule_is_minute_local_time(cls, value: time) -> time:
        if value.tzinfo is not None or value.second != 0 or value.microsecond != 0:
            raise ValueError("schedules must be local wall-clock times at minute precision")
        return value

    @model_validator(mode="after")
    def retry_delays_are_ordered(self) -> Self:
        if self.retry_base_delay_seconds > self.retry_max_delay_seconds:
            raise ValueError("retry base delay cannot exceed retry maximum delay")
        if self.repository_allowlist and not self.repository_allowlist_version:
            raise ValueError("a non-empty repository allowlist requires a version")
        return self

    @field_validator("repository_allowlist")
    @classmethod
    def repositories_are_full_names(cls, value: list[str]) -> list[str]:
        import re

        pattern = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
        if len(value) != len(set(value)) or any(pattern.fullmatch(item) is None for item in value):
            raise ValueError("repository allowlist must contain unique owner/name entries")
        return value

    @field_validator("discord_target_channels")
    @classmethod
    def discord_channels_are_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not item.isdigit() for item in value):
            raise ValueError("Discord target channels must be unique numeric IDs")
        return value

    @field_validator("discord_code_review_channel_id")
    @classmethod
    def discord_code_review_channel_is_id(cls, value: str | None) -> str | None:
        if value is not None and not value.isdigit():
            raise ValueError("Discord code-review channel must be a numeric ID")
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
            "ollama_num_batch": self.ollama_num_batch,
            "ollama_max_input_tokens": self.ollama_max_input_tokens,
            "ollama_max_output_tokens": self.ollama_max_output_tokens,
            "ollama_repair_attempts": self.ollama_repair_attempts,
            "ollama_seed": self.ollama_seed,
            "ollama_reasoning": self.ollama_reasoning,
            "ollama_model_digest": self.ollama_model_digest,
            "embedding_model": self.embedding_model,
            "retry_max_attempts": self.retry_max_attempts,
            "retry_base_delay_seconds": self.retry_base_delay_seconds,
            "retry_max_delay_seconds": self.retry_max_delay_seconds,
            "retry_jitter_ratio": self.retry_jitter_ratio,
            "connector_timeout_seconds": self.connector_timeout_seconds,
            "worker_concurrency": self.worker_concurrency,
            "queue_stalled_after_seconds": self.queue_stalled_after_seconds,
            "artifact_retention_days": self.artifact_retention_days,
            "repository_allowlist_count": len(self.repository_allowlist),
            "repository_allowlist_version": self.repository_allowlist_version,
            "discord_target_count": len(self.discord_target_channels),
            "discord_code_review_channel_configured": (
                self.discord_code_review_channel_id is not None
            ),
            "github_app_configured": all(
                (
                    self.github_app_id is not None,
                    self.github_installation_id is not None,
                    self.github_private_key is not None,
                    self.github_webhook_secret is not None,
                )
            ),
            "github_webhook_max_body_bytes": self.github_webhook_max_body_bytes,
            "git_clone_timeout_seconds": self.git_clone_timeout_seconds,
            "code_command_timeout_seconds": self.code_command_timeout_seconds,
            "code_command_output_bytes": self.code_command_output_bytes,
            "review_min_confidence": self.review_min_confidence,
            "code_review_schedule": self.code_review_schedule.isoformat(timespec="minutes"),
            "academic_morning_schedule": self.academic_morning_schedule.isoformat(
                timespec="minutes"
            ),
            "academic_end_of_day_schedule": self.academic_end_of_day_schedule.isoformat(
                timespec="minutes"
            ),
            "finance_market_open_schedule": self.finance_market_open_schedule.isoformat(
                timespec="minutes"
            ),
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
