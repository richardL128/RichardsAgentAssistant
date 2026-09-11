"""Stable, redacted error contracts shared by workers and the API."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ErrorCategory(StrEnum):
    """Retry behavior category decided by deterministic application code."""

    TRANSIENT = "transient"
    AUTHORIZATION = "authorization"
    PERMANENT = "permanent"


class ErrorCode(StrEnum):
    """Allowlisted error codes safe to persist and expose in the operations UI."""

    CONNECTOR_TRANSIENT = "connector_transient"
    MODEL_TRANSIENT = "model_transient"
    AUTHORIZATION_INVALID = "authorization_invalid"
    INPUT_INVALID = "input_invalid"
    ANALYSIS_INVALID_OUTPUT = "analysis_invalid_output"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    SCHEDULE_LATE = "schedule_late"
    SOURCE_SETUP_REQUIRED = "source_setup_required"
    SOURCE_SYNC_FAILED = "source_sync_failed"
    SOURCE_SYNC_PARTIAL = "source_sync_partial"
    SOURCE_STALE = "source_stale"
    DELIVERY_CONTENT_TOO_LONG = "delivery_content_too_long"
    INTERNAL = "internal"


class ErrorRecord(BaseModel):
    """Persistence-safe error metadata; it deliberately has no raw message field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    category: ErrorCategory
    retryable: bool
    diagnostic: str


class LifeAgentError(RuntimeError):
    """Base exception carrying a safe record rather than an arbitrary exception body."""

    def __init__(self, record: ErrorRecord):
        super().__init__(record.code.value)
        self.record = record


def transient_error(code: ErrorCode, diagnostic: str) -> LifeAgentError:
    """Build a retryable error from an allowlisted, non-sensitive diagnostic."""

    return LifeAgentError(
        ErrorRecord(
            code=code,
            category=ErrorCategory.TRANSIENT,
            retryable=True,
            diagnostic=diagnostic,
        )
    )


def authorization_error(diagnostic: str = "connector authorization is invalid") -> LifeAgentError:
    """Build a permanent-until-credentials-change authorization error."""

    return LifeAgentError(
        ErrorRecord(
            code=ErrorCode.AUTHORIZATION_INVALID,
            category=ErrorCategory.AUTHORIZATION,
            retryable=False,
            diagnostic=diagnostic,
        )
    )


def permanent_error(code: ErrorCode, diagnostic: str) -> LifeAgentError:
    """Build a deterministic non-retryable error."""

    return LifeAgentError(
        ErrorRecord(
            code=code,
            category=ErrorCategory.PERMANENT,
            retryable=False,
            diagnostic=diagnostic,
        )
    )


__all__ = [
    "ErrorCategory",
    "ErrorCode",
    "ErrorRecord",
    "LifeAgentError",
    "authorization_error",
    "permanent_error",
    "transient_error",
]
