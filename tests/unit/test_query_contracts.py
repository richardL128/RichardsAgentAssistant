from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agents.query_contracts import (
    CompletenessState,
    CompletionMode,
    CursorCodec,
    FreshnessState,
    NormalizedQueryFilters,
    QueryEnvelope,
    SourceFreshness,
    TemporalQuery,
    TemporalScope,
    resolve_query_completeness,
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


def test_query_completeness_precedence_is_source_scoped() -> None:
    fresh = SourceFreshness(
        source_id="fresh",
        state=FreshnessState.FRESH_COMPLETE,
        as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
    )
    unavailable = SourceFreshness(
        source_id="unavailable",
        state=FreshnessState.UNAVAILABLE,
        diagnostic_codes=("not_synced",),
    )
    cached = SourceFreshness(
        source_id="cached",
        state=FreshnessState.CACHED_STALE,
        as_of=datetime(2026, 9, 18, 16, tzinfo=UTC),
    )

    assert (
        resolve_query_completeness(freshness=(fresh, unavailable), has_more=True)
        is CompletenessState.PARTIAL
    )
    assert (
        resolve_query_completeness(freshness=(unavailable,), has_more=True)
        is CompletenessState.UNAVAILABLE
    )
    assert (
        resolve_query_completeness(freshness=(cached,), has_more=True)
        is CompletenessState.CACHED_STALE
    )
    assert (
        resolve_query_completeness(freshness=(fresh,), has_more=True)
        is CompletenessState.MORE_AVAILABLE
    )


def test_query_envelope_accepts_partial_and_rejects_wrong_precedence() -> None:
    window = resolve_temporal_window(
        TemporalQuery(scope=TemporalScope.TODAY),
        request_time=datetime(2026, 9, 19, 16, tzinfo=UTC),
        timezone="America/Toronto",
    )
    filters = NormalizedQueryFilters(temporal=window, limit=10)
    fresh = SourceFreshness(
        source_id="fresh",
        state=FreshnessState.FRESH_COMPLETE,
        as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
    )
    unavailable = SourceFreshness(
        source_id="unavailable",
        state=FreshnessState.UNAVAILABLE,
        diagnostic_codes=("not_synced",),
    )

    envelope = QueryEnvelope[dict[str, str]](
        query_id="query-1",
        as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
        timezone="America/Toronto",
        applied_filters=filters,
        freshness=(fresh, unavailable),
        items=(),
        result_count=0,
        has_more=False,
        completeness=CompletenessState.PARTIAL,
    )

    assert envelope.completeness is CompletenessState.PARTIAL
    with pytest.raises(ValidationError, match="completeness"):
        QueryEnvelope[dict[str, str]](
            query_id="query-2",
            as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
            timezone="America/Toronto",
            applied_filters=filters,
            freshness=(fresh, unavailable),
            items=(),
            result_count=0,
            has_more=False,
            completeness=CompletenessState.COMPLETE,
        )
