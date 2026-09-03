"""Deterministic operational-health evaluation from durable record facts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.health.checks import HealthState


class ProcessingStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DeliveryStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    INTENT = "intent"
    UNCERTAIN = "uncertain"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OperationalFacts(BaseModel):
    """The small durable-record projection used to calculate health."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str = Field(min_length=1, max_length=80)
    processing: ProcessingStatus
    delivery: DeliveryStatus
    connector_authenticated: bool = True
    evaluated_at: datetime
    last_success_at: datetime | None = None
    next_expected_at: datetime | None = None
    retry_attempt: int = Field(default=0, ge=0)
    retry_limit: int = Field(default=0, ge=0)
    diagnostic_code: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def timestamps_are_aware(self) -> OperationalFacts:
        for name in ("evaluated_at", "last_success_at", "next_expected_at"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        return self


class OperationalHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str
    state: HealthState
    rule: str
    diagnostic: str
    evaluated_at: datetime
    last_success_at: datetime | None
    next_expected_at: datetime | None


def evaluate_operational_health(facts: OperationalFacts) -> OperationalHealth:
    """Apply ordered, model-free rules to durable processing and delivery facts."""

    now = facts.evaluated_at.astimezone(UTC)
    due = facts.next_expected_at.astimezone(UTC) if facts.next_expected_at else None
    if not facts.connector_authenticated:
        state, rule = HealthState.FAILED, "connector_unauthenticated"
    elif facts.processing is ProcessingStatus.FAILED:
        state, rule = HealthState.FAILED, "required_processing_failed"
    elif facts.delivery is DeliveryStatus.FAILED:
        state, rule = HealthState.FAILED, "required_delivery_failed"
    elif due is not None and now > due and facts.processing is not ProcessingStatus.SUCCEEDED:
        state, rule = HealthState.ATTENTION, "run_overdue"
    elif facts.processing is ProcessingStatus.WAITING_RETRY:
        state, rule = HealthState.ATTENTION, "waiting_for_retry"
    elif facts.processing is ProcessingStatus.WAITING_APPROVAL:
        state, rule = HealthState.ATTENTION, "waiting_for_approval"
    elif facts.delivery in {DeliveryStatus.INTENT, DeliveryStatus.UNCERTAIN}:
        state, rule = HealthState.ATTENTION, "delivery_incomplete"
    elif facts.processing in {ProcessingStatus.QUEUED, ProcessingStatus.RUNNING}:
        state, rule = HealthState.ATTENTION, "processing_incomplete"
    elif facts.processing is ProcessingStatus.SUCCEEDED and facts.delivery in {
        DeliveryStatus.NOT_REQUIRED,
        DeliveryStatus.SUCCEEDED,
    }:
        state, rule = HealthState.HEALTHY, "processing_and_delivery_succeeded"
    else:
        state, rule = HealthState.FAILED, "inconsistent_record_state"

    retry = f"; retry {facts.retry_attempt} of {facts.retry_limit}" if facts.retry_limit > 0 else ""
    return OperationalHealth(
        component=facts.component,
        state=state,
        rule=rule,
        diagnostic=f"{facts.diagnostic_code}{retry}",
        evaluated_at=now,
        last_success_at=facts.last_success_at,
        next_expected_at=facts.next_expected_at,
    )


__all__ = [
    "DeliveryStatus",
    "OperationalFacts",
    "OperationalHealth",
    "ProcessingStatus",
    "evaluate_operational_health",
]
