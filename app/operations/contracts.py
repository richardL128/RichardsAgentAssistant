"""Stable, deliberately narrow contracts for the operations console."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

ConsoleState = Literal["healthy", "attention", "failed"]


class HealthCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: str
    label: str
    state: ConsoleState
    last_success_at: datetime | None
    next_expected_at: datetime | None
    diagnostic: str
    activity_url: str


class ExternalLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    url: HttpUrl


class ActivityItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    timestamp: datetime
    agent: str
    status: str
    severity: ConsoleState
    summary: str
    delivery_links: tuple[ExternalLink, ...] = ()
    evidence_links: tuple[ExternalLink, ...] = ()
    unresolved: str | None = None
    acknowledged: bool = False


class ActivityPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ActivityItem, ...]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0)
    pages: int = Field(ge=0)


class TimelineStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    attempt: int
    status: str
    started_at: datetime | None
    ended_at: datetime | None
    diagnostic: str | None


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str
    status: str
    sent_at: datetime | None
    link: ExternalLink | None
    error_code: str | None


class EvidenceLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    title: str
    url: HttpUrl
    published_at: datetime | None
    classification: str


class ActivityDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item: ActivityItem
    timeline: tuple[TimelineStep, ...]
    deliveries: tuple[DeliveryReceipt, ...]
    evidence: tuple[EvidenceLink, ...]
    warnings: tuple[str, ...]
    raw_record: dict[str, Any]


class AcknowledgementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    alert_key: str = Field(default="run", min_length=1, max_length=255, pattern=r"^[\w.-]+$")


class AcknowledgementResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    acknowledgement_id: UUID
    run_id: UUID
    alert_key: str
    acknowledged_at: datetime


class ApprovedSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slot: int = Field(ge=1, le=8)
    name: str
    hostname: str
    entitlement: str
    enabled: bool


class SourceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowlist_version: str | None
    schedule_enabled: bool
    approval_complete: bool
    sources: tuple[ApprovedSource, ...]
    diagnostic: str

    @model_validator(mode="after")
    def schedule_requires_approval(self) -> SourceSettings:
        if self.schedule_enabled and not self.approval_complete:
            raise ValueError("finance schedule cannot be enabled before source approval")
        return self


__all__ = [
    "AcknowledgementCreate",
    "AcknowledgementResult",
    "ActivityDetail",
    "ActivityItem",
    "ActivityPage",
    "ApprovedSource",
    "ConsoleState",
    "DeliveryReceipt",
    "EvidenceLink",
    "ExternalLink",
    "HealthCard",
    "SourceSettings",
    "TimelineStep",
]
