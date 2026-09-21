from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.academic_planner.contracts import (
    AcademicAssessmentOption,
    AcademicAssessmentQueryArgs,
    AcademicCourseQueryArgs,
    AssessmentType,
    UpdateAssessmentCall,
)
from app.agents.academic_planner.proposal_validation import proposed_changes_from_calls
from app.agents.query_contracts import TemporalQuery
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.models import AcademicCourseCalendar, Assessment, Base, Course


def _store() -> tuple[SQLAlchemyAcademicPlannerStore, str, str]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    course = Course(
        notion_id="course-page",
        course_code="ECE 202",
        title="Electric Circuits",
        term="Fall 2026",
        active=True,
    )
    with Session(engine) as session, session.begin():
        session.add(course)
        session.flush()
        calendar = AcademicCourseCalendar(
            course_id=course.id,
            course_page_id="course-page",
            child_database_id="database123",
            child_data_source_id="source123",
            title_property_id="titleProp",
            title_property_name="Name",
            date_property_id="dateProp",
            date_property_name="Date",
            discovery_status="valid",
            last_synced_at=datetime(2026, 9, 6, 18, tzinfo=UTC),
        )
        assessment = Assessment(
            course_id=course.id,
            notion_id="assessment-page",
            title="Lab report",
            assessment_type="assignment",
            due_at=datetime(2026, 9, 10, 21, tzinfo=UTC),
            estimated_minutes=60,
            fact_state="confirmed",
            confidence=1.0,
            source_id="source123",
            notion_last_edited_at=datetime(2026, 9, 6, 18, tzinfo=UTC),
            title_property_id="titleProp",
            active=True,
            archived=False,
        )
        session.add_all((calendar, assessment))
        session.flush()
        course_id = str(course.id)
        assessment_id = str(assessment.id)
    return SQLAlchemyAcademicPlannerStore(engine), course_id, assessment_id


def test_catalog_search_returns_only_opaque_writable_targets() -> None:
    store, course_id, assessment_id = _store()

    as_of = datetime(2026, 9, 19, 16, tzinfo=UTC)
    course_result = store.search_courses(
        AcademicCourseQueryArgs(query="circuits"),
        as_of=as_of,
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )
    courses = course_result.results
    assessment_result = store.search_assessments(
        AcademicAssessmentQueryArgs(query="Lab report", course_id=course_id),
        as_of=as_of,
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert [(item.course_id, item.course_code) for item in courses] == [(course_id, "ECE 202")]
    assert [(item.assessment_id, item.title) for item in assessment_result.results] == [
        (assessment_id, "Lab report")
    ]
    assert course_result.envelope.applied_filters.text == "circuits"
    assert assessment_result.envelope.applied_filters.temporal.scope == "all"


def test_assessment_today_scope_filters_before_ordering_and_excludes_completed() -> None:
    store, course_id, _today_id = _store()
    today = datetime(2026, 9, 19, 16, tzinfo=UTC)
    with Session(store.engine) as session, session.begin():
        course = session.get(Course, UUID(course_id))
        assert course is not None
        rows = [
            Assessment(
                course_id=course.id,
                notion_id="old-assessment",
                title="Old lab",
                assessment_type="assignment",
                due_at=datetime(2026, 9, 12, 18, tzinfo=UTC),
                estimated_minutes=60,
                fact_state="confirmed",
                confidence=1.0,
                source_id="source123",
                notion_last_edited_at=today,
                title_property_id="titleProp",
                active=True,
                archived=False,
            ),
            Assessment(
                course_id=course.id,
                notion_id="tomorrow-assessment",
                title="Tomorrow lab",
                assessment_type="assignment",
                due_at=datetime(2026, 9, 20, 18, tzinfo=UTC),
                estimated_minutes=60,
                fact_state="confirmed",
                confidence=1.0,
                source_id="source123",
                notion_last_edited_at=today,
                title_property_id="titleProp",
                active=True,
                archived=False,
            ),
            Assessment(
                course_id=course.id,
                notion_id="completed-today-assessment",
                title="Completed today lab",
                assessment_type="assignment",
                due_at=datetime(2026, 9, 19, 18, tzinfo=UTC),
                estimated_minutes=60,
                fact_state="confirmed",
                confidence=1.0,
                source_id="source123",
                notion_last_edited_at=today,
                title_property_id="titleProp",
                completed=True,
                active=True,
                archived=False,
            ),
            Assessment(
                course_id=course.id,
                notion_id="today-assessment",
                title="Today lab",
                assessment_type="assignment",
                due_at=datetime(2026, 9, 19, 18, tzinfo=UTC),
                estimated_minutes=60,
                fact_state="confirmed",
                confidence=1.0,
                source_id="source123",
                notion_last_edited_at=today,
                title_property_id="titleProp",
                active=True,
                archived=False,
            ),
        ]
        session.add_all(rows)

    result = store.search_assessments(
        AcademicAssessmentQueryArgs(
            query="What are today's todos?",
            course_id=course_id,
            temporal=TemporalQuery(scope="today"),
            limit=20,
        ),
        as_of=datetime(2026, 9, 19, 23, 9, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert [item.title for item in result.results] == ["Today lab"]
    assert result.envelope.applied_filters.temporal.scope == "today"
    assert result.envelope.applied_filters.completion == "incomplete"
    assert result.envelope.has_more is False


def test_today_treats_all_day_as_local_date_and_filters_before_page_limit() -> None:
    store, course_id, _assessment_id = _store()
    observed_at = datetime(2026, 9, 19, 23, 9, tzinfo=UTC)
    with Session(store.engine) as session, session.begin():
        course = session.get(Course, UUID(course_id))
        assert course is not None
        historical = [
            Assessment(
                course_id=course.id,
                notion_id=f"historical-{index}",
                title=f"Historical {index:02d}",
                assessment_type="assignment",
                due_at=datetime(2026, 8, 1, 12, index, tzinfo=UTC),
                estimated_minutes=30,
                fact_state="confirmed",
                confidence=1.0,
                source_id="source123",
                notion_last_edited_at=observed_at,
                title_property_id="titleProp",
                active=True,
                archived=False,
            )
            for index in range(25)
        ]
        session.add_all(
            [
                *historical,
                Assessment(
                    course_id=course.id,
                    notion_id="all-day-today",
                    title="All-day today",
                    assessment_type="assignment",
                    due_at=datetime(2026, 9, 19, 0, tzinfo=UTC),
                    is_all_day=True,
                    estimated_minutes=30,
                    fact_state="confirmed",
                    confidence=1.0,
                    source_id="source123",
                    notion_last_edited_at=observed_at,
                    title_property_id="titleProp",
                    active=True,
                    archived=False,
                ),
                Assessment(
                    course_id=course.id,
                    notion_id="timed-previous-local-day",
                    title="Timed previous local day",
                    assessment_type="assignment",
                    due_at=datetime(2026, 9, 19, 0, tzinfo=UTC),
                    is_all_day=False,
                    estimated_minutes=30,
                    fact_state="confirmed",
                    confidence=1.0,
                    source_id="source123",
                    notion_last_edited_at=observed_at,
                    title_property_id="titleProp",
                    active=True,
                    archived=False,
                ),
            ]
        )

    result = store.search_assessments(
        AcademicAssessmentQueryArgs(
            query="today",
            course_id=course_id,
            temporal=TemporalQuery(scope="today"),
            limit=20,
        ),
        as_of=observed_at,
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert [item.title for item in result.results] == ["All-day today"]
    assert result.results[0].due_date_local.isoformat() == "2026-09-19"
    assert result.results[0].due_at_local == "2026-09-19"


def test_mutation_targets_fail_closed_for_unknown_ids() -> None:
    store, course_id, assessment_id = _store()

    course = store.resolve_course_mutation_target(course_id)
    assessment = store.resolve_assessment_mutation_target(assessment_id)

    assert course is not None
    assert course.data_source_id == "source123"
    assert assessment is not None
    assert assessment.page_id == "assessment-page"
    assert store.resolve_course_mutation_target(str(uuid4())) is None
    assert store.resolve_assessment_mutation_target(str(uuid4())) is None


def test_nightly_candidate_loader_filters_trusted_fresh_course_items_and_carries_ranges() -> None:
    store, course_id, _assessment_id = _store()
    as_of = datetime(2026, 9, 20, 1, 0, tzinfo=UTC)
    timed_start = datetime(2026, 9, 20, 0, 30, tzinfo=UTC)
    timed_end = timed_start + timedelta(hours=1)
    with Session(store.engine) as session, session.begin():
        course = session.get(Course, UUID(course_id))
        assert course is not None
        calendar = session.query(AcademicCourseCalendar).filter_by(course_id=course.id).one()
        calendar.last_synced_at = as_of
        included = Assessment(
            course_id=course.id,
            notion_id="nightly-review-page",
            title="Review op amps",
            assessment_type="task",
            due_at=timed_start,
            ends_at=timed_end,
            estimated_minutes=60,
            fact_state="confirmed",
            confidence=1.0,
            source_id="source123",
            source_scope="notion:source123",
            notion_last_edited_at=as_of,
            title_property_id="titleProp",
            active=True,
            archived=False,
            calendar_semantic_status="valid",
            calendar_semantic_overview="Review practice problems.",
            calendar_semantic_description="Owner planned a review session.",
            calendar_semantic_evidence_ids=["frag-1"],
        )
        foreign_source = Assessment(
            course_id=course.id,
            notion_id="foreign-page",
            title="Foreign source",
            assessment_type="task",
            due_at=timed_start,
            estimated_minutes=60,
            fact_state="confirmed",
            confidence=1.0,
            source_id="other-source",
            notion_last_edited_at=as_of,
            title_property_id="titleProp",
            active=True,
            archived=False,
        )
        misc_course = Course(
            notion_id="misc-page",
            course_code="misc",
            title="misc",
            term="Fall 2026",
            active=True,
        )
        stale_course = Course(
            notion_id="stale-page",
            course_code="ECE 999",
            title="Stale Source",
            term="Fall 2026",
            active=True,
        )
        session.add_all((included, foreign_source, misc_course, stale_course))
        session.flush()
        session.add_all(
            (
                AcademicCourseCalendar(
                    course_id=misc_course.id,
                    course_page_id="misc-page",
                    child_database_id="misc-db",
                    child_data_source_id="misc-source",
                    title_property_id="titleProp",
                    date_property_id="dateProp",
                    discovery_status="valid",
                    last_synced_at=as_of,
                ),
                AcademicCourseCalendar(
                    course_id=stale_course.id,
                    course_page_id="stale-page",
                    child_database_id="stale-db",
                    child_data_source_id="stale-source",
                    title_property_id="titleProp",
                    date_property_id="dateProp",
                    discovery_status="valid",
                    last_synced_at=as_of - timedelta(days=3),
                ),
                Assessment(
                    course_id=misc_course.id,
                    notion_id="misc-nightly",
                    title="Misc task",
                    assessment_type="task",
                    due_at=timed_start,
                    estimated_minutes=60,
                    fact_state="confirmed",
                    confidence=1.0,
                    source_id="misc-source",
                    notion_last_edited_at=as_of,
                    title_property_id="titleProp",
                    active=True,
                    archived=False,
                ),
                Assessment(
                    course_id=stale_course.id,
                    notion_id="stale-nightly",
                    title="Stale task",
                    assessment_type="task",
                    due_at=timed_start,
                    estimated_minutes=60,
                    fact_state="confirmed",
                    confidence=1.0,
                    source_id="stale-source",
                    notion_last_edited_at=as_of,
                    title_property_id="titleProp",
                    active=True,
                    archived=False,
                ),
            )
        )

    candidates = store.load_nightly_current_day_assessment_candidates(
        local_date=date(2026, 9, 19),
        as_of=as_of,
        timezone="America/Toronto",
    )

    assert [item.title for item in candidates] == ["Review op amps"]
    assert candidates[0].starts_at == timed_start
    assert candidates[0].ends_at == timed_end
    assert candidates[0].local_date == date(2026, 9, 19)
    assert candidates[0].source_id == "source123"
    assert candidates[0].semantic_status == "valid"
    assert candidates[0].semantic_evidence_fragment_ids == ("frag-1",)


def test_update_assessment_validation_preserves_existing_range_and_all_day_precision() -> None:
    existing_start = datetime(2026, 9, 19, 0, tzinfo=UTC)
    existing = AcademicAssessmentOption(
        assessment_id="assessment-1",
        course_id="course-1",
        course_code="ECE 202",
        title="Review feedback",
        due_at=existing_start,
        ends_at=existing_start + timedelta(days=2),
        is_all_day=True,
        assessment_type=AssessmentType.TASK,
        expected_last_edited_at=datetime(2026, 9, 18, 12, tzinfo=UTC),
    )
    new_start = datetime(2026, 9, 20, 0, tzinfo=UTC)

    changes, error = proposed_changes_from_calls(
        (
            UpdateAssessmentCall(
                tool="update_assessment",
                assessment_id=existing.assessment_id,
                due_at=new_start,
            ),
        ),
        known_courses={},
        known_assessments={existing.assessment_id: existing},
        now=datetime(2026, 9, 19, 23, tzinfo=UTC),
    )

    assert error is None
    assert len(changes) == 1
    assert changes[0].due_at == new_start
    assert changes[0].ends_at == new_start + timedelta(days=2)
    assert changes[0].is_all_day is True
