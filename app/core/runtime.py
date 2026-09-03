"""Typed identity propagated through every durable workflow step."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentKind(StrEnum):
    CODE_REVIEW = "code_review"
    ACADEMIC_PLANNER = "academic_planner"
    FINANCE = "finance"
    SHARED = "shared"


class TriggerKind(StrEnum):
    SCHEDULE = "schedule"
    WEBHOOK = "webhook"
    APPROVAL_RESUME = "approval_resume"
    TEST = "test"


class RunContext(BaseModel):
    """Non-secret, stable metadata required to reconstruct a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    request_id: UUID
    agent: AgentKind
    trigger: TriggerKind
    idempotency_key: str = Field(min_length=3, max_length=512, pattern=r"^[a-z0-9][a-z0-9:._/-]+$")
    model_identifier: str | None = Field(default=None, min_length=1, max_length=240)
    model_digest: str | None = Field(default=None, min_length=1, max_length=128)
    config_version: str = Field(min_length=1, max_length=128)
    input_version: str = Field(min_length=1, max_length=128)
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        return value.astimezone(UTC)

    @classmethod
    def new(
        cls,
        *,
        agent: AgentKind,
        trigger: TriggerKind,
        idempotency_key: str,
        config_version: str,
        input_version: str,
        model_identifier: str | None = None,
        model_digest: str | None = None,
    ) -> Self:
        return cls(
            run_id=uuid4(),
            request_id=uuid4(),
            agent=agent,
            trigger=trigger,
            idempotency_key=idempotency_key,
            model_identifier=model_identifier,
            model_digest=model_digest,
            config_version=config_version,
            input_version=input_version,
            started_at=datetime.now(UTC),
        )


__all__ = ["AgentKind", "RunContext", "TriggerKind"]
