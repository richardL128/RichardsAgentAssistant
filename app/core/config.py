"""Typed application settings.

The settings object deliberately keeps credentials as ``SecretStr`` values and
offers a redacted diagnostics view for health pages and startup logs.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

SecretValue = SecretStr | None
DISCORD_API_BASE_URL: Final[str] = "https://discord.com/api/v10"


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
    model_trigger_mode: Literal["discord_mentions_only"] = "discord_mentions_only"
    ollama_max_concurrency: Annotated[int, Field(gt=0, le=128)] = 1
    ollama_num_ctx: Annotated[int, Field(gt=0)] = 2048
    ollama_num_batch: Annotated[int, Field(ge=32, le=512)] = 32
    ollama_timeout_seconds: Annotated[float, Field(gt=0, le=1800)] = 300.0
    ollama_model_keep_alive_seconds: Annotated[int, Field(gt=0, le=3600)] = 300
    ollama_startup_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    ollama_max_input_tokens: Annotated[int, Field(gt=0)] = 6000
    ollama_max_output_tokens: Annotated[int, Field(gt=0)] = 384
    ollama_repair_attempts: Annotated[int, Field(ge=0, le=1)] = 1
    ollama_seed: int = 1729
    ollama_reasoning: bool = False
    ollama_model_digest: str | None = (
        "d039cde69ac1f5a43d5134182adfefa65bdb533362a625b936e6171a53296eb3"
    )
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_model_digest: str | None = None
    embedding_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    discord_api_base_url: AnyHttpUrl = AnyHttpUrl(DISCORD_API_BASE_URL)

    # Connector settings are declared now so all deployment configuration has
    # one typed home, while later phases decide when to use each credential.
    github_app_id: int | None = None
    github_installation_id: int | None = None
    github_private_key: SecretValue = None
    github_webhook_secret: SecretValue = None
    discord_bot_token: SecretValue = None
    discord_webhook_secret: SecretValue = None
    discord_host_handoff_secret: SecretValue = None
    notion_token: SecretValue = None
    ops_console_username: SecretValue = None
    ops_console_password: SecretValue = None
    notion_courses_database_id: str | None = None
    notion_assessments_database_id: str | None = None
    notion_study_blocks_database_id: str | None = None
    finance_source_allowlist_version: str = "finance-sources-2026.09-v2"
    finance_eia_mode: Literal["bulk", "api"] = "bulk"
    sec_user_agent: str = "LifeAgent/0.1 contact@example.com"
    dvids_api_key: SecretValue = None
    eia_api_key: SecretValue = None
    alpha_vantage_api_key: SecretValue = None
    benzinga_api_token: SecretValue = None
    fmp_api_key: SecretValue = None
    repository_allowlist_version: str | None = None

    retry_max_attempts: Annotated[int, Field(gt=0, le=20)] = 3
    retry_base_delay_seconds: Annotated[float, Field(gt=0, le=3600)] = 5.0
    retry_max_delay_seconds: Annotated[float, Field(gt=0, le=86400)] = 300.0
    retry_jitter_ratio: Annotated[float, Field(ge=0, le=1)] = 0.2
    connector_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 10.0
    worker_concurrency: Annotated[int, Field(gt=0, le=32)] = 1
    queue_stalled_after_seconds: Annotated[int, Field(gt=0, le=86400)] = 120
    artifact_retention_days: Annotated[int, Field(gt=0)] = 30
    artifact_retention_schedule: time = time(hour=3, minute=30)
    repository_allowlist: list[str] = Field(default_factory=list)
    discord_target_channels: list[str] = Field(default_factory=list)
    discord_code_review_channel_id: str | None = None
    discord_academic_channel_id: str | None = None
    discord_finance_channel_id: str | None = None
    discord_application_id: str | None = None
    discord_academic_authorized_user_ids: list[int] = Field(default_factory=lambda: list[int]())
    discord_academic_message_content_enabled: bool = False
    discord_handoff_max_body_bytes: Annotated[int, Field(gt=0, le=65_536)] = 4_096
    discord_handoff_max_clock_skew_seconds: Annotated[int, Field(gt=0, le=300)] = 60
    discord_handoff_request_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 10.0
    discord_handoff_retry_attempts: Annotated[int, Field(gt=0, le=5)] = 3
    github_webhook_max_body_bytes: Annotated[int, Field(gt=0, le=10_485_760)] = 1_048_576
    git_clone_timeout_seconds: Annotated[float, Field(gt=0, le=600)] = 60.0
    code_command_timeout_seconds: Annotated[float, Field(gt=0, le=1800)] = 120.0
    code_command_output_bytes: Annotated[int, Field(gt=0, le=10_485_760)] = 262_144
    review_min_confidence: Annotated[float, Field(ge=0, le=1)] = 0.75
    code_review_schedule: time = time(hour=18)
    # Phase 4 — account-scale ingestion and daily operation.
    code_review_quick_scan_enabled: bool = True
    code_review_catchup_enabled: bool = False
    code_review_catchup_max_days: Annotated[int, Field(ge=0, le=30)] = 3
    code_review_daily_max_commits: Annotated[int, Field(gt=0, le=500)] = 50
    code_review_profile_refresh_days: Annotated[int, Field(gt=0, le=365)] = 30
    code_review_discovery_scope: str = "github-installation"
    github_discovery_page_size: Annotated[int, Field(gt=0, le=100)] = 50
    github_min_call_interval_seconds: Annotated[float, Field(ge=0, le=60)] = 2.0
    notion_attachment_max_bytes: Annotated[int, Field(gt=0, le=104_857_600)] = 25_165_824
    notion_material_max_block_depth: Annotated[int, Field(gt=0, le=16)] = 8
    notion_material_max_blocks: Annotated[int, Field(gt=0, le=5_000)] = 1_000
    notion_material_max_cursor_pages: Annotated[int, Field(gt=0, le=100)] = 20
    academic_material_pdf_max_pages: Annotated[int, Field(gt=0, le=15)] = 15
    academic_material_ocr_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 30.0
    academic_material_ocr_min_page_chars: Annotated[int, Field(ge=0, le=1_000)] = 24
    academic_material_retrieval_limit: Annotated[int, Field(gt=0, le=8)] = 8
    academic_material_agent_max_turns: Annotated[int, Field(gt=0, le=4)] = 4
    academic_material_prompt_max_chars: Annotated[int, Field(ge=1_000, le=16_000)] = 16_000
    academic_plan_horizon_days: Annotated[int, Field(ge=7, le=14)] = 14
    academic_buffer_ratio: Annotated[float, Field(ge=0.05, le=0.5)] = 0.15
    academic_default_block_minutes: Annotated[int, Field(ge=15, le=240)] = 60
    academic_memory_enabled: bool = True
    academic_memory_default_practice_minutes: Annotated[int, Field(ge=5, le=180)] = 30
    academic_memory_snooze_after_missed_checkins: Annotated[int, Field(ge=1, le=30)] = 2
    academic_memory_delete_after_missed_checkins: Annotated[int, Field(ge=1, le=30)] = 5
    academic_sync_lookback_days: Annotated[int, Field(ge=0, le=30)] = 2
    academic_confirmation_ttl_hours: Annotated[int, Field(gt=0, le=168)] = 24
    academic_morning_schedule: time = time(hour=8)
    academic_end_of_day_schedule: time = time(hour=21)
    finance_market_open_schedule: time = time(hour=9)
    finance_feed_poll_minutes: Annotated[int, Field(ge=5, le=10)] = 10
    finance_federal_register_poll_minutes: Annotated[int, Field(ge=15, le=1440)] = 60
    finance_eia_bulk_poll_minutes: Annotated[int, Field(ge=60, le=1440)] = 720
    finance_eia_api_poll_minutes: Annotated[int, Field(ge=5, le=1440)] = 60
    finance_etf_poll_minutes: Annotated[int, Field(ge=60, le=2880)] = 1440
    finance_cold_start_backfill_hours: Annotated[int, Field(ge=1, le=168)] = 24
    finance_registry_max_fanout: Annotated[int, Field(ge=1, le=100)] = 20
    finance_bulk_max_payload_bytes: Annotated[int, Field(ge=1_048_576, le=268_435_456)] = 67_108_864

    @field_validator(
        "github_private_key",
        "github_webhook_secret",
        "discord_bot_token",
        "discord_webhook_secret",
        "discord_host_handoff_secret",
        "notion_token",
        "ops_console_username",
        "ops_console_password",
        "dvids_api_key",
        "eia_api_key",
        "alpha_vantage_api_key",
        "benzinga_api_token",
        "fmp_api_key",
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
        "repository_allowlist_version",
        "discord_code_review_channel_id",
        "discord_academic_channel_id",
        "discord_finance_channel_id",
        "discord_application_id",
        "notion_courses_database_id",
        "notion_assessments_database_id",
        "notion_study_blocks_database_id",
        "embedding_model_digest",
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

    @field_validator("discord_academic_authorized_user_ids", mode="before")
    @classmethod
    def empty_authorized_user_list_is_empty(cls, value: Any) -> Any:
        return [] if value == "" else value

    @field_validator("code_review_discovery_scope")
    @classmethod
    def discovery_scope_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("code_review_discovery_scope must not be empty")
        return value

    @field_validator("finance_source_allowlist_version")
    @classmethod
    def finance_allowlist_version_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("finance source allowlist version must not be empty")
        return value

    @field_validator("sec_user_agent")
    @classmethod
    def sec_user_agent_is_descriptive(cls, value: str) -> str:
        if "/" not in value or "@" not in value or len(value) < 12:
            raise ValueError("SEC user agent must identify the application and a contact")
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
        "artifact_retention_schedule",
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
        if self.discord_api_url != DISCORD_API_BASE_URL and self.app_environment != "acceptance":
            raise ValueError(
                "non-default Discord API URL is only allowed in acceptance environment"
            )
        if self.finance_eia_mode == "api" and self.eia_api_key is None:
            raise ValueError("FINANCE_EIA_MODE=api requires EIA_API_KEY")
        if (
            self.academic_memory_snooze_after_missed_checkins
            > self.academic_memory_delete_after_missed_checkins
        ):
            raise ValueError("academic memory snooze threshold cannot exceed delete threshold")
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

    @field_validator(
        "discord_code_review_channel_id",
        "discord_academic_channel_id",
        "discord_finance_channel_id",
        "discord_application_id",
    )
    @classmethod
    def discord_identifier_is_numeric(cls, value: str | None) -> str | None:
        if value is not None and not value.isdigit():
            raise ValueError("Discord channel/application identifiers must be numeric IDs")
        return value

    @field_validator("discord_academic_authorized_user_ids")
    @classmethod
    def discord_authorized_users_are_positive(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)) or any(item <= 0 for item in value):
            raise ValueError(
                "Discord academic authorized user IDs must be unique positive integers"
            )
        return value

    @property
    def ollama_url(self) -> str:
        """Return the Ollama URL without a trailing slash."""

        return str(self.ollama_base_url).rstrip("/")

    @property
    def discord_api_url(self) -> str:
        """Return the Discord API URL without a trailing slash."""

        return str(self.discord_api_base_url).rstrip("/")

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
            "model_trigger_mode": self.model_trigger_mode,
            "ollama_max_concurrency": self.ollama_max_concurrency,
            "ollama_num_ctx": self.ollama_num_ctx,
            "ollama_num_batch": self.ollama_num_batch,
            "ollama_max_input_tokens": self.ollama_max_input_tokens,
            "ollama_max_output_tokens": self.ollama_max_output_tokens,
            "ollama_model_keep_alive_seconds": self.ollama_model_keep_alive_seconds,
            "ollama_startup_timeout_seconds": self.ollama_startup_timeout_seconds,
            "ollama_repair_attempts": self.ollama_repair_attempts,
            "ollama_seed": self.ollama_seed,
            "ollama_reasoning": self.ollama_reasoning,
            "ollama_model_digest": self.ollama_model_digest,
            "embedding_model": self.embedding_model,
            "embedding_model_digest": self.embedding_model_digest,
            "embedding_timeout_seconds": self.embedding_timeout_seconds,
            "discord_api_base_url": self._safe_url(str(self.discord_api_base_url)),
            "retry_max_attempts": self.retry_max_attempts,
            "retry_base_delay_seconds": self.retry_base_delay_seconds,
            "retry_max_delay_seconds": self.retry_max_delay_seconds,
            "retry_jitter_ratio": self.retry_jitter_ratio,
            "connector_timeout_seconds": self.connector_timeout_seconds,
            "worker_concurrency": self.worker_concurrency,
            "queue_stalled_after_seconds": self.queue_stalled_after_seconds,
            "artifact_retention_days": self.artifact_retention_days,
            "artifact_retention_schedule": self.artifact_retention_schedule.isoformat(
                timespec="minutes"
            ),
            "repository_allowlist_count": len(self.repository_allowlist),
            "repository_allowlist_version": self.repository_allowlist_version,
            "discord_target_count": len(self.discord_target_channels),
            "discord_code_review_channel_configured": (
                self.discord_code_review_channel_id is not None
            ),
            "discord_academic_channel_configured": self.discord_academic_channel_id is not None,
            "discord_finance_channel_configured": self.discord_finance_channel_id is not None,
            "discord_application_id_configured": self.discord_application_id is not None,
            "discord_academic_authorized_user_count": len(
                self.discord_academic_authorized_user_ids
            ),
            "discord_academic_message_content_enabled": (
                self.discord_academic_message_content_enabled
            ),
            "discord_host_handoff_configured": self.discord_host_handoff_secret is not None,
            "discord_handoff_max_body_bytes": self.discord_handoff_max_body_bytes,
            "discord_handoff_max_clock_skew_seconds": (self.discord_handoff_max_clock_skew_seconds),
            "discord_handoff_request_timeout_seconds": (
                self.discord_handoff_request_timeout_seconds
            ),
            "discord_handoff_retry_attempts": self.discord_handoff_retry_attempts,
            "ops_console_auth_configured": (
                self.ops_console_username is not None and self.ops_console_password is not None
            ),
            "finance_source_allowlist_version": self.finance_source_allowlist_version,
            "finance_eia_mode": self.finance_eia_mode,
            "sec_user_agent_configured": bool(self.sec_user_agent),
            "finance_feed_poll_minutes": self.finance_feed_poll_minutes,
            "finance_federal_register_poll_minutes": (self.finance_federal_register_poll_minutes),
            "finance_eia_bulk_poll_minutes": self.finance_eia_bulk_poll_minutes,
            "finance_eia_api_poll_minutes": self.finance_eia_api_poll_minutes,
            "finance_etf_poll_minutes": self.finance_etf_poll_minutes,
            "finance_cold_start_backfill_hours": self.finance_cold_start_backfill_hours,
            "finance_registry_max_fanout": self.finance_registry_max_fanout,
            "finance_bulk_max_payload_bytes": self.finance_bulk_max_payload_bytes,
            "finance_source_credentials_configured": sum(
                value is not None
                for value in (
                    self.dvids_api_key,
                    self.eia_api_key,
                    self.alpha_vantage_api_key,
                    self.benzinga_api_token,
                    self.fmp_api_key,
                )
            ),
            "notion_database_count": sum(
                value is not None
                for value in (
                    self.notion_courses_database_id,
                    self.notion_assessments_database_id,
                    self.notion_study_blocks_database_id,
                )
            ),
            "notion_token_configured": self.notion_token is not None,
            "notion_courses_database_configured": self.notion_courses_database_id is not None,
            "notion_deprecated_database_metadata_count": sum(
                value is not None
                for value in (
                    self.notion_assessments_database_id,
                    self.notion_study_blocks_database_id,
                )
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
            "code_review_quick_scan_enabled": self.code_review_quick_scan_enabled,
            "code_review_catchup_enabled": self.code_review_catchup_enabled,
            "code_review_catchup_max_days": self.code_review_catchup_max_days,
            "code_review_daily_max_commits": self.code_review_daily_max_commits,
            "code_review_profile_refresh_days": self.code_review_profile_refresh_days,
            "code_review_discovery_scope": self.code_review_discovery_scope,
            "github_discovery_page_size": self.github_discovery_page_size,
            "github_min_call_interval_seconds": self.github_min_call_interval_seconds,
            "notion_attachment_max_bytes": self.notion_attachment_max_bytes,
            "notion_material_max_block_depth": self.notion_material_max_block_depth,
            "notion_material_max_blocks": self.notion_material_max_blocks,
            "notion_material_max_cursor_pages": self.notion_material_max_cursor_pages,
            "academic_material_pdf_max_pages": self.academic_material_pdf_max_pages,
            "academic_material_ocr_timeout_seconds": (self.academic_material_ocr_timeout_seconds),
            "academic_material_ocr_min_page_chars": (self.academic_material_ocr_min_page_chars),
            "academic_material_retrieval_limit": self.academic_material_retrieval_limit,
            "academic_material_agent_max_turns": self.academic_material_agent_max_turns,
            "academic_material_prompt_max_chars": self.academic_material_prompt_max_chars,
            "academic_plan_horizon_days": self.academic_plan_horizon_days,
            "academic_buffer_ratio": self.academic_buffer_ratio,
            "academic_default_block_minutes": self.academic_default_block_minutes,
            "academic_memory_enabled": self.academic_memory_enabled,
            "academic_memory_default_practice_minutes": (
                self.academic_memory_default_practice_minutes
            ),
            "academic_memory_snooze_after_missed_checkins": (
                self.academic_memory_snooze_after_missed_checkins
            ),
            "academic_memory_delete_after_missed_checkins": (
                self.academic_memory_delete_after_missed_checkins
            ),
            "academic_sync_lookback_days": self.academic_sync_lookback_days,
            "academic_confirmation_ttl_hours": self.academic_confirmation_ttl_hours,
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
