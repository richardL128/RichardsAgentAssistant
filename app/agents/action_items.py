"""Shared canonical action-item and temporal contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


class ActionItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ActionItemDomain(StrEnum):
    ACADEMIC = "academic"
    CAREER = "career"
    PERSONAL = "personal"
    ADMINISTRATIVE = "administrative"
    PROJECT = "project"


class ActionItemKind(StrEnum):
    TASK = "task"
    ASSIGNMENT = "assignment"
    QUIZ = "quiz"
    EXAM = "exam"
    TUTORIAL = "tutorial"
    LAB = "lab"
    MEETING = "meeting"
    EVENT = "event"
    APPLICATION_FOLLOW_UP = "application_follow_up"
    INTERVIEW_PREP = "interview_prep"
    DEADLINE = "deadline"
    NEEDS_REVIEW = "needs_review"


class ActionItemStatus(StrEnum):
    INBOX = "inbox"
    NEEDS_REVIEW = "needs_review"
    TO_DO = "to_do"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    DONE = "done"
    CANCELED = "canceled"


class ActionItemSourceKind(StrEnum):
    NOTION_ACTION_ITEMS = "notion_action_items"
    NOTION_APPLICATIONS = "notion_applications"
    NOTION_INTERVIEWS = "notion_interviews"
    LEARN = "learn"
    GOOGLE_CALENDAR = "google_calendar"
    MANUAL = "manual"


class DateOnlyValue(ActionItemModel):
    precision: Literal["date"] = "date"
    start_date: date
    end_date_exclusive: date | None = None

    @model_validator(mode="after")
    def range_is_ordered(self) -> DateOnlyValue:
        if self.end_date_exclusive is not None and self.end_date_exclusive <= self.start_date:
            raise ValueError("end_date_exclusive must be after start_date")
        return self


class DateTimeValue(ActionItemModel):
    precision: Literal["datetime"] = "datetime"
    start_at: datetime
    end_at: datetime | None = None
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("start_at", "end_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime temporal values must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def range_is_ordered(self) -> DateTimeValue:
        if self.end_at is not None and self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


TemporalValue = Annotated[DateOnlyValue | DateTimeValue, Field(discriminator="precision")]


def parse_notion_temporal_value(
    start: date | datetime | str,
    end: date | datetime | str | None = None,
    time_zone: str | None = None,
    default_timezone: str = "America/Toronto",
) -> DateOnlyValue | DateTimeValue:
    """Parse a Notion date value into the canonical temporal contract.

    Date-only values stay calendar dates and never become instants. Notion's
    date-only range end is inclusive, so the canonical end is exclusive.
    Timed values become UTC instants while retaining the relevant IANA timezone.
    """

    timezone_name = time_zone or default_timezone
    timezone = _iana_timezone(timezone_name)
    parsed_start = _parse_notion_temporal_part(start)
    parsed_end = _parse_notion_temporal_part(end) if end is not None else None
    if isinstance(parsed_start, date) and not isinstance(parsed_start, datetime):
        if parsed_end is not None and not (
            isinstance(parsed_end, date) and not isinstance(parsed_end, datetime)
        ):
            raise ValueError("Notion date-only temporal values require date-only end")
        return DateOnlyValue(
            start_date=parsed_start,
            end_date_exclusive=(
                parsed_end + timedelta(days=1) if parsed_end is not None else None
            ),
        )
    if isinstance(parsed_end, date) and not isinstance(parsed_end, datetime):
        raise ValueError("Notion timed temporal values require timed end")
    if not isinstance(parsed_start, datetime):
        raise ValueError("Notion temporal start must be a date or datetime")
    return DateTimeValue(
        start_at=_notion_datetime_to_utc(parsed_start, timezone),
        end_at=_notion_datetime_to_utc(parsed_end, timezone) if parsed_end else None,
        timezone=timezone.key,
    )


def temporal_start_local_date(
    value: DateOnlyValue | DateTimeValue,
    default_timezone: str = "America/Toronto",
) -> date:
    if isinstance(value, DateOnlyValue):
        return value.start_date
    return value.start_at.astimezone(_iana_timezone(value.timezone or default_timezone)).date()


class ActionItemSource(ActionItemModel):
    kind: ActionItemSourceKind
    source_id: str | None = Field(default=None, min_length=1, max_length=255)
    source_label: str = Field(min_length=1, max_length=255)
    notion_database_id: str | None = Field(default=None, min_length=1, max_length=255)
    notion_data_source_id: str | None = Field(default=None, min_length=1, max_length=255)
    notion_page_id: str | None = Field(default=None, min_length=1, max_length=255)
    property_ids: dict[str, str] = Field(default_factory=dict)


class ActionItemContext(ActionItemModel):
    course_id: str | None = Field(default=None, min_length=1, max_length=255)
    course_code: str | None = Field(default=None, min_length=1, max_length=80)
    application_id: str | None = Field(default=None, min_length=1, max_length=255)
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    interview_id: str | None = Field(default=None, min_length=1, max_length=255)
    context_label: str | None = Field(default=None, min_length=1, max_length=500)


class CanonicalActionItem(ActionItemModel):
    item_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    domain: ActionItemDomain
    item_kind: ActionItemKind
    status: ActionItemStatus
    temporal: TemporalValue | None = None
    source: ActionItemSource
    context: ActionItemContext = Field(default_factory=ActionItemContext)
    freshness_as_of: datetime | None = None
    edit_version: datetime | None = None

    @field_validator("freshness_as_of", "edit_version")
    @classmethod
    def metadata_times_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("action item timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @computed_field(return_type=bool)
    @property
    def completed(self) -> bool:
        return self.status is ActionItemStatus.DONE


__all__ = [
    "ActionItemContext",
    "ActionItemDomain",
    "ActionItemKind",
    "ActionItemSource",
    "ActionItemSourceKind",
    "ActionItemStatus",
    "CanonicalActionItem",
    "DateOnlyValue",
    "DateTimeValue",
    "TemporalValue",
    "parse_notion_temporal_value",
    "temporal_start_local_date",
]


def _iana_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be an IANA timezone") from exc


def _parse_notion_temporal_part(value: date | datetime | str) -> date | datetime:
    if isinstance(value, date | datetime):
        return value
    raw = value.strip()
    if "T" not in raw:
        return date.fromisoformat(raw)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _notion_datetime_to_utc(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    _raise_for_invalid_local_wall_time(value, timezone)
    return value.replace(tzinfo=timezone).astimezone(UTC)


def _raise_for_invalid_local_wall_time(value: datetime, timezone: ZoneInfo) -> None:
    valid_offsets: set[object] = set()
    for fold in (0, 1):
        candidate = value.replace(tzinfo=timezone, fold=fold)
        round_tripped = candidate.astimezone(UTC).astimezone(timezone)
        if round_tripped.replace(tzinfo=None) == value:
            valid_offsets.add(candidate.utcoffset())
    if not valid_offsets:
        raise ValueError("local wall time does not exist in timezone")
    if len(valid_offsets) > 1:
        raise ValueError("local wall time is ambiguous in timezone")
