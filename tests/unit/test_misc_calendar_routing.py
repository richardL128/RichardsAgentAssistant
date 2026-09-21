from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.academic_planner.calendar_roles import (
    AcademicCalendarRole,
    academic_calendar_role,
    canonical_misc_task_title,
)
from app.agents.academic_planner.contracts import (
    AcademicAssessmentQueryArgs,
    AcademicCourseOption,
    AssessmentType,
    CreateMiscTaskCall,
)
from app.agents.academic_planner.discord_harness import NativeAcademicDiscordHandler
from app.agents.academic_planner.proposal_validation import proposed_changes_from_calls
from app.connectors.discord_gateway import DiscordAcademicMessageCreate
from app.db.academic import (
    AcademicRepository,
    AssessmentSourceTrace,
    CourseCalendarInput,
    SourceCitation,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import Base

NOW = datetime(2026, 9, 9, 14, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'misc.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _seed_calendar(
    engine,
    *,
    notion_id: str,
    title: str,
    active: bool = True,
    status: str = "valid",
):
    with Session(engine) as session, session.begin():
        course = AcademicRepository.upsert_course(
            session,
            notion_id=notion_id,
            course_code=title,
            title=title,
            term="unspecified",
            active=active,
        )
        AcademicRepository.upsert_course_calendar(
            session,
            calendar=CourseCalendarInput(
                course_id=course.id,
                course_page_id=notion_id,
                child_database_id=f"{notion_id}-database" if status == "valid" else None,
                child_data_source_id=f"{notion_id}-source" if status == "valid" else None,
                title_property_id="title-property" if status == "valid" else None,
                date_property_id="date-property" if status == "valid" else None,
                discovery_status=status,
                last_synced_at=NOW if status == "valid" else None,
            ),
        )
        return course.id


def test_misc_role_and_task_title_use_exact_normalized_reservation() -> None:
    assert academic_calendar_role("  MiSc  ") is AcademicCalendarRole.MISC
    assert academic_calendar_role("miscellaneous") is AcademicCalendarRole.COURSE
    assert academic_calendar_role("MISC 101") is AcademicCalendarRole.COURSE
    assert canonical_misc_task_title("Task — scrub the toilets") == "Task — scrub the toilets"
    assert canonical_misc_task_title("scrub the toilets") == "Task — scrub the toilets"
    assert canonical_misc_task_title("Taskmaster reminder") == "Task — Taskmaster reminder"


def test_store_resolves_only_active_valid_misc_calendar_and_labels_items(engine) -> None:
    misc_id = _seed_calendar(engine, notion_id="misc-row", title="  MiSc  ")
    _seed_calendar(engine, notion_id="inactive-misc", title="misc", active=False)
    _seed_calendar(engine, notion_id="broken-misc", title="misc", status="missing")
    due = NOW + timedelta(days=1)
    with Session(engine) as session, session.begin():
        AcademicRepository.upsert_assessment(
            session,
            notion_id="misc-task-1",
            course_id=misc_id,
            title="Task — scrub the toilets",
            assessment_type="task",
            due_at=due,
            grade_weight_percent=None,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
            trace=AssessmentSourceTrace(
                source_id="misc-row-source",
                source_scope="notion:misc-row-source",
                notion_last_edited_at=NOW,
                title_property_id="title-property",
            ),
        )

    store = SQLAlchemyAcademicPlannerStore(engine)
    matches = store.search_misc_courses()
    assert len(matches) == 1
    assert matches[0].course_id == str(misc_id)
    assert matches[0].calendar_role is AcademicCalendarRole.MISC
    tasks = store.search_assessments(
        AcademicAssessmentQueryArgs(query="toilets", course_id=str(misc_id)),
        as_of=NOW,
        timezone="America/Toronto",
        owner_scope="owner:channel",
    ).results
    assert len(tasks) == 1
    assert tasks[0].course_code.strip().casefold() == "misc"
    assert tasks[0].assessment_type is AssessmentType.TASK
    items = store.load_upcoming_calendar_items(occurrence=NOW, timezone="America/Toronto")
    assert len(items) == 1
    assert items[0]["source_area"] == "misc"
    assert items[0]["display_kind"] == "Task"


def test_store_returns_two_misc_rows_so_the_host_can_reject_duplicates(engine) -> None:
    _seed_calendar(engine, notion_id="misc-row-1", title="misc")
    _seed_calendar(engine, notion_id="misc-row-2", title="MISC")

    matches = SQLAlchemyAcademicPlannerStore(engine).search_misc_courses()

    assert len(matches) == 2
    assert all(item.calendar_role is AcademicCalendarRole.MISC for item in matches)


def test_misc_proposal_requires_verified_misc_role_and_canonicalizes_task() -> None:
    misc = AcademicCourseOption(
        course_id="misc-course",
        course_code="misc",
        title="misc",
        calendar_role=AcademicCalendarRole.MISC,
    )
    call = CreateMiscTaskCall(
        tool="create_misc_task",
        course_id=misc.course_id,
        title="scrub the toilets",
        due_at=NOW + timedelta(hours=8),
    )

    changes, error = proposed_changes_from_calls(
        (call,),
        known_courses={misc.course_id: misc},
        known_assessments={},
        now=NOW,
    )

    assert error is None
    assert len(changes) == 1
    assert changes[0].course_code == "misc"
    assert changes[0].title == "Task — scrub the toilets"
    assert changes[0].assessment_type is AssessmentType.TASK

    course = misc.model_copy(update={"calendar_role": AcademicCalendarRole.COURSE})
    changes, error = proposed_changes_from_calls(
        (call,),
        known_courses={course.course_id: course},
        known_assessments={},
        now=NOW,
    )
    assert changes == ()
    assert error == "I could not verify the reserved misc calendar for this task."


class _Gateway:
    def __init__(self, messages: Sequence[AIMessage]) -> None:
        self.messages = list(messages)
        self.tool_schemas: Sequence[Mapping[str, object]] = ()

    async def invoke_tools(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, object]],
    ) -> AIMessage:
        del messages
        self.tool_schemas = tools
        return self.messages.pop(0)


class _Runtime:
    async def ensure_ready(self) -> object:
        return object()


class _Syncer:
    async def sync(self, *, now: datetime):
        assert now == NOW
        return SimpleNamespace(status="succeeded", diagnostic_codes=())


class _Catalog:
    def __init__(self, misc: Sequence[AcademicCourseOption]) -> None:
        self.misc = tuple(misc)

    def search_misc_courses(self) -> Sequence[AcademicCourseOption]:
        return self.misc


class _Store:
    confirmation_ttl_hours = 24

    def __init__(self) -> None:
        self.proposals = []

    def save_discord_checkin(self, proposal, **kwargs):
        assert kwargs["owner_discord_user_id"] == "333333333333333333"
        self.proposals.append(proposal)
        return SimpleNamespace(status="created")


class _Progress:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def start(self, event: object | None = None) -> object:
        self.events.append(event)
        return object()

    async def update(self, event: object) -> None:
        self.events.append(event)

    async def finish_proposal_ready(self) -> None:
        self.events.append("proposal_ready")

    async def finish_completed(self) -> None:
        self.events.append("completed")

    async def finish_failed(self) -> None:
        self.events.append("failed")


class _Delivery:
    def __init__(self) -> None:
        self.progress = _Progress()
        self.responses: list[str] = []
        self.confirmations = []

    def create_progress_reporter(self, **kwargs):
        assert kwargs["root_event_id"] == "111111111111111111"
        return self.progress

    async def send_response(self, content: str, *, idempotency_key: str) -> object:
        assert idempotency_key
        self.responses.append(content)
        return object()

    async def send_confirmation(self, proposal, *, idempotency_key: str) -> object:
        assert idempotency_key
        self.confirmations.append(proposal)
        return object()


def _message() -> DiscordAcademicMessageCreate:
    return DiscordAcademicMessageCreate(
        message_id="111111111111111111",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        timestamp=NOW,
        content=SecretStr("scrub the toilets @6:00 pm tdy"),
    )


def _handler(gateway: _Gateway, catalog: _Catalog, store: _Store, delivery: _Delivery):
    return NativeAcademicDiscordHandler(
        store=store,
        delivery=delivery,
        allowed_channel_ids={"222222222222222222"},
        authorized_user_ids={"333333333333333333"},
        writer_provider=lambda: None,
        ollama_runtime=_Runtime(),
        agent_gateway=gateway,
        agent_catalog=catalog,
        assistant_user_id="444444444444444444",
        catalog_syncer=_Syncer(),
        catalog_sync_timeout_seconds=1,
    )


@pytest.mark.asyncio
async def test_discord_misc_chore_uses_host_resolved_target_and_confirmation() -> None:
    misc = AcademicCourseOption(
        course_id="opaque-misc-id",
        course_code="misc",
        title="misc",
        calendar_role=AcademicCalendarRole.MISC,
    )
    gateway = _Gateway(
        (
            AIMessage(
                content="I'll put that personal chore on your misc calendar for review.",
                tool_calls=(
                    {
                        "id": "misc-1",
                        "name": "create_misc_task",
                        "args": {
                            "title": "scrub the toilets",
                            "due_at": "2026-09-09T18:00:00",
                        },
                    },
                ),
            ),
            AIMessage(content="The misc task is ready for your confirmation."),
        )
    )
    store = _Store()
    delivery = _Delivery()

    result = await _handler(gateway, _Catalog((misc,)), store, delivery)(_message())

    assert result.status == "handled"
    assert len(delivery.confirmations) == 1
    change = delivery.confirmations[0].changes[0]
    assert change.course_id == "opaque-misc-id"
    assert change.course_code == "misc"
    assert change.title == "Task — scrub the toilets"
    assert change.due_at == datetime(2026, 9, 9, 22, tzinfo=UTC)
    assert change.assessment_type is AssessmentType.TASK
    misc_schema = next(
        schema
        for schema in gateway.tool_schemas
        if schema["function"]["name"] == "create_misc_task"  # type: ignore[index]
    )
    parameters = misc_schema["function"]["parameters"]  # type: ignore[index]
    assert "course_id" not in parameters["properties"]  # type: ignore[index]
    assert {"phase": "tool_activity", "tool_activity": "proposal_drafting"} in (
        delivery.progress.events
    )
    assert delivery.progress.events[-1] == "proposal_ready"


@pytest.mark.asyncio
async def test_missing_misc_calendar_fails_closed_without_a_proposal() -> None:
    gateway = _Gateway(
        (
            AIMessage(
                content="I'll check the misc calendar target.",
                tool_calls=(
                    {
                        "id": "misc-1",
                        "name": "create_misc_task",
                        "args": {
                            "title": "scrub the toilets",
                            "due_at": "2026-09-09T18:00:00",
                        },
                    },
                ),
            ),
            AIMessage(
                content=(
                    "I couldn't find a valid misc calendar, so I did not select another calendar."
                )
            ),
        )
    )
    store = _Store()
    delivery = _Delivery()

    result = await _handler(gateway, _Catalog(()), store, delivery)(_message())

    assert result.status == "handled"
    assert store.proposals == []
    assert delivery.confirmations == []
    assert any("No active `misc` row" in response for response in delivery.responses)
    assert delivery.progress.events[-1] == "completed"
