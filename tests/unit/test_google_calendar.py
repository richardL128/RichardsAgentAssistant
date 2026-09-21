from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.connectors.google_calendar import GoogleCalendarConnector
from app.core.errors import ErrorCategory, ErrorCode, LifeAgentError

SECRET_ICAL_URL = (
    "https://calendar.google.com/calendar/ical/proskillsrich%40gmail.com/private-"
    "0123456789abcdef/basic.ics?secret=must-not-leak"
)
NOW = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _ics_response(body: str, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=body.encode(),
        headers={"content-type": "text/calendar; charset=utf-8"},
    )


@pytest.mark.asyncio
async def test_fetch_events_expands_recurring_exclusions_and_normalizes_to_toronto() -> None:
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:ece240-lecture@example.com
SUMMARY:ECE 240 Lecture
DESCRIPTION:Lecture line one\\nline two
LOCATION:Room 101
URL:https://calendar.google.com/calendar/event?eid=lecture
DTSTART;TZID=America/Toronto:20260921T100000
DTEND;TZID=America/Toronto:20260921T112000
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE;TZID=America/Toronto:20260928T100000
DTSTAMP:20260919T120000Z
END:VEVENT
BEGIN:VEVENT
UID:ece240-lecture@example.com
RECURRENCE-ID;TZID=America/Toronto:20261005T100000
SUMMARY:ECE 240 Lecture moved
DTSTART;TZID=America/Toronto:20261005T130000
DTEND;TZID=America/Toronto:20261005T142000
LAST-MODIFIED:20260920T140000Z
END:VEVENT
BEGIN:VEVENT
UID:ece240-lab@example.com
SUMMARY:ECE 240 Lab
DTSTART;VALUE=DATE:20260922
DTEND;VALUE=DATE:20260923
DTSTAMP:20260919T120000Z
END:VEVENT
END:VCALENDAR
"""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _ics_response(ics)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = GoogleCalendarConnector(
            ical_url=SecretStr(SECRET_ICAL_URL),
            client=client,
            clock=lambda: NOW,
        )
        snapshot = await connector.fetch_events(
            datetime(2026, 9, 20, tzinfo=UTC),
            datetime(2026, 10, 20, tzinfo=UTC),
        )

    assert snapshot.source_id.startswith("google-calendar-source:")
    assert SECRET_ICAL_URL not in repr(snapshot)
    assert len(requests) == 1
    assert requests[0].url.host == "calendar.google.com"

    assert [event.title for event in snapshot.events] == [
        "ECE 240 Lecture",
        "ECE 240 Lab",
        "ECE 240 Lecture moved",
        "ECE 240 Lecture",
    ]
    assert [event.starts_at.isoformat() for event in snapshot.events] == [
        "2026-09-21T10:00:00-04:00",
        "2026-09-22T00:00:00-04:00",
        "2026-10-05T13:00:00-04:00",
        "2026-10-12T10:00:00-04:00",
    ]
    assert snapshot.events[1].is_all_day is True
    assert snapshot.events[1].ends_at.isoformat() == "2026-09-23T00:00:00-04:00"
    assert "line two" in (snapshot.events[0].description or "")
    assert len({event.event_id for event in snapshot.events}) == 4
    assert all(len(event.fingerprint) == 64 for event in snapshot.events)


@pytest.mark.asyncio
async def test_cancelled_override_excludes_recurrence_instance() -> None:
    ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:stat231-tutorial@example.com
SUMMARY:STAT 231 Tutorial
DTSTART;TZID=America/Toronto:20260921T090000
DTEND;TZID=America/Toronto:20260921T100000
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:stat231-tutorial@example.com
RECURRENCE-ID;TZID=America/Toronto:20260928T090000
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _ics_response(ics))
    ) as client:
        connector = GoogleCalendarConnector(ical_url=SECRET_ICAL_URL, client=client)
        snapshot = await connector.fetch_events(
            datetime(2026, 9, 20, tzinfo=UTC),
            datetime(2026, 10, 20, tzinfo=UTC),
        )

    assert [event.starts_at.day for event in snapshot.events] == [21, 5]


@pytest.mark.parametrize(
    "url",
    [
        "http://calendar.google.com/calendar/ical/private/basic.ics",
        "https://evil.example/calendar/ical/private/basic.ics",
        "https://user:password@calendar.google.com/calendar/ical/private/basic.ics",
        "https://calendar.google.com/calendar/embed?src=calendar-id",
    ],
)
def test_secret_ical_url_must_be_https_calendar_google_com(url: str) -> None:
    with pytest.raises(LifeAgentError) as raised:
        GoogleCalendarConnector(ical_url=SecretStr(url))

    assert raised.value.record.code == ErrorCode.INPUT_INVALID
    assert raised.value.record.category == ErrorCategory.PERMANENT
    assert "private" not in raised.value.record.diagnostic
    assert "password" not in raised.value.record.diagnostic


def test_source_identity_survives_secret_address_reset() -> None:
    first = GoogleCalendarConnector(ical_url=SecretStr(SECRET_ICAL_URL))
    reset = GoogleCalendarConnector(
        ical_url=SecretStr(
            "https://calendar.google.com/calendar/ical/"
            "proskillsrich%40gmail.com/private-fedcba9876543210/basic.ics"
        )
    )

    assert first.source_id == reset.source_id


@pytest.mark.asyncio
async def test_http_failures_are_safe_lifeagent_errors() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ) as client:
        connector = GoogleCalendarConnector(ical_url=SecretStr(SECRET_ICAL_URL), client=client)
        with pytest.raises(LifeAgentError) as raised:
            await connector.fetch_events(
                datetime(2026, 9, 20, tzinfo=UTC),
                datetime(2026, 9, 21, tzinfo=UTC),
            )

    assert raised.value.record.code == ErrorCode.AUTHORIZATION_INVALID
    assert raised.value.record.category == ErrorCategory.AUTHORIZATION
    assert "secret" not in raised.value.record.diagnostic
    assert "basic.ics" not in raised.value.record.diagnostic


@pytest.mark.asyncio
async def test_oversized_body_is_rejected_without_exposing_url() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _ics_response("x" * 100))
    ) as client:
        connector = GoogleCalendarConnector(
            ical_url=SecretStr(SECRET_ICAL_URL),
            client=client,
            max_response_bytes=20,
        )
        with pytest.raises(LifeAgentError) as raised:
            await connector.fetch_events(
                datetime(2026, 9, 20, tzinfo=UTC),
                datetime(2026, 9, 21, tzinfo=UTC),
            )

    assert raised.value.record.code == ErrorCode.SOURCE_SYNC_FAILED
    assert raised.value.record.retryable is False
    assert "must-not-leak" not in raised.value.record.diagnostic


@pytest.mark.asyncio
async def test_malformed_ics_is_a_safe_input_error() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _ics_response("not a calendar"))
    ) as client:
        connector = GoogleCalendarConnector(ical_url=SecretStr(SECRET_ICAL_URL), client=client)
        with pytest.raises(LifeAgentError) as raised:
            await connector.fetch_events(
                datetime(2026, 9, 20, tzinfo=UTC),
                datetime(2026, 9, 21, tzinfo=UTC),
            )

    assert raised.value.record.code == ErrorCode.INPUT_INVALID
    assert raised.value.record.category == ErrorCategory.PERMANENT
    assert "calendar.google.com" not in raised.value.record.diagnostic
