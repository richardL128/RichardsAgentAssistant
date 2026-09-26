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

    scope: TemporalScope = TemporalScope.ALL
    start_at: datetime | None = None
    end_at: datetime | None = None
    upcoming_days: int = Field(default=DEFAULT_UPCOMING_DAYS, ge=1, le=MAX_DATE_RANGE_DAYS)

    @field_validator("start_at", "end_at")
    @classmethod
    def range_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("date_range timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_scope_fields(self) -> TemporalQuery:
        has_range = self.start_at is not None or self.end_at is not None
        if self.scope is TemporalScope.DATE_RANGE:
            if self.start_at is None or self.end_at is None:
                raise ValueError("date_range requires start_at and end_at")
            if self.end_at <= self.start_at:
                raise ValueError("date_range end_at must be after start_at")
            if self.end_at - self.start_at > timedelta(days=MAX_DATE_RANGE_DAYS):
                raise ValueError(f"date_range may not exceed {MAX_DATE_RANGE_DAYS} days")
        elif has_range:
            raise ValueError("start_at and end_at are valid only for date_range")
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
    """Resolve relative intent once, using the original trusted request timestamp."""

    if request_time.tzinfo is None or request_time.utcoffset() is None:
        raise ValueError("request_time must be timezone-aware")
    zone = timezone if isinstance(timezone, ZoneInfo) else ZoneInfo(timezone)
    local_now = request_time.astimezone(zone)
    today = local_now.date()

    def local_midnight(day: date) -> datetime:
        return datetime.combine(day, time.min, tzinfo=zone)

    start: datetime | None
    end: datetime | None
    start_date: date | None
    end_date: date | None
    if query.scope is TemporalScope.ALL:
        start = end = None
        start_date = end_date = None
    elif query.scope is TemporalScope.TODAY:
        start_date, end_date = today, today + timedelta(days=1)
        start, end = local_midnight(start_date), local_midnight(end_date)
    elif query.scope is TemporalScope.TOMORROW:
        start_date, end_date = today + timedelta(days=1), today + timedelta(days=2)
        start, end = local_midnight(start_date), local_midnight(end_date)
    elif query.scope is TemporalScope.THIS_WEEK:
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=7)
        start, end = local_midnight(start_date), local_midnight(end_date)
    elif query.scope is TemporalScope.UPCOMING:
        start_date, end_date = today, today + timedelta(days=query.upcoming_days + 1)
        start, end = local_now, local_midnight(end_date)
    elif query.scope is TemporalScope.OVERDUE:
        start_date, end_date = None, today
        start, end = None, local_now
    else:
        assert query.start_at is not None
        assert query.end_at is not None
        start = query.start_at.astimezone(zone)
        end = query.end_at.astimezone(zone)
        start_date, end_date = start.date(), end.date()
    return ResolvedTemporalWindow(
        scope=query.scope,
        start_at=start,
        end_at=end,
        start_local_date=start_date,
        end_local_date_exclusive=end_date,
    )


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
    "model_json_size",
    "resolve_query_completeness",
    "resolve_temporal_window",
]
