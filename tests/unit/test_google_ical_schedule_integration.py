from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine

from app.agents.academic_planner.sync import AcademicNotionSync
from app.connectors.google_calendar import GoogleCalendarConnector
from app.connectors.notion import NotionCourse, NotionDiscoveryResult
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.models import Base

NOW = datetime(2026, 9, 19, 16, tzinfo=UTC)
SECRET_URL = (
    "https://calendar.google.com/calendar/ical/course-calendar-id/private-test-token/basic.ics"
)
ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:ece240-lecture@example.com
SUMMARY:ECE 240 Lecture
DESCRIPTION:Small-signal transistor models
LOCATION:E5 6004
DTSTART;TZID=America/Toronto:20260920T100000
DTEND;TZID=America/Toronto:20260920T112000
RRULE:FREQ=WEEKLY;COUNT=3
DTSTAMP:20260919T120000Z
END:VEVENT
END:VCALENDAR
"""


class _NotionConnector:
    async def discover_course_assessments(self) -> NotionDiscoveryResult:
        return NotionDiscoveryResult(
            courses_database_id="courses-db",
            courses_source_id="courses-source",
            courses_source_type="data_source",
            courses=(
                NotionCourse(
                    course_id="schedule-row",
                    course_page_id="schedule-row",
                    course_title="Classes + Tutorials + Labs",
                    raw_parent_id="courses-db",
                    courses_source_id="courses-source",
                    courses_source_type="data_source",
                    last_edited_at=NOW,
                    properties={},
                ),
            ),
            synced_at=NOW,
        )


@pytest.mark.asyncio
async def test_secret_ical_sync_reaches_morning_boundary_and_failure_hides_stale_rows(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'schedule.db'}")
    Base.metadata.create_all(engine)
    store = SQLAlchemyAcademicPlannerStore(engine)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=ICS.encode(),
                headers={"content-type": "text/calendar"},
            )
        )
    ) as client:
        syncer = AcademicNotionSync(
            connector=_NotionConnector(),  # type: ignore[arg-type]
            schedule_connector=GoogleCalendarConnector(
                ical_url=SECRET_URL,
                client=client,
                clock=lambda: NOW,
            ),
            store=store,
        )
        result = await syncer.sync(now=NOW)

    assert result.status == "succeeded"
    assert result.assessment_count == 2
    items = store.load_morning_calendar_items(
        occurrence=date(2026, 9, 20),
        timezone="America/Toronto",
    )
    assert len(items) == 1
    assert items[0]["source_area"] == "learn"
    assert items[0]["title"] == "ECE 240 Lecture"
    assert "Small-signal transistor models" in str(items[0]["inline_evidence"])
    assert SECRET_URL not in repr(items)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(404))
    ) as failed_client:
        failing_syncer = AcademicNotionSync(
            connector=_NotionConnector(),  # type: ignore[arg-type]
            schedule_connector=GoogleCalendarConnector(
                ical_url=SECRET_URL,
                client=failed_client,
                clock=lambda: NOW,
            ),
            store=store,
        )
        failed = await failing_syncer.sync(now=NOW)

    assert failed.status == "partial"
    assert failed.diagnostic_codes == ("academic_schedule_ical_unavailable",)
    assert (
        store.load_morning_calendar_items(
            occurrence=date(2026, 9, 20),
            timezone="America/Toronto",
        )
        == ()
    )
