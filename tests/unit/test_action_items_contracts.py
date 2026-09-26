from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.agents.action_items import (
    ActionItemContext,
    ActionItemDomain,
    ActionItemKind,
    ActionItemSource,
    ActionItemSourceKind,
    ActionItemStatus,
    CanonicalActionItem,
    DateOnlyValue,
    DateTimeValue,
    parse_notion_temporal_value,
    temporal_start_local_date,
)


def test_date_only_value_preserves_calendar_date_without_instant() -> None:
    value = DateOnlyValue(start_date=date(2026, 9, 22))

    assert value.precision == "date"
    assert value.start_date == date(2026, 9, 22)
    assert value.model_dump() == {
        "precision": "date",
        "start_date": date(2026, 9, 22),
        "end_date_exclusive": None,
    }


def test_datetime_value_normalizes_instants_to_utc_and_keeps_iana_timezone() -> None:
    value = DateTimeValue(
        start_at=datetime(2026, 9, 22, 9, 30, tzinfo=ZoneInfo("America/Toronto")),
        end_at=datetime(2026, 9, 22, 10, 0, tzinfo=ZoneInfo("America/Toronto")),
        timezone="America/Toronto",
    )

    assert value.precision == "datetime"
    assert value.start_at == datetime(2026, 9, 22, 13, 30, tzinfo=UTC)
    assert value.end_at == datetime(2026, 9, 22, 14, 0, tzinfo=UTC)
    assert value.timezone == "America/Toronto"


def test_datetime_value_rejects_naive_or_unknown_timezone() -> None:
    with pytest.raises(ValidationError):
        DateTimeValue(
            start_at=datetime.fromisoformat("2026-09-22T09:30:00"),
            timezone="America/Toronto",
        )

    with pytest.raises(ValidationError):
        DateTimeValue(
            start_at=datetime(2026, 9, 22, 13, 30, tzinfo=UTC),
            timezone="Mars/Base",
        )


def test_canonical_action_item_completion_is_derived_from_status() -> None:
    source = ActionItemSource(
        kind=ActionItemSourceKind.NOTION_ACTION_ITEMS,
        source_label="Action Items",
        notion_data_source_id="source-1",
        property_ids={"status": "prop-status"},
    )
    base = {
        "item_id": "item-1",
        "title": "Submit lab",
        "domain": ActionItemDomain.ACADEMIC,
        "item_kind": ActionItemKind.LAB,
        "source": source,
        "context": ActionItemContext(course_code="ECE101"),
    }

    open_item = CanonicalActionItem(status=ActionItemStatus.TO_DO, **base)
    done_item = CanonicalActionItem(status=ActionItemStatus.DONE, **base)
    canceled_item = CanonicalActionItem(status=ActionItemStatus.CANCELED, **base)

    assert open_item.completed is False
    assert done_item.completed is True
    assert done_item.model_dump()["completed"] is True
    assert canceled_item.completed is False


def test_status_contract_excludes_blocked_and_completed_aliases() -> None:
    assert {status.value for status in ActionItemStatus} == {
        "inbox",
        "needs_review",
        "to_do",
        "in_progress",
        "waiting",
        "done",
        "canceled",
    }


def test_active_source_kind_contract_excludes_legacy_course_assessment_source() -> None:
    assert "legacy_notion_course_assessment" not in {
        source_kind.value for source_kind in ActionItemSourceKind
    }


def test_parse_notion_date_range_keeps_date_only_and_makes_end_exclusive() -> None:
    value = parse_notion_temporal_value(
        start="2026-09-22",
        end="2026-09-24",
        time_zone=None,
        default_timezone="America/Toronto",
    )

    assert isinstance(value, DateOnlyValue)
    assert value.start_date == date(2026, 9, 22)
    assert value.end_date_exclusive == date(2026, 9, 25)


def test_parse_notion_timed_value_keeps_explicit_zone_and_normalizes_utc() -> None:
    value = parse_notion_temporal_value(
        start="2026-09-22T09:30:00",
        end=None,
        time_zone="America/Vancouver",
        default_timezone="America/Toronto",
    )

    assert isinstance(value, DateTimeValue)
    assert value.start_at == datetime(2026, 9, 22, 16, 30, tzinfo=UTC)
    assert value.timezone == "America/Vancouver"


def test_parse_notion_offset_crossing_keeps_owner_local_date() -> None:
    value = parse_notion_temporal_value(
        start="2026-09-22T23:30:00-04:00",
        end=None,
        time_zone=None,
        default_timezone="America/Toronto",
    )

    assert isinstance(value, DateTimeValue)
    assert value.start_at == datetime(2026, 9, 23, 3, 30, tzinfo=UTC)
    assert temporal_start_local_date(value) == date(2026, 9, 22)


def test_parse_notion_naive_timed_value_rejects_ambiguous_wall_time() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        parse_notion_temporal_value(
            start="2026-11-01T01:30:00",
            end=None,
            time_zone="America/Toronto",
            default_timezone="America/Toronto",
        )


def test_parse_notion_naive_timed_value_rejects_nonexistent_wall_time() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        parse_notion_temporal_value(
            start="2026-03-08T02:30:00",
            end=None,
            time_zone="America/Toronto",
            default_timezone="America/Toronto",
        )
