from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.job_interviews import JobInterviewRepository
from app.db.models import (
    AcademicCourseCalendar,
    Assessment,
    CareerInterviewEvent,
    CareerJobsWorkspace,
    Course,
)

ZONE = ZoneInfo("America/Toronto")


def _engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Course.__table__.create(engine)
    AcademicCourseCalendar.__table__.create(engine)
    Assessment.__table__.create(engine)
    return engine


def _course(session: Session, notion_id: str, title: str) -> Course:
    course = Course(
        notion_id=notion_id,
        course_code=title,
        title=title,
        term="F26",
        priority=50,
        active=True,
    )
    session.add(course)
    session.flush()
    session.add(
        AcademicCourseCalendar(
            course_id=course.id,
            course_page_id=notion_id,
            child_database_id=f"db-{notion_id}",
            child_data_source_id=f"source-{notion_id}",
            title_property_id="title",
            date_property_id="date",
            learn_context_property_id=(
                "context" if title == "Classes + Tutorials + Labs" else None
            ),
            discovery_status="valid",
        )
    )
    return course


def _assessment(
    session: Session,
    course: Course,
    notion_id: str,
    start: datetime,
    *,
    end: datetime | None = None,
    completed: bool = False,
) -> None:
    session.add(
        Assessment(
            course_id=course.id,
            notion_id=notion_id,
            title=notion_id,
            assessment_type="event",
            due_at=start.astimezone(UTC),
            ends_at=end.astimezone(UTC) if end is not None else None,
            fact_state="confirmed",
            confidence=1,
            estimated_minutes=60,
            completed=completed,
            is_all_day=False,
            active=True,
            archived=False,
            source_url=f"https://notion.so/{notion_id}",
            notion_last_edited_at=start.astimezone(UTC),
        )
    )


def test_morning_selection_uses_local_day_overlap_and_following_seven_days() -> None:
    engine = _engine()
    local_day = datetime(2026, 11, 1, 8, 0, tzinfo=ZONE)
    day_start = datetime(2026, 11, 1, 0, 0, tzinfo=ZONE)
    with Session(engine) as session, session.begin():
        course = _course(session, "course-real", "ECE 250")
        misc = _course(session, "course-misc", "misc")
        learn = _course(session, "course-learn", "Classes + Tutorials + Labs")
        _assessment(session, course, "course-today", day_start + timedelta(hours=9))
        _assessment(
            session,
            course,
            "course-completed",
            day_start + timedelta(hours=10),
            completed=True,
        )
        _assessment(session, course, "course-plus-seven", day_start + timedelta(days=7, hours=23))
        _assessment(session, course, "course-plus-eight", day_start + timedelta(days=8))
        _assessment(
            session,
            misc,
            "misc-overlap",
            day_start - timedelta(hours=2),
            end=day_start + timedelta(hours=1),
        )
        _assessment(session, misc, "misc-tomorrow", day_start + timedelta(days=1))
        _assessment(session, learn, "learn-today", day_start + timedelta(hours=13))
        _assessment(session, learn, "learn-tomorrow", day_start + timedelta(days=1, hours=13))

    store = SQLAlchemyAcademicPlannerStore(engine)
    courses = store.load_active_morning_courses()
    items = store.load_morning_calendar_items(occurrence=local_day)

    assert [item["course_id"] for item in courses] == ["course-real"]
    assert {item["event_id"] for item in items} == {
        "course-today",
        "course-plus-seven",
        "misc-overlap",
        "learn-today",
    }
    by_id = {item["event_id"]: item for item in items}
    assert by_id["misc-overlap"]["source_area"] == "misc"
    assert by_id["learn-today"]["source_area"] == "learn"
    assert by_id["learn-today"]["starts_at"].tzinfo is not None
    engine.dispose()


def test_jobs_selection_includes_ranges_overlapping_today_and_excludes_completed() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    CareerJobsWorkspace.__table__.create(engine)
    CareerInterviewEvent.__table__.create(engine)
    day_start = datetime(2026, 11, 1, 0, 0, tzinfo=ZONE)
    with Session(engine) as session, session.begin():
        workspace = CareerJobsWorkspace(
            scope="default",
            jobs_page_id="jobs",
            discovery_status="valid",
            active=True,
        )
        session.add(workspace)
        session.flush()
        session.add_all(
            (
                CareerInterviewEvent(
                    workspace_id=workspace.id,
                    interview_page_id="overlap",
                    title="Overnight interview event",
                    date_start=(day_start - timedelta(hours=2)).astimezone(UTC),
                    local_date=(day_start - timedelta(days=1)).date(),
                    is_all_day=False,
                    timezone="America/Toronto",
                    notion_last_edited_at=day_start.astimezone(UTC),
                    property_snapshot={
                        "Date": {
                            "start": (day_start - timedelta(hours=2)).isoformat(),
                            "end": (day_start + timedelta(hours=1)).isoformat(),
                            "time_zone": None,
                        }
                    },
                    content_fingerprint="a" * 64,
                    active=True,
                    archived=False,
                ),
                CareerInterviewEvent(
                    workspace_id=workspace.id,
                    interview_page_id="completed",
                    title="Completed event",
                    date_start=(day_start + timedelta(hours=9)).astimezone(UTC),
                    local_date=day_start.date(),
                    is_all_day=False,
                    timezone="America/Toronto",
                    notion_last_edited_at=day_start.astimezone(UTC),
                    property_snapshot={"Status": "Completed"},
                    content_fingerprint="b" * 64,
                    active=True,
                    archived=False,
                ),
            )
        )

    with Session(engine) as session:
        items = JobInterviewRepository.load_morning_calendar_items(
            session,
            occurrence=day_start + timedelta(hours=8),
        )

    assert [item["event_id"] for item in items] == ["overlap"]
    assert items[0]["ends_at"] == (day_start + timedelta(hours=1)).astimezone(UTC)
    engine.dispose()
