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
    describe_temporal_window,
    owner_calendar_context,
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


_SATURDAY_AFTERNOON = datetime(2026, 9, 26, 18, 3, tzinfo=UTC)  # Sat 14:03 in Toronto


def _window(scope: str, request_time: datetime = _SATURDAY_AFTERNOON, **fields: object):
    return resolve_temporal_window(
        TemporalQuery.model_validate({"scope": scope, **fields}),
        request_time=request_time,
        timezone="America/Toronto",
    )


def _days(window) -> tuple[str, str]:
    return (
        window.start_local_date.isoformat(),
        window.end_local_date_exclusive.isoformat(),
    )


def test_named_week_and_month_scopes_resolve_host_side() -> None:
    assert _days(_window("this_week")) == ("2026-09-21", "2026-09-28")
    assert _days(_window("next_week")) == ("2026-09-28", "2026-10-05")
    assert _days(_window("this_month")) == ("2026-09-01", "2026-10-01")
    assert _days(_window("next_month")) == ("2026-10-01", "2026-11-01")

    next_week = _window("next_week")
    assert next_week.start_at == datetime.fromisoformat("2026-09-28T00:00:00-04:00")
    assert next_week.end_at == datetime.fromisoformat("2026-10-05T00:00:00-04:00")


def test_week_scopes_on_week_boundaries() -> None:
    sunday_night = datetime(2026, 9, 28, 3, 30, tzinfo=UTC)  # Sun 23:30 in Toronto
    monday_morning = datetime(2026, 9, 28, 4, 30, tzinfo=UTC)  # Mon 00:30 in Toronto

    assert _days(_window("this_week", sunday_night)) == ("2026-09-21", "2026-09-28")
    assert _days(_window("next_week", sunday_night)) == ("2026-09-28", "2026-10-05")
    assert _days(_window("this_week", monday_morning)) == ("2026-09-28", "2026-10-05")
    assert _days(_window("next_week", monday_morning)) == ("2026-10-05", "2026-10-12")


def test_next_month_crosses_the_year_boundary() -> None:
    december = datetime(2026, 12, 15, 17, tzinfo=UTC)

    assert _days(_window("this_month", december)) == ("2026-12-01", "2027-01-01")
    assert _days(_window("next_month", december)) == ("2027-01-01", "2027-02-01")


def test_date_range_takes_inclusive_local_dates() -> None:
    window = _window("date_range", start_date="2026-10-01", end_date="2026-10-04")

    assert _days(window) == ("2026-10-01", "2026-10-05")
    assert window.start_at == datetime.fromisoformat("2026-10-01T00:00:00-04:00")
    assert window.end_at == datetime.fromisoformat("2026-10-05T00:00:00-04:00")
    assert _days(_window("date_range", start_date="2026-10-02", end_date="2026-10-02")) == (
        "2026-10-02",
        "2026-10-03",
    )


def test_date_range_across_dst_end_keeps_local_midnights() -> None:
    window = _window("date_range", start_date="2026-10-31", end_date="2026-11-01")

    assert window.start_at == datetime.fromisoformat("2026-10-31T00:00:00-04:00")
    assert window.end_at == datetime.fromisoformat("2026-11-02T00:00:00-05:00")
    assert window.end_at.astimezone(UTC) - window.start_at.astimezone(UTC) == timedelta(hours=49)


def test_date_range_date_form_validation() -> None:
    with pytest.raises(ValidationError, match="start_date and end_date"):
        TemporalQuery(scope="date_range", start_date="2026-10-01")
    with pytest.raises(ValidationError, match="on or after start_date"):
        TemporalQuery(scope="date_range", start_date="2026-10-04", end_date="2026-10-01")
    with pytest.raises(ValidationError, match="31 days"):
        TemporalQuery(scope="date_range", start_date="2026-10-01", end_date="2026-11-01")
    with pytest.raises(ValidationError, match="not both"):
        TemporalQuery(
            scope="date_range",
            start_date="2026-10-01",
            end_date="2026-10-02",
            start_at="2026-10-01T00:00:00-04:00",
            end_at="2026-10-02T00:00:00-04:00",
        )
    with pytest.raises(ValidationError, match="valid only for date_range"):
        TemporalQuery(scope="next_week", start_date="2026-10-01", end_date="2026-10-02")

    longest = TemporalQuery(scope="date_range", start_date="2026-10-01", end_date="2026-10-31")
    assert longest.end_date is not None


def test_legacy_instant_date_range_uses_local_days_without_truncation() -> None:
    inclusive_end = _window(
        "date_range",
        start_at="2026-10-01T00:00:00-04:00",
        end_at="2026-10-04T23:59:59-04:00",
    )
    assert _days(inclusive_end) == ("2026-10-01", "2026-10-05")

    exclusive_end = _window(
        "date_range",
        start_at="2026-10-01T00:00:00-04:00",
        end_at="2026-10-05T00:00:00-04:00",
    )
    assert _days(exclusive_end) == ("2026-10-01", "2026-10-05")

    utc_written = _window(
        "date_range",
        start_at="2026-10-01T04:00:00Z",
        end_at="2026-10-05T04:00:00Z",
    )
    assert _days(utc_written) == ("2026-10-01", "2026-10-05")


def test_model_schema_exposes_date_fields_and_hides_legacy_instants() -> None:
    schema = TemporalQuery.model_json_schema()
    properties = schema["properties"]

    assert "start_date" in properties
    assert "end_date" in properties
    assert "start_at" not in properties
    assert "end_at" not in properties
    scopes = schema["$defs"]["TemporalScope"]["enum"]
    assert {"next_week", "this_month", "next_month"} <= set(scopes)


def test_window_labels_name_the_searched_days() -> None:
    assert describe_temporal_window(_window("today")) == "Sat Sep 26, 2026"
    assert describe_temporal_window(_window("next_week")) == "Mon Sep 28 through Sun Oct 4, 2026"
    assert describe_temporal_window(_window("overdue")) == "before Sat Sep 26, 2026"
    assert describe_temporal_window(_window("all")) is None
    december = datetime(2026, 12, 30, 17, tzinfo=UTC)
    assert (
        describe_temporal_window(_window("next_week", december))
        == "Mon Jan 4 through Sun Jan 10, 2027"
    )
    assert (
        describe_temporal_window(_window("this_week", december))
        == "Mon Dec 28, 2026 through Sun Jan 3, 2027"
    )


def test_owner_calendar_context_states_weekday_and_week_bounds() -> None:
    context = owner_calendar_context(_SATURDAY_AFTERNOON, "America/Toronto")

    assert "Today is Saturday; now is 2026-09-26T14:03:00-04:00 (America/Toronto)" in context
    assert "This week is Mon 2026-09-21 through Sun 2026-09-27" in context
    assert "Next week is Mon 2026-09-28 through Sun 2026-10-04" in context
    assert "Fri 2026-10-02" in context
    assert "Fri 2026-10-09" in context
    assert "Sat 2026-10-10" not in context
