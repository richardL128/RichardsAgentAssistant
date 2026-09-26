"""Host-owned contracts for deterministic, bounded model-facing reads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import TypeVar, cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

DEFAULT_UPCOMING_DAYS = 14
MAX_DATE_RANGE_DAYS = 31
MAX_QUERY_PAGE_SIZE = 20
MODEL_TOOL_RESULT_MAX_CHARS = 4_096


class QueryContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TemporalScope(StrEnum):
    TODAY = "today"
    TOMORROW = "tomorrow"
    THIS_WEEK = "this_week"
    NEXT_WEEK = "next_week"
    THIS_MONTH = "this_month"
    NEXT_MONTH = "next_month"
    UPCOMING = "upcoming"
    OVERDUE = "overdue"
    DATE_RANGE = "date_range"
    ALL = "all"


class CompletionMode(StrEnum):
    INCOMPLETE = "incomplete"
    COMPLETED = "completed"
    ALL = "all"


class FreshnessState(StrEnum):
    FRESH_COMPLETE = "fresh_complete"
    FRESH_PARTIAL_FOR_UNREQUESTED_SOURCES = "fresh_partial_for_unrequested_sources"
    CACHED_STALE = "cached_stale"
    UNAVAILABLE = "unavailable"


class CompletenessState(StrEnum):
    COMPLETE = "complete"
    MORE_AVAILABLE = "more_available"
    PARTIAL = "partial"
    CACHED_STALE = "cached_stale"
    UNAVAILABLE = "unavailable"


class QueryResultKind(StrEnum):
    """Host-owned capability carried by a trusted query envelope."""

    UNKNOWN = "unknown"
    CALENDAR_ITEMS = "calendar_items"
    COURSE_SOURCES = "course_sources"
    JOBS = "jobs"
    LEARN_CONTENT = "learn_content"


class TemporalQuery(QueryContractModel):
    """Model-selected intent; the host resolves it against the immutable request time."""

    # The model names a period and never does calendar arithmetic: named scopes cover common
    # relative periods, and date_range takes inclusive owner-local calendar dates.

    scope: TemporalScope = Field(
        default=TemporalScope.ALL,
        description=(
            "Owner-local period. Weeks run Monday-Sunday; months are whole calendar months."
        ),
    )
    start_date: date | None = Field(
        default=None,
        description="date_range only: first local day, inclusive.",
    )
    end_date: date | None = Field(
        default=None,
        description="date_range only: last local day, inclusive.",
    )
    # Legacy aware-instant date_range form: still accepted, hidden from the model schema.
    start_at: SkipJsonSchema[datetime | None] = None
    end_at: SkipJsonSchema[datetime | None] = None
    upcoming_days: int = Field(
        default=DEFAULT_UPCOMING_DAYS,
        ge=1,
        le=MAX_DATE_RANGE_DAYS,
        description="upcoming only: days ahead.",
    )

    @field_validator("start_at", "end_at")
    @classmethod
    def range_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("date_range timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_scope_fields(self) -> TemporalQuery:
        has_dates = self.start_date is not None or self.end_date is not None
        has_instants = self.start_at is not None or self.end_at is not None
        if self.scope is TemporalScope.DATE_RANGE:
            if has_dates and has_instants:
                raise ValueError(
                    "date_range takes either start_date/end_date or start_at/end_at, not both"
                )
            if has_instants:
                if self.start_at is None or self.end_at is None:
                    raise ValueError("date_range requires start_at and end_at together")
                if self.end_at <= self.start_at:
                    raise ValueError("date_range end_at must be after start_at")
                if self.end_at - self.start_at > timedelta(days=MAX_DATE_RANGE_DAYS):
                    raise ValueError(f"date_range may not exceed {MAX_DATE_RANGE_DAYS} days")
            else:
                if self.start_date is None or self.end_date is None:
                    raise ValueError(
                        "date_range requires start_date and end_date (inclusive YYYY-MM-DD)"
                    )
                if self.end_date < self.start_date:
                    raise ValueError("date_range end_date must be on or after start_date")
                if (self.end_date - self.start_date).days >= MAX_DATE_RANGE_DAYS:
                    raise ValueError(f"date_range may not exceed {MAX_DATE_RANGE_DAYS} days")
        elif has_dates or has_instants:
            raise ValueError(
                "start_date, end_date, start_at and end_at are valid only for date_range"
            )
        if self.scope is not TemporalScope.UPCOMING and self.upcoming_days != DEFAULT_UPCOMING_DAYS:
            raise ValueError("upcoming_days is valid only for upcoming")
        return self


class ResolvedTemporalWindow(QueryContractModel):
    scope: TemporalScope
    start_at: datetime | None = None
    end_at: datetime | None = None
    start_local_date: date | None = None
    end_local_date_exclusive: date | None = None

    @field_validator("start_at", "end_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("resolved timestamps must be timezone-aware")
        return value


class NormalizedQueryFilters(QueryContractModel):
    temporal: ResolvedTemporalWindow
    completion: CompletionMode = CompletionMode.INCOMPLETE
    view: str | None = Field(default=None, max_length=32)
    text: str = Field(default="", max_length=300)
    roles: tuple[str, ...] = Field(default=(), max_length=10)
    source_ids: tuple[str, ...] = Field(default=(), max_length=50)
    limit: int = Field(default=10, ge=1, le=MAX_QUERY_PAGE_SIZE)

    @field_validator("roles", "source_ids")
    @classmethod
    def values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("filter values must be unique")
        return value


class SourceFreshness(QueryContractModel):
    source_id: str = Field(min_length=1, max_length=255)
    state: FreshnessState
    as_of: datetime | None = None
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=10)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("freshness as_of must be timezone-aware")
        return value


def resolve_query_completeness(
    *,
    freshness: Sequence[SourceFreshness],
    has_more: bool,
) -> CompletenessState:
    """Resolve query completeness with source freshness taking precedence."""

    freshness_states = tuple(item.state for item in freshness)
    if freshness_states and all(state is FreshnessState.UNAVAILABLE for state in freshness_states):
        return CompletenessState.UNAVAILABLE
    if any(state is FreshnessState.UNAVAILABLE for state in freshness_states):
        return CompletenessState.PARTIAL
    if any(state is FreshnessState.CACHED_STALE for state in freshness_states):
        return CompletenessState.CACHED_STALE
    return CompletenessState.MORE_AVAILABLE if has_more else CompletenessState.COMPLETE


ItemT = TypeVar("ItemT")


class QueryEnvelope[ItemT](QueryContractModel):
    query_id: str = Field(min_length=1, max_length=128)
    as_of: datetime
    timezone: str = Field(min_length=1, max_length=64)
    result_kind: QueryResultKind = QueryResultKind.UNKNOWN
    applied_filters: NormalizedQueryFilters
    freshness: tuple[SourceFreshness, ...] = Field(min_length=1, max_length=50)
    items: tuple[ItemT, ...] = Field(default=(), max_length=MAX_QUERY_PAGE_SIZE)
    result_count: int = Field(ge=0, le=MAX_QUERY_PAGE_SIZE)
    has_more: bool
    next_cursor: str | None = Field(default=None, max_length=2_000)
    completeness: CompletenessState

    @field_validator("as_of")
    @classmethod
    def envelope_as_of_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("query as_of must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_page(self) -> QueryEnvelope[ItemT]:
        if self.result_count != len(self.items):
            raise ValueError("result_count must equal the number of items")
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("has_more and next_cursor must agree")
        expected = resolve_query_completeness(
            freshness=self.freshness,
            has_more=self.has_more,
        )
        if self.completeness is not expected:
            raise ValueError("completeness does not match pagination and freshness")
        return self


def resolve_temporal_window(
    query: TemporalQuery,
    *,
    request_time: datetime,
    timezone: ZoneInfo | str,
) -> ResolvedTemporalWindow:
    """Resolve relative intent once, using the original trusted request timestamp.

    Calendar-day bounds are always computed from owner-local dates and never by truncating
    instants, so date-only items on a range's last day are included.
    """

    if request_time.tzinfo is None or request_time.utcoffset() is None:
        raise ValueError("request_time must be timezone-aware")
    zone = _zone(timezone)
    local_now = request_time.astimezone(zone)
    today = local_now.date()

    if query.scope is TemporalScope.ALL:
        return ResolvedTemporalWindow(scope=query.scope)
    if query.scope is TemporalScope.OVERDUE:
        return ResolvedTemporalWindow(
            scope=query.scope,
            end_at=local_now,
            end_local_date_exclusive=today,
        )
    if query.scope is TemporalScope.UPCOMING:
        end_date = today + timedelta(days=query.upcoming_days + 1)
        return ResolvedTemporalWindow(
            scope=query.scope,
            start_at=local_now,
            end_at=_local_midnight(end_date, zone),
            start_local_date=today,
            end_local_date_exclusive=end_date,
        )
    if query.scope is TemporalScope.DATE_RANGE and query.start_at is not None:
        assert query.end_at is not None
        start = query.start_at.astimezone(zone)
        end = query.end_at.astimezone(zone)
        # A local-midnight end is an exclusive boundary; any later end time includes that day.
        end_date = end.date() if end.time() == time.min else end.date() + timedelta(days=1)
        return ResolvedTemporalWindow(
            scope=query.scope,
            start_at=start,
            end_at=end,
            start_local_date=start.date(),
            end_local_date_exclusive=end_date,
        )

    start_date: date
    end_date_exclusive: date
    if query.scope is TemporalScope.TODAY:
        start_date, end_date_exclusive = today, today + timedelta(days=1)
    elif query.scope is TemporalScope.TOMORROW:
        start_date, end_date_exclusive = today + timedelta(days=1), today + timedelta(days=2)
    elif query.scope is TemporalScope.THIS_WEEK:
        start_date = today - timedelta(days=today.weekday())
        end_date_exclusive = start_date + timedelta(days=7)
    elif query.scope is TemporalScope.NEXT_WEEK:
        start_date = today - timedelta(days=today.weekday()) + timedelta(days=7)
        end_date_exclusive = start_date + timedelta(days=7)
    elif query.scope is TemporalScope.THIS_MONTH:
        start_date = today.replace(day=1)
        end_date_exclusive = _first_of_next_month(start_date)
    elif query.scope is TemporalScope.NEXT_MONTH:
        start_date = _first_of_next_month(today)
        end_date_exclusive = _first_of_next_month(start_date)
    else:
        assert query.start_date is not None
        assert query.end_date is not None
        start_date, end_date_exclusive = query.start_date, query.end_date + timedelta(days=1)
    return resolve_local_date_range(
        start_date,
        end_date_exclusive - timedelta(days=1),
        timezone=zone,
        scope=query.scope,
    )


def resolve_local_date_range(
    start_date: date,
    end_date: date,
    *,
    timezone: ZoneInfo | str,
    scope: TemporalScope = TemporalScope.DATE_RANGE,
) -> ResolvedTemporalWindow:
    """Resolve an inclusive owner-local calendar-date range to local-midnight bounds."""

    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    zone = _zone(timezone)
    end_date_exclusive = end_date + timedelta(days=1)
    return ResolvedTemporalWindow(
        scope=scope,
        start_at=_local_midnight(start_date, zone),
        end_at=_local_midnight(end_date_exclusive, zone),
        start_local_date=start_date,
        end_local_date_exclusive=end_date_exclusive,
    )


def describe_temporal_window(window: ResolvedTemporalWindow) -> str | None:
    """Return a short owner-facing label for the calendar days a window covers."""

    start = window.start_local_date
    end_exclusive = window.end_local_date_exclusive
    if start is None and end_exclusive is None:
        return None
    if start is None:
        assert end_exclusive is not None
        return f"before {_day_label(end_exclusive, with_year=True)}"
    if end_exclusive is None:
        return f"from {_day_label(start, with_year=True)}"
    last = end_exclusive - timedelta(days=1)
    if last <= start:
        return _day_label(start, with_year=True)
    return (
        f"{_day_label(start, with_year=start.year != last.year)} through "
        f"{_day_label(last, with_year=True)}"
    )


def owner_calendar_context(
    request_time: datetime,
    timezone: ZoneInfo | str,
    *,
    days: int = 14,
) -> str:
    """Host-computed calendar facts so the model looks dates up instead of computing them."""

    if request_time.tzinfo is None or request_time.utcoffset() is None:
        raise ValueError("request_time must be timezone-aware")
    zone = _zone(timezone)
    local_now = request_time.astimezone(zone)
    today = local_now.date()
    week_start = today - timedelta(days=today.weekday())
    next_week_start = week_start + timedelta(days=7)
    upcoming = ", ".join(
        f"{day:%a} {day.isoformat()}" for day in (today + timedelta(days=i) for i in range(days))
    )
    return (
        f"Today is {today:%A}; now is {local_now.isoformat(timespec='seconds')} "
        f"({zone.key}). This week is Mon {week_start.isoformat()} through Sun "
        f"{(week_start + timedelta(days=6)).isoformat()}. Next week is Mon "
        f"{next_week_start.isoformat()} through Sun "
        f"{(next_week_start + timedelta(days=6)).isoformat()}. "
        f"Next {days} days: {upcoming}."
    )


def _zone(timezone: ZoneInfo | str) -> ZoneInfo:
    return timezone if isinstance(timezone, ZoneInfo) else ZoneInfo(timezone)


def _local_midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone)


def _first_of_next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _day_label(day: date, *, with_year: bool) -> str:
    return f"{day:%a %b} {day.day}, {day.year}" if with_year else f"{day:%a %b} {day.day}"


class CursorCodec:
    """Authenticated ephemeral cursor bound to owner, filters, ordering and snapshot."""

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("cursor secret must contain at least 16 bytes")
        self._secret = secret

    def encode(
        self,
        *,
        filters: NormalizedQueryFilters,
        owner_scope: str,
        snapshot: str,
        last_key: tuple[str, ...],
    ) -> str:
        payload = {
            "v": 1,
            "filters": _fingerprint(filters.model_dump(mode="json")),
            "owner": _fingerprint(owner_scope),
            "snapshot": snapshot,
            "last_key": list(last_key),
        }
        raw = _canonical_json(payload).encode()
        signature = hmac.new(self._secret, raw, hashlib.sha256).digest()
        return _b64(raw + signature)

    def decode(
        self,
        cursor: str,
        *,
        filters: NormalizedQueryFilters,
        owner_scope: str,
        snapshot: str,
    ) -> tuple[str, ...]:
        try:
            signed = _unb64(cursor)
            raw, signature = signed[:-32], signed[-32:]
            expected = hmac.new(self._secret, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            decoded = json.loads(raw)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("cursor is invalid") from exc
        if not isinstance(decoded, dict):
            raise ValueError("cursor is invalid")
        payload = cast(dict[str, object], decoded)
        if payload.get("v") != 1:
            raise ValueError("cursor is invalid")
        if payload.get("filters") != _fingerprint(filters.model_dump(mode="json")):
            raise ValueError("cursor does not match the normalized filters")
        if payload.get("owner") != _fingerprint(owner_scope):
            raise ValueError("cursor does not match the owner scope")
        if payload.get("snapshot") != snapshot:
            raise ValueError("cursor snapshot is no longer current")
        last_key = payload.get("last_key")
        if not isinstance(last_key, list):
            raise ValueError("cursor is invalid")
        last_key_values = cast(list[object], last_key)
        if not last_key_values or not all(isinstance(item, str) for item in last_key_values):
            raise ValueError("cursor is invalid")
        return tuple(cast(list[str], last_key_values))


def model_json_size(value: object) -> int:
    return len(_canonical_json(value))


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: object) -> str:
    raw = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


__all__ = [
    "DEFAULT_UPCOMING_DAYS",
    "MAX_DATE_RANGE_DAYS",
    "MAX_QUERY_PAGE_SIZE",
    "MODEL_TOOL_RESULT_MAX_CHARS",
    "CompletenessState",
    "CompletionMode",
    "CursorCodec",
    "FreshnessState",
    "NormalizedQueryFilters",
    "QueryEnvelope",
    "QueryResultKind",
    "ResolvedTemporalWindow",
    "SourceFreshness",
    "TemporalQuery",
    "TemporalScope",
    "describe_temporal_window",
    "model_json_size",
    "owner_calendar_context",
    "resolve_local_date_range",
    "resolve_query_completeness",
    "resolve_temporal_window",
]
