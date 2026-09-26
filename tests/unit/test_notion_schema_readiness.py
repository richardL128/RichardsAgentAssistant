"""Tests for the redacted Notion schema readiness script."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from app.connectors.notion import (
    NotionActionItemRecord,
    NotionApplicationRecord,
    NotionConfiguredSource,
    NotionDateValue,
    NotionInterviewRecord,
    NotionPreflightResult,
)
from scripts.notion_schema_readiness import build_redacted_report

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _source(database: str) -> NotionConfiguredSource:
    return NotionConfiguredSource(
        database=database,  # type: ignore[arg-type]
        database_id=f"{database}-database-secret",
        source_id=f"{database}-source-secret",
        source_type="database",
        property_ids={"Name": "property-secret"},
        property_types={"Name": "title"},
    )


def _base(database: str) -> dict[str, object]:
    return {
        "page_id": f"{database}-page-secret",
        "database": database,
        "database_id": f"{database}-database-secret",
        "source_id": f"{database}-source-secret",
        "source_type": "database",
        "source_url": "https://www.notion.so/private-url",
        "title": f"SECRET {database} title",
        "last_edited_at": NOW,
        "property_ids": {"Name": "property-secret"},
        "properties": {"Name": f"SECRET {database} title"},
    }


def test_notion_schema_readiness_report_is_redacted_and_counts_coverage() -> None:
    preflight = NotionPreflightResult(
        sources={
            "courses": _source("courses"),
            "action_items": _source("action_items"),
            "applications": _source("applications"),
            "interviews": _source("interviews"),
        },
        diagnostics=(),
        synced_at=NOW,
    )
    action_item = NotionActionItemRecord(
        **_base("action_items"),
        date=NotionDateValue(start="2026-09-24", end=None, time_zone=None),
        review_reason="SECRET review text",
        relation_ids={
            "Course": ("course-secret",),
            "Application": (),
            "Interview": ("interview-secret",),
        },
        course_ids=("course-secret",),
        interview_ids=("interview-secret",),
    )
    application = NotionApplicationRecord(
        **_base("applications"),
        role="SECRET role",
        company="SECRET company",
        applied_on=NotionDateValue(start="2026-09-24T09:00:00-04:00", end=None, time_zone=None),
        posting_url="https://example.test/private-job",
    )
    interview = NotionInterviewRecord(
        **_base("interviews"),
        date=None,
        relation_ids={"Application": ("application-secret",)},
        application_ids=("application-secret",),
    )

    report = build_redacted_report(
        preflight,
        courses=[],
        action_items=[action_item],
        applications=[application],
        interviews=[interview],
    )
    encoded = json.dumps(report, sort_keys=True)

    assert report["ready"] is True
    assert report["sources"]["action_items"]["count"] == 1  # type: ignore[index]
    assert report["sources"]["action_items"]["date_precision"]["date"] == 1  # type: ignore[index]
    assert report["sources"]["action_items"]["relation_coverage"]["Application"] == {  # type: ignore[index]
        "linked": 0,
        "missing": 1,
    }
    assert report["sources"]["action_items"]["review_count"] == 1  # type: ignore[index]
    assert report["sources"]["applications"]["date_precision"]["datetime"] == 1  # type: ignore[index]
    assert report["sources"]["interviews"]["relation_coverage"]["Application"] == {  # type: ignore[index]
        "linked": 1,
        "missing": 0,
    }
    assert "SECRET" not in encoded
    assert "notion.so" not in encoded
    assert "example.test" not in encoded
    assert "course-secret" not in encoded
