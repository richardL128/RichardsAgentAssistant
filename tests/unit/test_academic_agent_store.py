from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.academic_planner.calendar_roles import AcademicCalendarRole
from app.agents.academic_planner.contracts import (
    AcademicAssessmentOption,
    AcademicAssessmentQueryArgs,
    AcademicCalendarItemQueryArgs,
    AcademicCourseQueryArgs,
    AssessmentType,
    CalendarItemView,
    UpdateAssessmentCall,
)
from app.agents.academic_planner.proposal_validation import proposed_changes_from_calls
from app.agents.query_contracts import (
    CompletenessState,
    FreshnessState,
    QueryResultKind,
    TemporalQuery,
    TemporalScope,
)
from app.db.academic import SQLAlchemyAcademicPlannerStore, _canonical_action_item_domain_values
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


def _add_calendar_source(
    store: SQLAlchemyAcademicPlannerStore,
    *,
    course_code: str,
    title: str,
    source_id: str,
    last_synced_at: datetime | None,
    discovery_status: str = "valid",
) -> str:
    course = Course(
        notion_id=f"{source_id}-page",
        course_code=course_code,
        title=title,
        term="Fall 2026",
        active=True,
    )
    with Session(store.engine) as session, session.begin():
        session.add(course)
        session.flush()
        session.add(
            AcademicCourseCalendar(
                course_id=course.id,
                course_page_id=course.notion_id,
                child_database_id=f"{source_id}-db",
                child_data_source_id=source_id,
                title_property_id="titleProp",
                date_property_id="dateProp",
                discovery_status=discovery_status,
                diagnostic_code=(
                    None if discovery_status == "valid" else f"{discovery_status}_calendar"
                ),
                last_synced_at=last_synced_at,
            )
        )
        return str(course.id)


def _add_assessment(
    store: SQLAlchemyAcademicPlannerStore,
    *,
    course_id: str,
    source_id: str,
    notion_id: str,
    title: str,
    due_at: datetime,
    assessment_type: str = "assignment",
    is_all_day: bool = False,
) -> None:
    with Session(store.engine) as session, session.begin():
        course = session.get(Course, UUID(course_id))
        assert course is not None
        session.add(
            Assessment(
                course_id=course.id,
                notion_id=notion_id,
                title=title,
                assessment_type=assessment_type,
                domain="academic",
                item_kind=assessment_type,
                status="to_do",
                due_at=due_at,
                start_date=due_at.date() if is_all_day else None,
                start_at=None if is_all_day else due_at,
                date_precision="date" if is_all_day else "datetime",
                timezone=None if is_all_day else "America/Toronto",
                is_all_day=is_all_day,
                estimated_minutes=60,
                fact_state="confirmed",
                confidence=1.0,
                source_kind="notion_action_items",
                source_label="Notion Action Items",
                source_id=source_id,
                notion_data_source_id=source_id,
                notion_page_id=notion_id,
                notion_last_edited_at=datetime(2026, 9, 19, 16, tzinfo=UTC),
                title_property_id="titleProp",
                active=True,
                archived=False,
            )
        )


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


def test_broad_assessment_search_returns_partial_with_unavailable_sources() -> None:
    store, _course_id, assessment_id = _store()
    stale_course_id = _add_calendar_source(
        store,
        course_code="ECE 404",
        title="Unavailable Circuits",
        source_id="unavailable-source",
        last_synced_at=None,
    )
    _add_assessment(
        store,
        course_id=stale_course_id,
        source_id="unavailable-source",
        notion_id="unavailable-assessment",
        title="Should not leak",
        due_at=datetime(2026, 9, 9, 16, tzinfo=UTC),
    )

    result = store.search_assessments(
        AcademicAssessmentQueryArgs(query="", limit=20),
        as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert [item.assessment_id for item in result.results] == [assessment_id]
    assert result.envelope.completeness is CompletenessState.PARTIAL
    assert {item.state for item in result.envelope.freshness} == {
        FreshnessState.FRESH_COMPLETE,
        FreshnessState.UNAVAILABLE,
    }


def test_explicit_unavailable_course_is_required_and_returns_unavailable() -> None:
    store, _course_id, _assessment_id = _store()
    stale_course_id = _add_calendar_source(
        store,
        course_code="ECE 499",
        title="Required stale source",
        source_id="required-stale-source",
        last_synced_at=None,
    )
    _add_assessment(
        store,
        course_id=stale_course_id,
        source_id="required-stale-source",
        notion_id="required-stale-assessment",
        title="Required stale item",
        due_at=datetime(2026, 9, 19, 16, tzinfo=UTC),
    )

    result = store.search_assessments(
        AcademicAssessmentQueryArgs(course_id=stale_course_id),
        as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert result.results == ()
    assert result.envelope.completeness is CompletenessState.UNAVAILABLE
    assert result.envelope.freshness == (result.envelope.freshness[0],)
    assert result.envelope.freshness[0].source_id == "required-stale-source"
    assert result.envelope.freshness[0].state is FreshnessState.UNAVAILABLE


def test_empty_broad_partial_does_not_upgrade_to_complete() -> None:
    store, _course_id, _assessment_id = _store()
    _add_calendar_source(
        store,
        course_code="ECE 405",
        title="Unavailable empty source",
        source_id="unavailable-empty-source",
        last_synced_at=None,
    )

    result = store.search_assessments(
        AcademicAssessmentQueryArgs(query="no matching academic item", limit=20),
        as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert result.results == ()
    assert result.envelope.completeness is CompletenessState.PARTIAL


def test_all_unavailable_requested_sources_return_unavailable() -> None:
    store, course_id, _assessment_id = _store()
    with Session(store.engine) as session, session.begin():
        course = session.get(Course, UUID(course_id))
        assert course is not None
        calendar = session.query(AcademicCourseCalendar).filter_by(course_id=course.id).one()
        calendar.last_synced_at = None

    result = store.search_assessments(
        AcademicAssessmentQueryArgs(query="", limit=20),
        as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert result.results == ()
    assert result.envelope.completeness is CompletenessState.UNAVAILABLE
    assert [item.state for item in result.envelope.freshness] == [FreshnessState.UNAVAILABLE]


def test_assessment_search_pagination_uses_usable_sources() -> None:
    store, _course_id, _assessment_id = _store()
    second_course_id = _add_calendar_source(
        store,
        course_code="ECE 303",
        title="Signals",
        source_id="fresh-second-source",
        last_synced_at=datetime(2026, 9, 19, 16, tzinfo=UTC),
    )
    _add_assessment(
        store,
        course_id=second_course_id,
        source_id="fresh-second-source",
        notion_id="second-fresh-assessment",
        title="Second fresh item",
        due_at=datetime(2026, 9, 11, 16, tzinfo=UTC),
    )

    result = store.search_assessments(
        AcademicAssessmentQueryArgs(query="", limit=1),
        as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert len(result.results) == 1
    assert result.envelope.has_more is True
    assert result.envelope.completeness is CompletenessState.MORE_AVAILABLE
    assert result.envelope.next_cursor is not None


def test_unavailable_source_rows_do_not_consume_page_or_leak() -> None:
    store, course_id, _assessment_id = _store()
    bad_course_id = _add_calendar_source(
        store,
        course_code="AAA 101",
        title="Unavailable first in ordering",
        source_id="unavailable-first-source",
        last_synced_at=None,
    )
    for index in range(25):
        _add_assessment(
            store,
            course_id=bad_course_id,
            source_id="unavailable-first-source",
            notion_id=f"bad-page-{index}",
            title=f"Page crowding item {index:02d}",
            due_at=datetime(2026, 9, 1, 12, index, tzinfo=UTC),
        )
    _add_assessment(
        store,
        course_id=course_id,
        source_id="source123",
        notion_id="good-page-after-bad-source",
        title="Page valid item",
        due_at=datetime(2026, 9, 20, 16, tzinfo=UTC),
    )

    result = store.search_assessments(
        AcademicAssessmentQueryArgs(query="Page", limit=1),
        as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert [item.title for item in result.results] == ["Page valid item"]
    assert result.envelope.has_more is False
    assert result.envelope.completeness is CompletenessState.PARTIAL
    assert all(item.course_id != bad_course_id for item in result.results)


def test_explicit_role_without_source_is_reported_unavailable() -> None:
    store, _course_id, _assessment_id = _store()

    result = store.search_assessments(
        AcademicAssessmentQueryArgs(roles=(AcademicCalendarRole.LEARN,), limit=20),
        as_of=datetime(2026, 9, 19, 16, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert result.results == ()
    assert result.envelope.completeness is CompletenessState.UNAVAILABLE
    assert result.envelope.freshness[0].source_id == "role:learn"


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
            temporal=TemporalQuery(scope=TemporalScope.TODAY),
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


def test_calendar_item_semantic_views_use_persisted_types_not_titles() -> None:
    store, course_id, _assessment_id = _store()
    observed_at = datetime(2026, 9, 19, 14, tzinfo=UTC)
    with Session(store.engine) as session, session.begin():
        course = session.get(Course, UUID(course_id))
        assert course is not None
        session.add_all(
            [
                Assessment(
                    course_id=course.id,
                    notion_id="semantic-task",
                    title="Meeting-looking actionable work",
                    assessment_type="task",
                    due_at=datetime(2026, 9, 19, 16, tzinfo=UTC),
                    notion_last_edited_at=observed_at,
                    active=True,
                    archived=False,
                ),
                Assessment(
                    course_id=course.id,
                    notion_id="semantic-assignment",
                    title="Lecture-looking assignment",
                    assessment_type="assignment",
                    due_at=datetime(2026, 9, 19, 17, tzinfo=UTC),
                    notion_last_edited_at=observed_at,
                    active=True,
                    archived=False,
                ),
                Assessment(
                    course_id=course.id,
                    notion_id="semantic-tutorial",
                    title="Homework-looking tutorial",
                    assessment_type="tutorial",
                    due_at=datetime(2026, 9, 19, 18, tzinfo=UTC),
                    notion_last_edited_at=observed_at,
                    active=True,
                    archived=False,
                ),
                Assessment(
                    course_id=course.id,
                    notion_id="semantic-event",
                    title="Quiz-looking appointment",
                    assessment_type="event",
                    due_at=datetime(2026, 9, 19, 19, tzinfo=UTC),
                    notion_last_edited_at=observed_at,
                    active=True,
                    archived=False,
                ),
            ]
        )

    def search(view: CalendarItemView):
        return store.search_calendar_items(
            AcademicCalendarItemQueryArgs(
                view=view,
                temporal=TemporalQuery(scope=TemporalScope.TODAY),
                course_id=course_id,
                limit=20,
            ),
            as_of=observed_at,
            timezone="America/Toronto",
            owner_scope=f"owner:channel:{view.value}",
        )

    tasks = search(CalendarItemView.TASKS)
    schedule = search(CalendarItemView.SCHEDULE)
    agenda = search(CalendarItemView.AGENDA)

    assert [item.assessment_type for item in tasks.results] == [
        AssessmentType.TASK,
        AssessmentType.ASSIGNMENT,
    ]
    assert [item.assessment_type for item in schedule.results] == [
        AssessmentType.TUTORIAL,
        AssessmentType.EVENT,
    ]
    assert {item.assessment_type for item in agenda.results} == {
        AssessmentType.TASK,
        AssessmentType.ASSIGNMENT,
        AssessmentType.TUTORIAL,
        AssessmentType.EVENT,
    }
    assert tasks.envelope.result_kind is QueryResultKind.CALENDAR_ITEMS
    assert tasks.envelope.applied_filters.view == "tasks"


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
            temporal=TemporalQuery(scope=TemporalScope.TODAY),
            limit=20,
        ),
        as_of=observed_at,
        timezone="America/Toronto",
        owner_scope="owner:channel",
    )

    assert [item.title for item in result.results] == ["All-day today"]
    assert result.results[0].due_date_local.isoformat() == "2026-09-19"
    assert result.results[0].due_at_local == "2026-09-19"


def test_calendar_date_only_sep_22_is_absent_from_sep_23() -> None:
    store, course_id, _assessment_id = _store()
    _add_assessment(
        store,
        course_id=course_id,
        source_id="source123",
        notion_id="sep-22-date-only",
        title="Date-only Sep 22",
        due_at=datetime(2026, 9, 22, 0, tzinfo=UTC),
        is_all_day=True,
    )

    def search(day: date):
        local_midnight_utc = datetime(day.year, day.month, day.day, 4, tzinfo=UTC)
        return store.search_calendar_items(
            AcademicCalendarItemQueryArgs(
                view=CalendarItemView.ALL_ITEMS,
                course_id=course_id,
                temporal=TemporalQuery(
                    scope=TemporalScope.DATE_RANGE,
                    start_at=local_midnight_utc,
                    end_at=local_midnight_utc + timedelta(days=1),
                ),
                limit=20,
            ),
            as_of=datetime(2026, 9, 22, 12, tzinfo=UTC),
            timezone="America/Toronto",
            owner_scope=f"owner:channel:{day.isoformat()}",
        )

    sep_22 = search(date(2026, 9, 22))
    sep_23 = search(date(2026, 9, 23))

    assert "Date-only Sep 22" in {item.title for item in sep_22.results}
    assert "Date-only Sep 22" not in {item.title for item in sep_23.results}
    sep_22_item = next(item for item in sep_22.results if item.title == "Date-only Sep 22")
    assert sep_22_item.temporal is not None
    assert getattr(sep_22_item.temporal, "start_date") == date(2026, 9, 22)
    assert getattr(sep_22_item.temporal, "start_at", None) is None
    assert sep_22.envelope.applied_filters.roles == ("academic",)


def test_calendar_timed_utc_crossing_uses_local_date_window() -> None:
    store, course_id, _assessment_id = _store()
    _add_assessment(
        store,
        course_id=course_id,
        source_id="source123",
        notion_id="sep-22-late-timed",
        title="Timed late Sep 22",
        due_at=datetime(2026, 9, 23, 3, 30, tzinfo=UTC),
    )

    local_sep_22_start = datetime(2026, 9, 22, 4, tzinfo=UTC)
    local_sep_23_start = local_sep_22_start + timedelta(days=1)

    sep_22 = store.search_calendar_items(
        AcademicCalendarItemQueryArgs(
            view=CalendarItemView.ALL_ITEMS,
            course_id=course_id,
            temporal=TemporalQuery(
                scope=TemporalScope.DATE_RANGE,
                start_at=local_sep_22_start,
                end_at=local_sep_23_start,
            ),
            limit=20,
        ),
        as_of=datetime(2026, 9, 22, 12, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel:timed-sep-22",
    )
    sep_23 = store.search_calendar_items(
        AcademicCalendarItemQueryArgs(
            view=CalendarItemView.ALL_ITEMS,
            course_id=course_id,
            temporal=TemporalQuery(
                scope=TemporalScope.DATE_RANGE,
                start_at=local_sep_23_start,
                end_at=local_sep_23_start + timedelta(days=1),
            ),
            limit=20,
        ),
        as_of=datetime(2026, 9, 23, 12, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel:timed-sep-23",
    )

    assert "Timed late Sep 22" in {item.title for item in sep_22.results}
    assert "Timed late Sep 22" not in {item.title for item in sep_23.results}
    sep_22_item = next(item for item in sep_22.results if item.title == "Timed late Sep 22")
    assert sep_22_item.temporal is not None
    assert getattr(sep_22_item.temporal, "start_at").isoformat() == "2026-09-23T03:30:00+00:00"


def test_calendar_item_search_preserves_partial_invalid_source_row() -> None:
    store, course_id, _assessment_id = _store()
    malformed_course_id = _add_calendar_source(
        store,
        course_code="ECE 406",
        title="Malformed Calendar",
        source_id="malformed-source",
        last_synced_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
        discovery_status="malformed",
    )
    _add_assessment(
        store,
        course_id=malformed_course_id,
        source_id="malformed-source",
        notion_id="malformed-hidden-item",
        title="Malformed hidden item",
        due_at=datetime(2026, 9, 22, 16, tzinfo=UTC),
    )
    _add_assessment(
        store,
        course_id=course_id,
        source_id="source123",
        notion_id="valid-visible-item",
        title="Valid visible item",
        due_at=datetime(2026, 9, 22, 15, tzinfo=UTC),
    )

    result = store.search_calendar_items(
        AcademicCalendarItemQueryArgs(
            view=CalendarItemView.ALL_ITEMS,
            temporal=TemporalQuery(
                scope=TemporalScope.DATE_RANGE,
                start_at=datetime(2026, 9, 22, 4, tzinfo=UTC),
                end_at=datetime(2026, 9, 23, 4, tzinfo=UTC),
            ),
            limit=20,
        ),
        as_of=datetime(2026, 9, 22, 12, tzinfo=UTC),
        timezone="America/Toronto",
        owner_scope="owner:channel:partial-invalid",
    )

    assert "Valid visible item" in {item.title for item in result.results}
    assert "Malformed hidden item" not in {item.title for item in result.results}
    assert result.envelope.completeness is CompletenessState.PARTIAL
    malformed_freshness = next(
        item for item in result.envelope.freshness if item.source_id == "malformed-source"
    )
    assert malformed_freshness.state is FreshnessState.UNAVAILABLE
    assert "malformed_calendar" in malformed_freshness.diagnostic_codes
    assert "discovery_malformed" in malformed_freshness.diagnostic_codes


def test_canonical_action_item_domain_normalization_retains_career() -> None:
    assert _canonical_action_item_domain_values(("academic", "career", "ACADEMIC")) == (
        "academic",
        "career",
    )


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
