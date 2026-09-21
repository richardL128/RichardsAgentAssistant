"""Secret iCal reader for Google Calendar course events."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dateutil.rrule import rrule as dateutil_rrule
from dateutil.rrule import rruleset, rrulestr
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.core.errors import (
    ErrorCategory,
    ErrorCode,
    ErrorRecord,
    LifeAgentError,
    authorization_error,
    permanent_error,
    transient_error,
)

GOOGLE_CALENDAR_HOST: Final[str] = "calendar.google.com"
MAX_GOOGLE_CALENDAR_RESPONSE_BYTES: Final[int] = 2_000_000
MAX_GOOGLE_CALENDAR_EVENTS: Final[int] = 5_000
DEFAULT_GOOGLE_CALENDAR_WINDOW_DAYS: Final[int] = 180

_TORONTO: Final[ZoneInfo] = ZoneInfo("America/Toronto")
_DATE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{8}$")
_DATE_TIME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{8}T\d{6}Z?$")


class GoogleCalendarEvent(BaseModel):
    """One normalized occurrence from the Google Calendar iCal feed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=96)
    source_event_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=1_024)
    starts_at: datetime
    ends_at: datetime
    is_all_day: bool
    updated_at: datetime | None = None
    fingerprint: str = Field(min_length=64, max_length=64)
    description: str | None = Field(default=None, max_length=10_000)
    location: str | None = Field(default=None, max_length=1_024)
    source_url: str | None = Field(default=None, max_length=4_096)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def event_times_are_toronto(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Google Calendar event times must be timezone-aware")
        return value.astimezone(_TORONTO)

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Google Calendar updated_at must be timezone-aware")
        return value.astimezone(UTC)


class GoogleCalendarSnapshot(BaseModel):
    """A bounded event snapshot for one secret Google Calendar iCal source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=96)
    retrieved_at: datetime
    window_start: datetime
    window_end: datetime
    events: tuple[GoogleCalendarEvent, ...] = Field(max_length=MAX_GOOGLE_CALENDAR_EVENTS)

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Google Calendar retrieved_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("window_start", "window_end")
    @classmethod
    def window_times_are_toronto(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Google Calendar window times must be timezone-aware")
        return value.astimezone(_TORONTO)


@dataclass(frozen=True, slots=True)
class _IcsProperty:
    name: str
    params: dict[str, tuple[str, ...]]
    value: str


@dataclass(frozen=True, slots=True)
class _IcsDateTime:
    value: datetime
    is_date: bool


@dataclass(frozen=True, slots=True)
class _RawEvent:
    properties: dict[str, tuple[_IcsProperty, ...]]

    def first(self, name: str) -> _IcsProperty | None:
        values = self.properties.get(name)
        if not values:
            return None
        return values[0]

    def all(self, name: str) -> tuple[_IcsProperty, ...]:
        return self.properties.get(name, ())


class GoogleCalendarConnector:
    """Read a secret Google Calendar iCal URL without exposing URL secrets."""

    def __init__(
        self,
        *,
        ical_url: SecretStr | str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 8.0,
        max_response_bytes: int = MAX_GOOGLE_CALENDAR_RESPONSE_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise permanent_error(
                ErrorCode.INPUT_INVALID,
                "google calendar timeout must be finite and between 0 and 60 seconds",
            )
        if max_response_bytes <= 0 or max_response_bytes > MAX_GOOGLE_CALENDAR_RESPONSE_BYTES:
            raise permanent_error(
                ErrorCode.INPUT_INVALID,
                "google calendar response limit is invalid",
            )
        url = _secret_value(ical_url)
        self._url = _validate_secret_ical_url(url)
        self._source_id = _stable_id("google-calendar-source", _calendar_identity(self._url))
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def source_id(self) -> str:
        return self._source_id

    async def fetch_events(
        self,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> GoogleCalendarSnapshot:
        start, end = _normalize_window(window_start, window_end, clock=self._clock)
        body = await self._fetch_ics()
        events = _parse_events(body.decode("utf-8-sig"), window_start=start, window_end=end)
        return GoogleCalendarSnapshot(
            source_id=self._source_id,
            retrieved_at=_aware_utc(self._clock()),
            window_start=start,
            window_end=end,
            events=tuple(
                sorted(events, key=lambda event: (event.starts_at, event.ends_at, event.event_id))
            ),
        )

    async def _fetch_ics(self) -> bytes:
        client = self._client
        if client is None:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as owned_client:
                return await self._stream_ics(owned_client)
        return await self._stream_ics(client)

    async def _stream_ics(self, client: httpx.AsyncClient) -> bytes:
        try:
            async with client.stream(
                "GET",
                self._url,
                follow_redirects=False,
                timeout=self._timeout_seconds,
            ) as response:
                if response.status_code in {401, 403, 404}:
                    raise authorization_error("google calendar iCal address is not accessible")
                if response.status_code >= 400:
                    raise transient_error(
                        ErrorCode.CONNECTOR_TRANSIENT,
                        "google calendar iCal request failed",
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None and int(content_length) > self._max_response_bytes:
                    raise _oversized_error()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self._max_response_bytes:
                        raise _oversized_error()
                    chunks.append(chunk)
                return b"".join(chunks)
        except LifeAgentError:
            raise
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "google calendar iCal request failed",
            ) from None


def _parse_events(
    ics_text: str, *, window_start: datetime, window_end: datetime
) -> list[GoogleCalendarEvent]:
    raw_events = _parse_ics_events(ics_text)
    masters: dict[str, list[_RawEvent]] = {}
    overrides: dict[str, dict[str, _RawEvent]] = {}
    cancelled: dict[str, set[str]] = {}
    loose_overrides: list[_RawEvent] = []

    for raw_event in raw_events:
        uid = _required_text(raw_event, "UID")
        recurrence_id = raw_event.first("RECURRENCE-ID")
        if recurrence_id is None:
            masters.setdefault(uid, []).append(raw_event)
            continue
        recurrence_start = _parse_ics_datetime(recurrence_id)
        key = _occurrence_key(recurrence_start.value)
        if _status(raw_event) == "CANCELLED":
            cancelled.setdefault(uid, set()).add(key)
        else:
            overrides.setdefault(uid, {})[key] = raw_event
            loose_overrides.append(raw_event)

    events: list[GoogleCalendarEvent] = []
    consumed_overrides: set[int] = set()
    for uid, uid_masters in masters.items():
        for master in uid_masters:
            if _status(master) == "CANCELLED":
                continue
            if _is_recurring(master):
                for event, override_id in _expand_recurring_event(
                    uid,
                    master,
                    overrides=overrides.get(uid, {}),
                    cancelled=cancelled.get(uid, set()),
                    window_start=window_start,
                    window_end=window_end,
                ):
                    events.append(event)
                    if override_id is not None:
                        consumed_overrides.add(override_id)
            else:
                event = _build_event(uid, master, occurrence_start=None)
                if _intersects(event, window_start, window_end):
                    events.append(event)
            if len(events) > MAX_GOOGLE_CALENDAR_EVENTS:
                raise permanent_error(
                    ErrorCode.SOURCE_SYNC_FAILED,
                    "google calendar iCal payload produced too many events",
                )

    for raw_event in loose_overrides:
        if id(raw_event) in consumed_overrides:
            continue
        uid = _required_text(raw_event, "UID")
        recurrence_id = raw_event.first("RECURRENCE-ID")
        if recurrence_id is None:
            raise _malformed_ics_error()
        event = _build_event(
            uid, raw_event, occurrence_start=_parse_ics_datetime(recurrence_id).value
        )
        if _intersects(event, window_start, window_end):
            events.append(event)

    return events


def _expand_recurring_event(
    uid: str,
    master: _RawEvent,
    *,
    overrides: dict[str, _RawEvent],
    cancelled: set[str],
    window_start: datetime,
    window_end: datetime,
) -> Iterable[tuple[GoogleCalendarEvent, int | None]]:
    dtstart = _required_datetime(master, "DTSTART")
    duration = _event_duration(master, dtstart)
    recurrence_set = rruleset()
    try:
        has_rrule = bool(master.all("RRULE"))
        for rule in master.all("RRULE"):
            parsed_rule = rrulestr(rule.value, dtstart=dtstart.value)
            if not isinstance(parsed_rule, dateutil_rrule):
                raise _malformed_ics_error()
            recurrence_set.rrule(parsed_rule)
        if not has_rrule:
            recurrence_set.rdate(dtstart.value)
        for rdate in _date_list(master, "RDATE"):
            recurrence_set.rdate(rdate.value)
        for exdate in _date_list(master, "EXDATE"):
            recurrence_set.exdate(exdate.value)
    except (TypeError, ValueError):
        raise _malformed_ics_error() from None

    search_start = window_start - duration
    try:
        occurrence_starts = recurrence_set.between(search_start, window_end, inc=True)
    except (TypeError, ValueError):
        raise _malformed_ics_error() from None

    for occurrence_start in occurrence_starts:
        occurrence_start = occurrence_start.astimezone(_TORONTO)
        occurrence_key = _occurrence_key(occurrence_start)
        if occurrence_key in cancelled:
            continue
        override = overrides.get(occurrence_key)
        raw_event = override or master
        event = _build_event(uid, raw_event, occurrence_start=occurrence_start)
        if _intersects(event, window_start, window_end):
            yield event, id(override) if override is not None else None


def _build_event(
    uid: str,
    raw_event: _RawEvent,
    *,
    occurrence_start: datetime | None,
) -> GoogleCalendarEvent:
    start = _required_datetime(raw_event, "DTSTART")
    end = _first_datetime(raw_event, "DTEND")
    starts_at = start.value
    ends_at = end.value if end is not None else starts_at + _default_duration(start)
    fallback_start = _first_datetime(raw_event, "RECURRENCE-ID", default=start)
    if fallback_start is None:
        raise _malformed_ics_error()
    original_start = occurrence_start or fallback_start.value

    if occurrence_start is not None and raw_event.first("RECURRENCE-ID") is None:
        delta = ends_at - starts_at
        starts_at = occurrence_start
        ends_at = occurrence_start + delta

    title = _text(raw_event.first("SUMMARY")) or "(untitled)"
    description = _optional_bounded_text(_text(raw_event.first("DESCRIPTION")), 10_000)
    location = _optional_bounded_text(_text(raw_event.first("LOCATION")), 1_024)
    source_url = _optional_bounded_text(_text(raw_event.first("URL")), 4_096)
    updated_at = _updated_at(raw_event)
    source_event_id = f"{uid}:{original_start.astimezone(UTC).isoformat()}"
    event_id = _stable_id("google-calendar-event", source_event_id)
    fingerprint = _fingerprint(
        {
            "source_event_id": source_event_id,
            "title": title,
            "starts_at": starts_at.astimezone(UTC).isoformat(),
            "ends_at": ends_at.astimezone(UTC).isoformat(),
            "is_all_day": start.is_date,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "description": description,
            "location": location,
            "source_url": source_url,
        }
    )
    return GoogleCalendarEvent(
        event_id=event_id,
        source_event_id=source_event_id,
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        is_all_day=start.is_date,
        updated_at=updated_at,
        fingerprint=fingerprint,
        description=description,
        location=location,
        source_url=source_url,
    )


def _parse_ics_events(ics_text: str) -> tuple[_RawEvent, ...]:
    lines = _unfold_ics_lines(ics_text)
    if not lines or lines[0].upper() != "BEGIN:VCALENDAR":
        raise _malformed_ics_error()
    in_calendar = False
    in_event = False
    event_lines: list[str] = []
    events: list[_RawEvent] = []

    for line in lines:
        upper = line.upper()
        if upper == "BEGIN:VCALENDAR":
            if in_calendar:
                raise _malformed_ics_error()
            in_calendar = True
            continue
        if upper == "END:VCALENDAR":
            if not in_calendar or in_event:
                raise _malformed_ics_error()
            in_calendar = False
            continue
        if not in_calendar:
            raise _malformed_ics_error()
        if upper == "BEGIN:VEVENT":
            if in_event:
                raise _malformed_ics_error()
            in_event = True
            event_lines = []
            continue
        if upper == "END:VEVENT":
            if not in_event:
                raise _malformed_ics_error()
            events.append(_parse_raw_event(event_lines))
            in_event = False
            event_lines = []
            continue
        if in_event:
            event_lines.append(line)

    if in_calendar or in_event:
        raise _malformed_ics_error()
    return tuple(events)


def _unfold_ics_lines(ics_text: str) -> list[str]:
    unfolded: list[str] = []
    for raw_line in ics_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")):
            if not unfolded:
                raise _malformed_ics_error()
            unfolded[-1] += raw_line[1:]
            continue
        if raw_line:
            unfolded.append(raw_line)
    return unfolded


def _parse_raw_event(lines: Iterable[str]) -> _RawEvent:
    properties: dict[str, list[_IcsProperty]] = {}
    for line in lines:
        prop = _parse_property(line)
        properties.setdefault(prop.name, []).append(prop)
    return _RawEvent({name: tuple(values) for name, values in properties.items()})


def _parse_property(line: str) -> _IcsProperty:
    if ":" not in line:
        raise _malformed_ics_error()
    left, value = line.split(":", 1)
    parts = left.split(";")
    name = parts[0].upper()
    if not name:
        raise _malformed_ics_error()
    params: dict[str, tuple[str, ...]] = {}
    for raw_param in parts[1:]:
        if "=" not in raw_param:
            raise _malformed_ics_error()
        key, raw_value = raw_param.split("=", 1)
        values = tuple(item.strip('"') for item in raw_value.split(","))
        params[key.upper()] = values
    return _IcsProperty(name=name, params=params, value=value)


def _date_list(raw_event: _RawEvent, name: str) -> tuple[_IcsDateTime, ...]:
    values: list[_IcsDateTime] = []
    for prop in raw_event.all(name):
        values.extend(
            _parse_ics_datetime(_IcsProperty(prop.name, prop.params, item))
            for item in prop.value.split(",")
        )
    return tuple(values)


def _required_datetime(raw_event: _RawEvent, name: str) -> _IcsDateTime:
    value = _first_datetime(raw_event, name)
    if value is None:
        raise _malformed_ics_error()
    return value


def _first_datetime(
    raw_event: _RawEvent,
    name: str,
    *,
    default: _IcsDateTime | None = None,
) -> _IcsDateTime | None:
    prop = raw_event.first(name)
    if prop is None:
        return default
    return _parse_ics_datetime(prop)


def _parse_ics_datetime(prop: _IcsProperty | None) -> _IcsDateTime:
    if prop is None:
        raise _malformed_ics_error()
    value = prop.value.strip()
    if prop.params.get("VALUE") == ("DATE",) or _DATE_PATTERN.fullmatch(value):
        parsed_date = datetime.strptime(value, "%Y%m%d").replace(tzinfo=_TORONTO).date()
        return _IcsDateTime(
            datetime.combine(parsed_date, time.min, tzinfo=_TORONTO),
            is_date=True,
        )
    if not _DATE_TIME_PATTERN.fullmatch(value):
        raise _malformed_ics_error()
    has_utc_suffix = value.endswith("Z")
    clean_value = value.removesuffix("Z")
    parsed = datetime.strptime(clean_value, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    if has_utc_suffix:
        aware = parsed
    else:
        tzid = prop.params.get("TZID", ("America/Toronto",))[0]
        try:
            aware = parsed.replace(tzinfo=ZoneInfo(tzid))
        except ZoneInfoNotFoundError:
            raise _malformed_ics_error() from None
    return _IcsDateTime(aware.astimezone(_TORONTO), is_date=False)


def _event_duration(raw_event: _RawEvent, start: _IcsDateTime) -> timedelta:
    end = _first_datetime(raw_event, "DTEND")
    if end is not None:
        return end.value - start.value
    return _default_duration(start)


def _default_duration(start: _IcsDateTime) -> timedelta:
    if start.is_date:
        return timedelta(days=1)
    return timedelta(0)


def _required_text(raw_event: _RawEvent, name: str) -> str:
    value = _text(raw_event.first(name))
    if not value:
        raise _malformed_ics_error()
    return value


def _text(prop: _IcsProperty | None) -> str | None:
    if prop is None:
        return None
    return _unescape_text(prop.value).strip()


def _unescape_text(value: str) -> str:
    result: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            result.append("\n" if char in {"n", "N"} else char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            result.append(char)
    if escaped:
        result.append("\\")
    return "".join(result)


def _optional_bounded_text(value: str | None, max_length: int) -> str | None:
    if not value:
        return None
    return value[:max_length]


def _updated_at(raw_event: _RawEvent) -> datetime | None:
    for name in ("UPDATED", "LAST-MODIFIED", "DTSTAMP"):
        parsed = _first_datetime(raw_event, name)
        if parsed is not None:
            return parsed.value.astimezone(UTC)
    return None


def _status(raw_event: _RawEvent) -> str | None:
    status = _text(raw_event.first("STATUS"))
    if status is None:
        return None
    return status.upper()


def _is_recurring(raw_event: _RawEvent) -> bool:
    return bool(raw_event.all("RRULE") or raw_event.all("RDATE"))


def _intersects(
    event: GoogleCalendarEvent,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    return event.starts_at < window_end and event.ends_at > window_start


def _normalize_window(
    window_start: datetime | None,
    window_end: datetime | None,
    *,
    clock: Callable[[], datetime],
) -> tuple[datetime, datetime]:
    window_start = _aware_toronto(clock()) if window_start is None else _aware_toronto(window_start)
    if window_end is None:
        window_end = window_start + timedelta(days=DEFAULT_GOOGLE_CALENDAR_WINDOW_DAYS)
    else:
        window_end = _aware_toronto(window_end)
    if window_end <= window_start:
        raise permanent_error(
            ErrorCode.INPUT_INVALID,
            "google calendar event window end must be after start",
        )
    return window_start, window_end


def _validate_secret_ical_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != GOOGLE_CALENDAR_HOST:
        raise permanent_error(
            ErrorCode.INPUT_INVALID,
            "google calendar iCal URL must use HTTPS calendar.google.com",
        )
    path_parts = tuple(part for part in parsed.path.split("/") if part)
    if (
        parsed.username
        or parsed.password
        or len(path_parts) < 5
        or path_parts[:2] != ("calendar", "ical")
        or not path_parts[2]
        or not path_parts[-2].startswith("private-")
        or path_parts[-1] != "basic.ics"
    ):
        raise permanent_error(
            ErrorCode.INPUT_INVALID,
            "google calendar iCal URL is invalid",
        )
    return url


def _calendar_identity(url: str) -> str:
    """Return the non-secret calendar identifier so secret resets keep one source."""

    path_parts = tuple(part for part in urlsplit(url).path.split("/") if part)
    return path_parts[2]


def _secret_value(value: SecretStr | str) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _fingerprint(payload: dict[str, object]) -> str:
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def _occurrence_key(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _aware_toronto(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=_TORONTO)
    return value.astimezone(_TORONTO)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _malformed_ics_error() -> LifeAgentError:
    return permanent_error(
        ErrorCode.INPUT_INVALID,
        "google calendar iCal payload is malformed",
    )


def _oversized_error() -> LifeAgentError:
    return LifeAgentError(
        ErrorRecord(
            code=ErrorCode.SOURCE_SYNC_FAILED,
            category=ErrorCategory.PERMANENT,
            retryable=False,
            diagnostic="google calendar iCal payload exceeded maximum size",
        )
    )


__all__ = [
    "DEFAULT_GOOGLE_CALENDAR_WINDOW_DAYS",
    "GOOGLE_CALENDAR_HOST",
    "MAX_GOOGLE_CALENDAR_EVENTS",
    "MAX_GOOGLE_CALENDAR_RESPONSE_BYTES",
    "GoogleCalendarConnector",
    "GoogleCalendarEvent",
    "GoogleCalendarSnapshot",
]
