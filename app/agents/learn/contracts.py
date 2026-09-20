"""Strict LEARN contracts shared by the bridge, semantics, and read-only tools."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LearnModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class LearnDatePrecision(StrEnum):
    DATE = "date"
    DATETIME = "datetime"


class LearnSemanticStatus(StrEnum):
    VALID = "valid"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    OVERSIZE = "oversize"


class LearnCourse(LearnModel):
    """One user-visible active or recently visible LEARN course."""

    org_unit_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=300)
    term: str | None = Field(default=None, max_length=128)
    active: bool = True
    url: str = Field(min_length=1, max_length=2_000)


class LearnScheduledItem(LearnModel):
    """One bounded dated LEARN item surfaced by the authenticated browser bridge."""

    source_id: str = Field(min_length=1, max_length=255)
    course_org_unit_id: str = Field(min_length=1, max_length=128)
    course_code: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=500)
    start_at: datetime | date | None = None
    due_at: datetime | date | None = None
    end_at: datetime | date | None = None
    date_precision: LearnDatePrecision
    completed: bool = False
    url: str | None = Field(default=None, max_length=2_000)
    fingerprint: str = Field(min_length=16, max_length=128)

    @field_validator("start_at", "due_at", "end_at")
    @classmethod
    def datetimes_are_aware(cls, value: datetime | date | None) -> datetime | date | None:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("LEARN scheduled datetimes must be timezone-aware")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def has_a_date(self) -> LearnScheduledItem:
        if self.start_at is None and self.due_at is None and self.end_at is None:
            raise ValueError("LEARN scheduled items require at least one date value")
        return self


class LearnAnnouncementEvidence(LearnModel):
    """Bounded announcement evidence; body fragments are runtime-only untrusted text."""

    source_id: str = Field(min_length=1, max_length=255)
    course_org_unit_id: str = Field(min_length=1, max_length=128)
    course_code: str = Field(min_length=1, max_length=80)
    published_at: datetime
    updated_at: datetime | None = None
    body_fragments: tuple[str, ...] = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2_000)
    fingerprint: str = Field(min_length=16, max_length=128)
    attachments_present: bool = False
    oversized: bool = False

    @field_validator("published_at", "updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("LEARN announcement timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("body_fragments")
    @classmethod
    def fragments_are_bounded(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(" ".join(fragment.split()) for fragment in value if fragment.strip())
        if not normalized:
            raise ValueError("LEARN announcement evidence requires text fragments")
        if any(len(fragment) > 4_000 for fragment in normalized):
            raise ValueError("LEARN announcement fragments must be bounded")
        return normalized

    @property
    def effective_at(self) -> datetime:
        if self.updated_at is not None and self.updated_at > self.published_at:
            return self.updated_at
        return self.published_at


class LearnActionItem(LearnModel):
    """One grounded action item extracted from an announcement."""

    text: str = Field(min_length=1, max_length=500)
    evidence_fragment_ids: tuple[str, ...] = Field(min_length=1, max_length=12)


class LearnDatedImplication(LearnModel):
    """One model-grounded academic date implied by an announcement."""

    course_code: str = Field(min_length=1, max_length=80)
    activity_type: str = Field(min_length=1, max_length=120)
    date_value: datetime | date
    date_precision: LearnDatePrecision
    end_value: datetime | date | None = None
    evidence_fragment_ids: tuple[str, ...] = Field(min_length=1, max_length=12)

    @field_validator("date_value", "end_value")
    @classmethod
    def implication_datetimes_are_aware(
        cls, value: datetime | date | None
    ) -> datetime | date | None:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("LEARN implication datetimes must be timezone-aware")
            return value.astimezone(UTC)
        return value


class LearnAnnouncementSemanticResult(LearnModel):
    """Model-proposed LEARN announcement semantics after schema validation."""

    source_id: str = Field(min_length=1, max_length=255)
    course_code: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=700)
    why_it_matters: str = Field(min_length=1, max_length=700)
    action_items: tuple[LearnActionItem, ...] = Field(default=(), max_length=10)
    dated_implications: tuple[LearnDatedImplication, ...] = Field(default=(), max_length=20)
    evidence_fragment_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    source_url: str = Field(min_length=1, max_length=2_000)
    prompt_version: str = Field(min_length=1, max_length=80)
    model_identity: str | None = Field(default=None, max_length=128)

    @field_validator("evidence_fragment_ids")
    @classmethod
    def citations_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("LEARN semantic citations must be unique")
        return value


class LearnAnnouncementSemanticOutcome(LearnModel):
    """Host-reviewed announcement semantics, with no raw announcement body."""

    status: LearnSemanticStatus
    source_id: str = Field(min_length=1, max_length=255)
    course_code: str = Field(min_length=1, max_length=80)
    source_url: str = Field(min_length=1, max_length=2_000)
    fingerprint: str = Field(min_length=16, max_length=128)
    result: LearnAnnouncementSemanticResult | None = None
    prompt_version: str
    critic_version: str
    model_identity: str | None = Field(default=None, max_length=128)
    config_version: str | None = Field(default=None, max_length=128)
    error_code: str | None = Field(default=None, max_length=80)
    reason: str | None = Field(default=None, max_length=500)

    @property
    def safe_summary(self) -> str:
        return self.result.summary if self.result is not None else "summary unavailable"

    def tool_payload(self) -> dict[str, object]:
        """Return a model/tool-safe payload that never includes source body text."""

        if self.result is None:
            return {
                "status": self.status.value,
                "course_code": self.course_code,
                "summary": "summary unavailable",
                "why_it_matters": None,
                "action_items": [],
                "dated_implications": [],
                "source_url": self.source_url,
                "error_code": self.error_code,
            }
        return {
            "status": self.status.value,
            "course_code": self.course_code,
            "summary": self.result.summary,
            "why_it_matters": self.result.why_it_matters,
            "action_items": [
                {
                    "text": item.text,
                    "evidence_fragment_ids": list(item.evidence_fragment_ids),
                }
                for item in self.result.action_items
            ],
            "dated_implications": [
                {
                    "course_code": item.course_code,
                    "activity_type": item.activity_type,
                    "date_value": item.date_value.isoformat(),
                    "date_precision": item.date_precision.value,
                    "end_value": item.end_value.isoformat() if item.end_value is not None else None,
                    "evidence_fragment_ids": list(item.evidence_fragment_ids),
                }
                for item in self.result.dated_implications
            ],
            "source_url": self.source_url,
            "prompt_version": self.result.prompt_version,
            "model_identity": self.result.model_identity,
        }


def fingerprint_learn_payload(payload: object) -> str:
    """Return a deterministic fingerprint for normalized LEARN bridge payloads."""

    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
