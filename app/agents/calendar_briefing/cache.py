"""Cache matching boundaries for calendar event semantic results."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.calendar_briefing.contracts import (
    CalendarEventSemanticInput,
    CalendarEventSemanticResult,
    CalendarEventSemanticStatus,
)
from app.agents.calendar_briefing.semantic_interpreter import CALENDAR_SEMANTIC_PROMPT_VERSION


class CalendarSemanticCacheModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CalendarSemanticCacheRecord(CalendarSemanticCacheModel):
    """Persisted semantic metadata sufficient to decide exact cache reuse."""

    event_id: str = Field(min_length=1, max_length=255)
    semantic_status: CalendarEventSemanticStatus
    source_fingerprint: str = Field(min_length=8, max_length=128)
    source_last_edited_at: datetime | None = None
    model_identity: str = Field(min_length=1, max_length=128)
    config_version: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=128)
    result: CalendarEventSemanticResult | None = None

    @field_validator("source_last_edited_at")
    @classmethod
    def source_last_edited_at_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cache source edit version must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def status_matches_result(self) -> CalendarSemanticCacheRecord:
        if self.semantic_status in {
            CalendarEventSemanticStatus.VALID,
            CalendarEventSemanticStatus.NOT_SUBSTANTIVE,
        }:
            if self.result is None:
                raise ValueError("reusable calendar semantic cache records require a result")
            if self.result.event_id != self.event_id:
                raise ValueError("calendar semantic cache result belongs to another event")
            if self.semantic_status == CalendarEventSemanticStatus.VALID:
                if not self.result.description_present:
                    raise ValueError("valid cache records require a description result")
            elif self.result.description_present:
                raise ValueError("not_substantive cache records cannot carry a description result")
        elif self.result is not None:
            raise ValueError("failed calendar semantic cache records cannot carry a result")
        return self


class CalendarSemanticCacheDecision(CalendarSemanticCacheModel):
    reusable: bool
    reason: str = Field(min_length=1, max_length=120)


def decide_calendar_semantic_cache_reuse(
    event: CalendarEventSemanticInput,
    cached: CalendarSemanticCacheRecord | None,
    *,
    model_identity: str,
    config_version: str,
    prompt_version: str = CALENDAR_SEMANTIC_PROMPT_VERSION,
) -> CalendarSemanticCacheDecision:
    """Return whether cached semantics exactly match the current source/model boundary."""

    if cached is None:
        return CalendarSemanticCacheDecision(reusable=False, reason="missing")
    if cached.event_id != event.event_id:
        return CalendarSemanticCacheDecision(reusable=False, reason="event_id_mismatch")
    if cached.semantic_status not in {
        CalendarEventSemanticStatus.VALID,
        CalendarEventSemanticStatus.NOT_SUBSTANTIVE,
    }:
        return CalendarSemanticCacheDecision(reusable=False, reason="status_not_reusable")
    if cached.source_fingerprint != event.source_fingerprint:
        return CalendarSemanticCacheDecision(reusable=False, reason="source_fingerprint_mismatch")
    if cached.source_last_edited_at != event.source_last_edited_at:
        return CalendarSemanticCacheDecision(reusable=False, reason="source_edit_version_mismatch")
    if cached.model_identity != model_identity:
        return CalendarSemanticCacheDecision(reusable=False, reason="model_identity_mismatch")
    if cached.config_version != config_version:
        return CalendarSemanticCacheDecision(reusable=False, reason="config_version_mismatch")
    if cached.prompt_version != prompt_version:
        return CalendarSemanticCacheDecision(reusable=False, reason="prompt_version_mismatch")
    return CalendarSemanticCacheDecision(reusable=True, reason="exact_match")


__all__ = [
    "CalendarSemanticCacheDecision",
    "CalendarSemanticCacheRecord",
    "decide_calendar_semantic_cache_reuse",
]
