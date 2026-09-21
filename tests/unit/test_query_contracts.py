from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agents.query_contracts import (
    CompletionMode,
    CursorCodec,
    NormalizedQueryFilters,
    TemporalQuery,
    TemporalScope,
    resolve_temporal_window,
)


def test_today_uses_request_time_and_owner_local_dst_boundaries() -> None:
    window = resolve_temporal_window(
        TemporalQuery(scope=TemporalScope.TODAY),
        request_time=datetime(2026, 3, 8, 6, 30, tzinfo=UTC),
        timezone=ZoneInfo("America/Toronto"),
    )

    assert window.start_at == datetime.fromisoformat("2026-03-08T00:00:00-05:00")
    assert window.end_at == datetime.fromisoformat("2026-03-09T00:00:00-04:00")
    assert window.end_at.astimezone(UTC) - window.start_at.astimezone(UTC) == timedelta(hours=23)


def test_today_does_not_change_when_processing_crosses_local_midnight() -> None:
    window = resolve_temporal_window(
        TemporalQuery(scope="today"),
        request_time=datetime(2026, 9, 20, 3, 59, tzinfo=UTC),
        timezone="America/Toronto",
    )

    assert window.start_local_date.isoformat() == "2026-09-19"
    assert window.end_local_date_exclusive.isoformat() == "2026-09-20"


def test_date_range_rejects_naive_reversed_and_oversized_ranges() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        TemporalQuery(
            scope="date_range",
            start_at="2026-09-01T00:00:00",
            end_at="2026-09-02T00:00:00",
        )
    with pytest.raises(ValidationError, match="after start_at"):
        TemporalQuery(
            scope="date_range",
            start_at="2026-09-02T00:00:00-04:00",
            end_at="2026-09-01T00:00:00-04:00",
        )
    with pytest.raises(ValidationError, match="31 days"):
        TemporalQuery(
            scope="date_range",
            start_at="2026-07-01T00:00:00-04:00",
            end_at="2026-09-01T00:00:00-04:00",
        )


def test_cursor_is_bound_to_filters_owner_and_snapshot() -> None:
    codec = CursorCodec(b"a deterministic test secret")
    window = resolve_temporal_window(
        TemporalQuery(scope="today"),
        request_time=datetime(2026, 9, 19, 16, tzinfo=UTC),
        timezone="America/Toronto",
    )
    filters = NormalizedQueryFilters(
        temporal=window,
        completion=CompletionMode.INCOMPLETE,
        text="lab",
        limit=10,
    )
    cursor = codec.encode(
        filters=filters,
        owner_scope="owner:channel",
        snapshot="sync-1",
        last_key=("2026-09-19T21:00:00Z", "Lab", "id-1"),
    )

    assert (
        codec.decode(
            cursor,
            filters=filters,
            owner_scope="owner:channel",
            snapshot="sync-1",
        )[-1]
        == "id-1"
    )
    with pytest.raises(ValueError, match="owner scope"):
        codec.decode(cursor, filters=filters, owner_scope="other", snapshot="sync-1")
    with pytest.raises(ValueError, match="snapshot"):
        codec.decode(cursor, filters=filters, owner_scope="owner:channel", snapshot="sync-2")
