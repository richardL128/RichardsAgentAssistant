"""Strict contracts for model-reasoned calendar event briefing semantics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CalendarBriefingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CalendarEventSourceArea(StrEnum):
    COURSE = "course"
    JOBS = "jobs"


class CalendarEventSourceKind(StrEnum):
    PROPERTY = "property"
    PAGE_BODY_BLOCK = "page_body_block"


class CalendarEventSemanticStatus(StrEnum):
    VALID = "valid"
    NOT_SUBSTANTIVE = "not_substantive"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class CalendarEventEvidenceFragment(CalendarBriefingModel):
    """One bounded user-authored fragment local to a calendar event."""

    fragment_id: str = Field(min_length=1, max_length=255)
    event_id: str = Field(min_length=1, max_length=255)
    source_kind: CalendarEventSourceKind
    source_label: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=4_000)
    ordinal: int = Field(ge=0, le=10_000)


class CalendarEventSemanticInput(CalendarBriefingModel):
    """Host-approved model input for one calendar event."""

    event_id: str = Field(min_length=1, max_length=255)
    source_area: CalendarEventSourceArea
    source_label: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    event_kind: str = Field(min_length=1, max_length=128)
    local_date_label: str = Field(min_length=1, max_length=128)
    local_time_label: str | None = Field(default=None, min_length=1, max_length=128)
    is_all_day: bool = False
    source_fingerprint: str = Field(min_length=8, max_length=128)
    source_last_edited_at: datetime | None = None
    evidence_fragments: tuple[CalendarEventEvidenceFragment, ...] = Field(
        default=(),
        max_length=40,
    )

    @field_validator("source_last_edited_at")
    @classmethod
    def source_last_edited_at_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source edit version must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def fragments_belong_to_event(self) -> CalendarEventSemanticInput:
        fragment_ids: set[str] = set()
        ordinals: set[int] = set()
        for fragment in self.evidence_fragments:
            if fragment.event_id != self.event_id:
                raise ValueError("calendar evidence fragment belongs to another event")
            if fragment.fragment_id in fragment_ids:
                raise ValueError("calendar evidence fragment ids must be unique")
            if fragment.ordinal in ordinals:
                raise ValueError("calendar evidence fragment ordinals must be unique")
            fragment_ids.add(fragment.fragment_id)
            ordinals.add(fragment.ordinal)
        return self


class CalendarEventSemanticResult(CalendarBriefingModel):
    """Model-proposed event semantics after schema validation."""

    event_id: str = Field(min_length=1, max_length=255)
    overview: str = Field(min_length=1, max_length=700)
    description_present: bool
    description: str | None = Field(default=None, min_length=1, max_length=1_500)
    evidence_fragment_ids: tuple[str, ...] = Field(min_length=1, max_length=12)
    description_fragment_ids: tuple[str, ...] = Field(default=(), max_length=12)
    classification_rationale: str | None = Field(default=None, max_length=500)

    @field_validator("evidence_fragment_ids", "description_fragment_ids")
    @classmethod
    def cited_fragment_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("calendar semantic citations must be unique")
        return value

    @model_validator(mode="after")
    def description_fields_match_decision(self) -> CalendarEventSemanticResult:
        if self.description_present:
            if not self.description:
                raise ValueError("description_present requires description text")
            if not self.description_fragment_ids:
                raise ValueError("description_present requires description citations")
        else:
            if self.description is not None:
                raise ValueError("no-description results must not include description text")
            if self.description_fragment_ids:
                raise ValueError("no-description results must not include description citations")
        return self


class ScheduledMorningCalendarItem(CalendarBriefingModel):
    """Host-approved rendering facts for one scheduled morning calendar item."""

    event_id: str = Field(min_length=1, max_length=255)
    source_area: CalendarEventSourceArea
    source_label: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    display_kind: str = Field(min_length=1, max_length=128)
    local_start_label: str = Field(min_length=1, max_length=128)
    local_end_label: str | None = Field(default=None, min_length=1, max_length=128)
    relative_date_label: str = Field(min_length=1, max_length=128)
    is_all_day: bool = False
    completed: bool = False
    semantic_status: CalendarEventSemanticStatus = CalendarEventSemanticStatus.UNAVAILABLE
    semantic_overview: str | None = Field(default=None, min_length=1, max_length=700)
    semantic_description: str | None = Field(default=None, min_length=1, max_length=1_500)
    semantic_evidence_fragment_ids: tuple[str, ...] = Field(default=(), max_length=12)
    semantic_description_fragment_ids: tuple[str, ...] = Field(default=(), max_length=12)
    source_url: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def semantic_status_matches_payload(self) -> ScheduledMorningCalendarItem:
        if self.semantic_status in {
            CalendarEventSemanticStatus.UNAVAILABLE,
            CalendarEventSemanticStatus.INVALID,
        } and (self.semantic_overview is not None or self.semantic_description is not None):
            raise ValueError("unavailable or invalid calendar semantics cannot carry prose")
        if self.semantic_status == CalendarEventSemanticStatus.NOT_SUBSTANTIVE:
            if self.semantic_overview is None:
                raise ValueError("not_substantive calendar semantics require an overview")
            if self.semantic_description is not None or self.semantic_description_fragment_ids:
                raise ValueError("not_substantive calendar semantics cannot carry a description")
        if self.semantic_status == CalendarEventSemanticStatus.VALID and (
            self.semantic_overview is None or self.semantic_description is None
        ):
            raise ValueError("valid calendar semantics require overview and description")
        return self


def fingerprint_event_evidence(
    fragments: Sequence[CalendarEventEvidenceFragment],
    *,
    event_id: str,
) -> str:
    """Return a stable fingerprint for the exact event-local evidence supplied to Qwen."""

    for fragment in fragments:
        if fragment.event_id != event_id:
            raise ValueError("calendar evidence fingerprint cannot include another event")

    payload = [
        {
            "fragment_id": fragment.fragment_id,
            "event_id": fragment.event_id,
            "source_kind": fragment.source_kind.value,
            "source_label": fragment.source_label,
            "text": fragment.text,
            "ordinal": fragment.ordinal,
        }
        for fragment in sorted(fragments, key=lambda item: (item.ordinal, item.fragment_id))
    ]
    digest = hashlib.sha256(
        json.dumps(
            {"event_id": event_id, "fragments": payload},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


__all__ = [
    "CalendarEventEvidenceFragment",
    "CalendarEventSemanticInput",
    "CalendarEventSemanticResult",
    "CalendarEventSemanticStatus",
    "CalendarEventSourceArea",
    "CalendarEventSourceKind",
    "ScheduledMorningCalendarItem",
    "fingerprint_event_evidence",
]
