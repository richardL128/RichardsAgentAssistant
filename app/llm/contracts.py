"""Stable contracts shared by the model gateway and evaluation harness."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InvocationStatus(StrEnum):
    VALID = "valid"
    INVALID_OUTPUT = "invalid_output"
    FAILED = "failed"


class ModelCallTelemetry(BaseModel):
    """Non-secret metadata for one physical model attempt."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    attempt: int = Field(ge=1, le=2)
    model_identity: str
    config_version: str
    started_at: datetime
    finished_at: datetime
    model_started_at: datetime
    model_finished_at: datetime
    latency_ms: float = Field(ge=0)
    queue_wait_ms: float = Field(ge=0)
    model_latency_ms: float = Field(ge=0)
    input_characters: int = Field(ge=0)
    output_characters: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)
    reported_input_tokens: int | None = Field(default=None, ge=0)
    reported_output_tokens: int | None = Field(default=None, ge=0)
    output_valid: bool
    error_code: str | None = None


class InvocationResult[ResponseT: BaseModel](BaseModel):
    """Result of one logical invocation, including its bounded repair attempt."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    request_id: UUID
    status: InvocationStatus
    output: ResponseT | None = None
    raw_text: str = Field(default="", repr=False)
    telemetry: list[ModelCallTelemetry] = Field(default_factory=lambda: list[ModelCallTelemetry]())
    error_code: str | None = None
    error_diagnostic: str | None = None
