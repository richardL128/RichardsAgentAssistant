from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

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

    courses = store.search_courses("ece202")
    assessments = store.search_assessments("lab", course_id)

    assert [(item.course_id, item.course_code) for item in courses] == [(course_id, "ECE 202")]
    assert [(item.assessment_id, item.title) for item in assessments] == [
        (assessment_id, "Lab report")
    ]


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
