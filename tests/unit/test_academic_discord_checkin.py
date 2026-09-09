from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.academic_planner.agent_clarification import (
    AcademicAgentClarificationService,
)
from app.agents.academic_planner.contracts import (
    AcademicAgentDecision,
    AcademicAgentWireDecision,
    AcademicAssessmentOption,
    AcademicCourseOption,
    AcademicRequestRouteDecision,
    ArchiveAssessmentCall,
    AssessmentType,
    CheckinProposal,
    CreateAssessmentCall,
    ProposedChange,
    SearchAssessmentsCall,
    SearchCoursesCall,
    UserCreatableAssessmentType,
)
from app.agents.academic_planner.discord_checkin import (
    AcademicDiscordCheckinHandler,
    parse_academic_command,
)
from app.artifacts.store import ArtifactStore
from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    DiscordAcademicResponseDelivery,
    _academic_proposal_preview,
)
from app.connectors.discord_gateway import (
    DiscordAcademicMessageCreate,
    DiscordClarificationCallbackResult,
    DiscordClarificationInteraction,
    DiscordGatewayListener,
)
from app.db.academic import SQLAlchemyAcademicPlannerStore
from app.db.models import (
    AcademicCheckIn,
    AcademicDiscourseSession,
    AcademicDiscourseTurn,
    AcademicProposedChange,
    AuditEvent,
    Base,
    Delivery,
)

CHANNEL = "987654321012345678"
OWNER = "123456789012345678"
ASSISTANT = "777777777777777777"


class _ReadyRuntime:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls = 0
        self._events = events

    async def ensure_ready(self) -> object:
        self.calls += 1
        if self._events is not None:
            self._events.append("ready")
        return object()


class _CalendarSemanticRouter:
    async def invoke_structured(self, *, prompt, response_model):
        assert response_model is AcademicRequestRouteDecision
        payload = json.loads(prompt.partition("\n")[2])
        return SimpleNamespace(
            output=AcademicRequestRouteDecision(
                calendar_request=payload["message_untrusted"],
            )
        )


@pytest.fixture
def engine() -> Iterator[Engine]:
    value = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(value)
    yield value
    value.dispose()


def _message(
    message_id: str, content: str, *, author_id: str = OWNER
) -> DiscordAcademicMessageCreate:
    mentions = (ASSISTANT,) if re.search(rf"<@!?{ASSISTANT}>", content) is not None else ()
    return DiscordAcademicMessageCreate(
        message_id=message_id,
        channel_id=CHANNEL,
        author_id=author_id,
        timestamp=datetime(2026, 9, 6, 22, tzinfo=UTC),
        content=SecretStr(content),
        mentioned_user_ids=mentions,
    )


def _handler(
    engine: Engine,
    client: httpx.AsyncClient,
    *,
    writer: object | None = None,
    agent_gateway: object | None = None,
    semantic_router_gateway: object | None = None,
    agent_catalog: object | None = None,
    assistant_user_id: str | None = None,
    memory_service: object | None = None,
    ollama_runtime: object | None = None,
    agent_clarification_service: AcademicAgentClarificationService | None = None,
) -> AcademicDiscordCheckinHandler:
    adapter = DiscordAcademicPlannerAdapter(
        token=SecretStr("test-token"),
        allowed_channel_ids={CHANNEL},
        client=client,
    )
    return AcademicDiscordCheckinHandler(
        store=SQLAlchemyAcademicPlannerStore(engine),
        delivery=DiscordAcademicResponseDelivery(
            engine=engine,
            channel_id=CHANNEL,
            adapter=adapter,
        ),
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={OWNER},
        writer_provider=lambda: writer,  # type: ignore[return-value]
        ollama_runtime=(ollama_runtime or _ReadyRuntime()),  # type: ignore[arg-type]
        agent_gateway=agent_gateway,  # type: ignore[arg-type]
        semantic_router_gateway=(
            semantic_router_gateway
            if semantic_router_gateway is not None
            else _CalendarSemanticRouter()
            if agent_gateway is not None
            else None
        ),
        agent_catalog=agent_catalog,  # type: ignore[arg-type]
        assistant_user_id=assistant_user_id,
        memory_service=memory_service,  # type: ignore[arg-type]
        agent_clarification_service=agent_clarification_service,
    )


def _seed_pending_assignment_proposal(engine: Engine, external_event_id: str) -> uuid.UUID:
    proposal_id = uuid.uuid5(uuid.NAMESPACE_URL, f"lifeagent-test:{external_event_id}")
    SQLAlchemyAcademicPlannerStore(engine).save_discord_checkin(
        CheckinProposal(
            proposal_id=proposal_id,
            confirmation_event=f"confirm {proposal_id}",
            changes=(
                ProposedChange(
                    field="create_assessment",
                    value="Assignment \N{EM DASH} Seeded proposal",
                    course_id="course-222",
                    course_code="ECE 222",
                    title="Assignment \N{EM DASH} Seeded proposal",
                    due_at=datetime(2026, 9, 20, 21, tzinfo=UTC),
                    assessment_type=AssessmentType.ASSIGNMENT,
                ),
            ),
            expires_at=datetime(2026, 9, 7, 22, tzinfo=UTC),
        ),
        external_event_id=external_event_id,
        channel=CHANNEL,
        received_at=datetime(2026, 9, 6, 22, tzinfo=UTC),
    )
    with Session(engine) as session:
        row = session.scalar(select(AcademicProposedChange))
        assert row is not None
        return uuid.UUID(row.target_id)


class _UnusedClarificationHandler:
    async def __call__(
        self, interaction: DiscordClarificationInteraction
    ) -> DiscordClarificationCallbackResult:
        raise AssertionError(f"unexpected clarification interaction {interaction.interaction_id}")


def test_study_session_preview_is_toronto_local_and_complete() -> None:
    proposal_id = "11111111-1111-4111-8111-111111111111"
    content = _academic_proposal_preview(
        SimpleNamespace(
            proposal_id=proposal_id,
            changes=(
                SimpleNamespace(
                    field="create_assessment",
                    assessment_type=AssessmentType.STUDYING_BLOCK,
                    course_id="opaque-course-id",
                    course_code="ECE 250",
                    title="Studying Block — Race conditions",
                    due_at=datetime(2026, 9, 10, 23, 0, tzinfo=UTC),
                    ends_at=datetime(2026, 9, 10, 23, 45, tzinfo=UTC),
                ),
            ),
            expires_at=None,
        )
    )

    assert (
        "- Create `Studying Block — Race conditions` in ECE 250, "
        "September 10, 2026, 7:00\N{EN DASH}7:45 PM America/Toronto (45 minutes)."
    ) in content
    assert "opaque-course-id" not in content
    assert f"Confirm exactly: confirm {proposal_id}" in content
    assert f"Reject exactly: reject {proposal_id}" in content


def test_study_session_preview_duration_uses_elapsed_time_across_dst_fallback() -> None:
    content = _academic_proposal_preview(
        SimpleNamespace(
            proposal_id="11111111-1111-4111-8111-111111111111",
            changes=(
                SimpleNamespace(
                    field="create_assessment",
                    assessment_type=AssessmentType.STUDYING_BLOCK,
                    course_id="course-ece250",
                    course_code="ECE 250",
                    title="Studying Block — DST review",
                    due_at=datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                    ends_at=datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
                ),
            ),
            expires_at=None,
        )
    )

    assert (
        "- Create `Studying Block \N{EM DASH} DST review` in ECE 250, "
        "November 1, 2026, 1:30\N{EN DASH}1:30 AM America/Toronto (60 minutes)."
    ) in content


@pytest.mark.asyncio
async def test_agent_absent_fails_closed_without_deterministic_fallback(
    engine: Engine,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "111111111111111111"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            assistant_user_id=ASSISTANT,
        )(
            _message(
                "222222222222222222",
                f"<@{ASSISTANT}> completed assignment-page-1",
            )
        )

    assert result.status == "failed"
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["enforce_nonce"] is True
    assert body["allowed_mentions"] == {"parse": []}
    assert "Qwen semantic planning is not configured" in body["content"]
    with Session(engine) as session:
        assert session.scalar(select(AcademicCheckIn)) is None
        assert session.scalar(select(AcademicProposedChange)) is None


@pytest.mark.asyncio
async def test_gateway_to_repository_path_creates_one_model_planned_proposal(
    engine: Engine,
) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.decisions = iter(
                (
                    AcademicAgentDecision(
                        tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 222"),)
                    ),
                    AcademicAgentDecision(
                        tool_calls=(
                            CreateAssessmentCall(
                                tool="create_assessment",
                                course_id="course-222",
                                title="Assignment 1",
                                due_at=datetime(2026, 9, 20, 21, tzinfo=UTC),
                                assessment_type=UserCreatableAssessmentType.ASSIGNMENT,
                            ),
                        )
                    ),
                )
            )

        async def invoke_structured(self, *, prompt, response_model):
            del prompt
            assert response_model is AcademicAgentWireDecision
            return SimpleNamespace(output=next(self.decisions))

    class Catalog:
        def search_courses(self, query: str):
            assert query == "ECE 222"
            return (
                AcademicCourseOption(
                    course_id="course-222",
                    course_code="ECE 222",
                    title="Signals",
                ),
            )

        def search_assessments(self, query: str, course_id: str | None = None):
            del query, course_id
            return ()

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": str(111111111111111111 + len(requests))})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        message_handler = _handler(
            engine,
            client,
            agent_gateway=Gateway(),
            agent_catalog=Catalog(),
            assistant_user_id=ASSISTANT,
        )
        listener = DiscordGatewayListener(
            token=SecretStr("test-token"),
            api_base_url="https://discord.com/api/v10",
            allowed_channel_ids={CHANNEL},
            authorized_user_ids={OWNER},
            clarification_enqueuer=_UnusedClarificationHandler(),
            message_content_enabled=True,
            message_handler=message_handler,
        )
        payload = {
            "t": "MESSAGE_CREATE",
            "d": {
                "id": "232323232323232323",
                "channel_id": CHANNEL,
                "author": {"id": OWNER, "bot": False},
                "timestamp": "2026-09-06T22:00:00Z",
                "content": f"<@{ASSISTANT}> schedule an ECE 222 assignment for Sep 20",
                "mentions": [{"id": ASSISTANT}],
            },
        }
        assert await listener.handle_gateway_payload(payload) == "handled"
        assert await listener.handle_gateway_payload(payload) == "duplicate"
        await listener.drain_message_tasks()

    with Session(engine) as session:
        assert len(list(session.scalars(select(AcademicCheckIn)))) == 1
        assert len(list(session.scalars(select(AcademicProposedChange)))) == 1
        assert len(list(session.scalars(select(Delivery)))) >= 1


@pytest.mark.asyncio
async def test_semantic_not_applicable_message_creates_no_write_capable_proposal(
    engine: Engine,
) -> None:
    private_text = "I did a bunch of secret unstructured work today"

    class Gateway:
        async def invoke_structured(self, *, prompt, response_model):
            del prompt
            assert response_model is AcademicAgentWireDecision
            return SimpleNamespace(output=AcademicAgentDecision(not_applicable=True))

    class Catalog:
        def search_courses(self, query: str):
            del query
            return ()

        def search_assessments(self, query: str, course_id: str | None = None):
            del query, course_id
            return ()

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": str(111111111111111111 + len(requests))})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            agent_gateway=Gateway(),
            agent_catalog=Catalog(),
            assistant_user_id=ASSISTANT,
        )(
            _message(
                "333333333333333333",
                f"<@{ASSISTANT}> {private_text}",
            )
        )

    assert result.status == "handled"
    with Session(engine) as session:
        assert session.scalar(select(AcademicCheckIn)) is None
        assert session.scalar(select(AcademicProposedChange)) is None
    contents = [
        json.loads(request.content)["content"] for request in requests if request.method == "POST"
    ]
    assert any("could not identify a supported academic calendar" in item for item in contents)


@pytest.mark.asyncio
async def test_semantic_calendar_query_delivers_grounded_answer_without_proposal(
    engine: Engine,
) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.decisions = iter(
                (
                    AcademicAgentDecision(
                        tool_calls=(
                            SearchAssessmentsCall(
                                tool="search_assessments",
                                query="to dos tomorrow",
                            ),
                        )
                    ),
                    AcademicAgentDecision(
                        answer="Tomorrow you have Studying Block — race conditions at 7:00 PM."
                    ),
                )
            )

        async def invoke_structured(self, *, prompt, response_model):
            del prompt
            assert response_model is AcademicAgentWireDecision
            return SimpleNamespace(output=next(self.decisions))

    class Catalog:
        def search_courses(self, query: str):
            del query
            return ()

        def search_assessments(self, query: str, course_id: str | None = None):
            del query, course_id
            return (
                AcademicAssessmentOption(
                    assessment_id="assessment-study",
                    course_id="course-250",
                    course_code="ECE 250",
                    title="Studying Block — race conditions",
                    due_at=datetime(2026, 9, 7, 23, tzinfo=UTC),
                    assessment_type=AssessmentType.STUDYING_BLOCK,
                    expected_last_edited_at=datetime(2026, 9, 6, 18, tzinfo=UTC),
                ),
            )

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": str(111111111111111111 + len(requests))})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            agent_gateway=Gateway(),
            agent_catalog=Catalog(),
            assistant_user_id=ASSISTANT,
        )(_message("343434343434343434", f"<@{ASSISTANT}> what are my to dos tmr"))

    assert result.status == "handled"
    with Session(engine) as session:
        assert session.scalar(select(AcademicCheckIn)) is None
        assert session.scalar(select(AcademicProposedChange)) is None
    contents = [
        json.loads(request.content)["content"] for request in requests if request.method == "POST"
    ]
    assert any("race conditions at 7:00 PM" in item for item in contents)
    assert all("assessment-study" not in item for item in contents)


@pytest.mark.asyncio
async def test_qwen_agent_batches_create_and_archive_from_one_mentioned_message(
    engine: Engine,
) -> None:
    class Gateway:
        def __init__(self, events: list[str]) -> None:
            self.events = events
            self.decisions = iter(
                (
                    AcademicAgentDecision(
                        tool_calls=(
                            SearchCoursesCall(tool="search_courses", query="ECE 202"),
                            SearchCoursesCall(tool="search_courses", query="ECE 250"),
                        )
                    ),
                    AcademicAgentDecision(
                        tool_calls=(
                            SearchAssessmentsCall(
                                tool="search_assessments",
                                query="old assignment",
                                course_id="course-250",
                            ),
                        )
                    ),
                    AcademicAgentDecision(
                        tool_calls=(
                            CreateAssessmentCall(
                                tool="create_assessment",
                                course_id="course-202",
                                title="New assignment",
                                due_at=datetime(2026, 9, 20, 21, tzinfo=UTC),
                                assessment_type=AssessmentType.ASSIGNMENT,
                            ),
                            ArchiveAssessmentCall(
                                tool="archive_assessment",
                                assessment_id="assessment-250",
                            ),
                        )
                    ),
                )
            )
            self.prompts: list[str] = []

        async def invoke_structured(self, *, prompt, response_model):
            self.events.append("qwen")
            self.prompts.append(prompt)
            return SimpleNamespace(output=next(self.decisions))

    class Catalog:
        def search_courses(self, query: str):
            suffix = "202" if "202" in query else "250"
            return (
                AcademicCourseOption(
                    course_id=f"course-{suffix}",
                    course_code=f"ECE {suffix}",
                    title=f"Course {suffix}",
                ),
            )

        def search_assessments(self, query: str, course_id: str | None = None):
            assert query == "old assignment"
            assert course_id == "course-250"
            return (
                AcademicAssessmentOption(
                    assessment_id="assessment-250",
                    course_id="course-250",
                    course_code="ECE 250",
                    title="Old assignment",
                    due_at=datetime(2026, 9, 18, 21, tzinfo=UTC),
                    assessment_type=AssessmentType.ASSIGNMENT,
                    expected_last_edited_at=datetime(2026, 9, 6, 18, tzinfo=UTC),
                ),
            )

    requests: list[httpx.Request] = []
    events: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        events.append(
            "ack" if request.method == "POST" and len(requests) == 1 else request.method.casefold()
        )
        return httpx.Response(200, json={"id": str(111111111111111111 + len(requests))})

    gateway = Gateway(events)
    runtime = _ReadyRuntime(events)
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            agent_gateway=gateway,
            agent_catalog=Catalog(),
            assistant_user_id=ASSISTANT,
            ollama_runtime=runtime,
        )(
            _message(
                "343434343434343434",
                f"<@{ASSISTANT}> create an assignment in ECE 202 and delete "
                "old assignment in ECE 250",
            )
        )

    assert result.status == "handled"
    post_requests = [request for request in requests if request.method == "POST"]
    patch_requests = [request for request in requests if request.method == "PATCH"]
    assert len(post_requests) == 2
    assert patch_requests
    assert runtime.calls == 1
    assert events[:4] == ["ack", "ready", "patch", "qwen"]
    assert f"<@{ASSISTANT}>" not in gateway.prompts[0]
    with Session(engine) as session:
        proposal = session.scalar(select(AcademicProposedChange))
        assert proposal is not None
        assert [item["field"] for item in proposal.payload["changes"]] == [
            "create_assessment",
            "archive_assessment",
        ]
    progress_updates = "\n".join(
        json.loads(request.content)["content"] for request in patch_requests
    )
    first_progress = json.loads(patch_requests[0].content)["content"]
    assert first_progress.splitlines() == [
        "- Waking Qwen.",
        "- Qwen is interpreting your request (agent turn 1 of 10).",
    ]
    assert "Qwen is interpreting your request (agent turn 1 of 10)." in progress_updates
    assert "Qwen is interpreting your request (agent turn 3 of 10)." in progress_updates
    assert "Looking up matching courses." in progress_updates
    assert "Looking up matching assessments." in progress_updates
    assert "Validating a safe proposal." in progress_updates
    assert "Proposal ready." in progress_updates
    confirmation = json.loads(post_requests[-1].content)["content"]
    assert "Create assignment `Assignment — New assignment` in ECE 202" in confirmation
    assert "Archive `Old assignment` (Notion delete)" in confirmation


@pytest.mark.parametrize(
    ("user_phrase", "assessment_type", "canonical_title"),
    [
        ("tutorial", UserCreatableAssessmentType.TUTORIAL, "Tutorial — Vectors"),
        ("lab", UserCreatableAssessmentType.LAB, "Lab — Oscilloscope"),
        (
            "studying block",
            UserCreatableAssessmentType.STUDYING_BLOCK,
            "Studying Block — Midterm Review",
        ),
    ],
)
@pytest.mark.asyncio
async def test_qwen_agent_creates_expanded_user_todo_types_from_discord_boundary(
    engine: Engine,
    user_phrase: str,
    assessment_type: UserCreatableAssessmentType,
    canonical_title: str,
) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.decisions = iter(
                (
                    AcademicAgentDecision(
                        tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 222"),)
                    ),
                    AcademicAgentDecision(
                        tool_calls=(
                            CreateAssessmentCall(
                                tool="create_assessment",
                                course_id="course-222",
                                title=canonical_title,
                                due_at=datetime(2026, 9, 20, 21, tzinfo=UTC),
                                assessment_type=assessment_type,
                            ),
                        )
                    ),
                )
            )

        async def invoke_structured(self, *, prompt, response_model):
            del response_model
            self.prompts.append(prompt)
            return SimpleNamespace(output=next(self.decisions))

    class Catalog:
        def search_courses(self, query: str):
            assert query == "ECE 222"
            return (
                AcademicCourseOption(
                    course_id="course-222",
                    course_code="ECE 222",
                    title="Signals",
                ),
            )

        def search_assessments(self, query: str, course_id: str | None = None):
            del query, course_id
            return ()

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": str(111111111111111111 + len(requests))})

    gateway = Gateway()
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            agent_gateway=gateway,
            agent_catalog=Catalog(),
            assistant_user_id=ASSISTANT,
        )(
            _message(
                str(370000000000000000 + len(user_phrase)),
                f"<@{ASSISTANT}> schedule an ECE 222 {user_phrase} for Sep 20",
            )
        )

    assert result.status == "handled"
    post_requests = [request for request in requests if request.method == "POST"]
    assert len(post_requests) == 2
    assert f"ECE 222 {user_phrase}" in gateway.prompts[0]
    with Session(engine) as session:
        checkin = session.scalar(select(AcademicCheckIn))
        proposal = session.scalar(select(AcademicProposedChange))
    assert checkin is not None
    assert checkin.status == "proposal_pending"
    assert proposal is not None
    assert proposal.payload["changes"] == [
        {
            "field": "create_assessment",
            "value": canonical_title,
            "course_id": "course-222",
            "course_code": "ECE 222",
            "title": canonical_title,
            "due_at": "2026-09-20T21:00:00Z",
            "assessment_type": assessment_type.value,
        }
    ]
    confirmation = json.loads(post_requests[-1].content)["content"]
    assert f"Create {assessment_type.value} `{canonical_title}` in ECE 222" in confirmation


@pytest.mark.parametrize(
    ("content", "question"),
    [
        (
            "<@777777777777777777> schedule an ECE 222 thing",
            "Please specify quiz, assignment, tutorial, lab, or studying block.",
        ),
        (
            "<@777777777777777777> schedule an ECE 222 quiz assignment",
            "Please choose exactly one type: quiz, assignment, tutorial, lab, or studying block.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_qwen_agent_type_clarifications_create_no_write_capable_proposal(
    engine: Engine,
    content: str,
    question: str,
) -> None:
    class Gateway:
        async def invoke_structured(self, *, prompt, response_model):
            del prompt, response_model
            return SimpleNamespace(output=AcademicAgentDecision(question=question))

    class Catalog:
        def search_courses(self, query: str):
            del query
            return ()

        def search_assessments(self, query: str, course_id: str | None = None):
            del query, course_id
            return ()

    class Writer:
        async def apply_confirmed_changes(self, changes, *, proposal_id, confirmation_event):
            raise AssertionError("clarification-only requests must not write to Notion")

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": str(111111111111111111 + len(requests))})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            writer=Writer(),
            agent_gateway=Gateway(),
            agent_catalog=Catalog(),
            assistant_user_id="777777777777777777",
        )(_message("383838383838383838", content))

    assert result.status == "handled"
    with Session(engine) as session:
        checkin = session.scalar(select(AcademicCheckIn))
        proposal = session.scalar(select(AcademicProposedChange))
    assert checkin is not None
    assert checkin.status == "questioned"
    assert proposal is None
    post_requests = [request for request in requests if request.method == "POST"]
    assert len(post_requests) == 2
    response = json.loads(post_requests[-1].content)["content"]
    assert question in response
    assert "Reply with the requested details, or say cancel" in response


@pytest.mark.asyncio
async def test_unmentioned_clarification_reply_resumes_attempt_two_and_proposes(
    engine: Engine,
    tmp_path,
) -> None:
    question = "Please specify the todo type: quiz, assignment, tutorial, lab, or studying block."

    class Gateway:
        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.decisions = iter(
                (
                    AcademicAgentDecision(question=question),
                    AcademicAgentDecision(
                        tool_calls=(SearchCoursesCall(tool="search_courses", query="ECE 222"),)
                    ),
                    AcademicAgentDecision(
                        tool_calls=(
                            CreateAssessmentCall(
                                tool="create_assessment",
                                course_id="course-222",
                                title="Assignment 1",
                                due_at=datetime(2026, 9, 20, 21, tzinfo=UTC),
                                assessment_type=UserCreatableAssessmentType.ASSIGNMENT,
                            ),
                        )
                    ),
                )
            )

        async def invoke_structured(self, *, prompt, response_model):
            assert response_model is AcademicAgentWireDecision
            self.prompts.append(prompt)
            return SimpleNamespace(output=next(self.decisions))

    class Catalog:
        def search_courses(self, query: str):
            assert query == "ECE 222"
            return (
                AcademicCourseOption(
                    course_id="course-222",
                    course_code="ECE 222",
                    title="Signals",
                ),
            )

        def search_assessments(self, query: str, course_id: str | None = None):
            del query, course_id
            return ()

    now = datetime(2026, 9, 6, 22, tzinfo=UTC)
    artifacts = ArtifactStore(
        tmp_path / "artifacts",
        retention_days_by_class={"academic_agent_context": 1},
        clock=lambda: now,
    )
    clarification_service = AcademicAgentClarificationService(
        engine=engine,
        artifact_store=artifacts,
        clock=lambda: now,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": str(111111111111111111 + len(requests))})

    gateway = Gateway()
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        handler = _handler(
            engine,
            client,
            agent_gateway=gateway,
            agent_catalog=Catalog(),
            assistant_user_id=ASSISTANT,
            agent_clarification_service=clarification_service,
        )
        first = await handler(
            _message(
                "383838383838383401",
                f"<@{ASSISTANT}> schedule an ECE 222 thing for Sep 20",
            )
        )
        second = await handler(_message("383838383838383402", "assignment"))

    assert first.status == second.status == "handled"
    assert len(gateway.prompts) == 3
    continuation_prompt = json.loads(gateway.prompts[1].partition("\n")[2])
    assert continuation_prompt["original_user_request_untrusted"] == (
        "schedule an ECE 222 thing for Sep 20"
    )
    assert continuation_prompt["prior_clarification_questions_untrusted"] == [question]
    assert continuation_prompt["clarification_answers_untrusted"] == ["assignment"]
    post_bodies = [
        json.loads(request.content)["content"] for request in requests if request.method == "POST"
    ]
    assert len(post_bodies) == 4
    assert question in post_bodies[1]
    assert "Reply with the requested details, or say cancel" in post_bodies[1]
    assert "Proposed academic updates" in post_bodies[-1]
    with Session(engine) as session:
        discourse = session.scalar(select(AcademicDiscourseSession))
        turns = list(session.scalars(select(AcademicDiscourseTurn)))
        checkins = list(session.scalars(select(AcademicCheckIn)))
        proposals = list(session.scalars(select(AcademicProposedChange)))
    assert discourse is not None
    assert discourse.state == "completed"
    assert discourse.session_kind == "agent_clarification"
    assert discourse.partial_state["attempt_number"] == 2
    assert discourse.partial_state["outcome"] == "resolved"
    assert discourse.partial_state["context_artifact_key"] is None
    assert "schedule an ECE 222 thing" not in str(discourse.partial_state)
    assert len(turns) == 2
    assert len(checkins) == 2
    assert len(proposals) == 1


@pytest.mark.asyncio
async def test_third_clarification_exhausts_and_unmentioned_reply_cannot_continue(
    engine: Engine,
    tmp_path,
) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke_structured(self, *, prompt, response_model):
            del prompt
            assert response_model is AcademicAgentWireDecision
            self.calls += 1
            return SimpleNamespace(
                output=AcademicAgentDecision(
                    question="Which exact assessment and date should I use?"
                )
            )

    class EmptyCatalog:
        def search_courses(self, query: str):
            del query
            return ()

        def search_assessments(self, query: str, course_id: str | None = None):
            del query, course_id
            return ()

    now = datetime(2026, 9, 6, 22, tzinfo=UTC)
    service = AcademicAgentClarificationService(
        engine=engine,
        artifact_store=ArtifactStore(
            tmp_path / "artifacts",
            retention_days_by_class={"academic_agent_context": 1},
            clock=lambda: now,
        ),
        clock=lambda: now,
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": str(121111111111111111 + len(requests))})

    gateway = Gateway()
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        handler = _handler(
            engine,
            client,
            agent_gateway=gateway,
            agent_catalog=EmptyCatalog(),
            assistant_user_id=ASSISTANT,
            agent_clarification_service=service,
        )
        for event_id, text in (
            ("383838383838383411", "prepare my assessment"),
            ("383838383838383412", "the upcoming one"),
            ("383838383838383413", "the important one"),
        ):
            result = await handler(_message(event_id, f"<@{ASSISTANT}> {text}"))
            assert result.status == "handled"
        ignored = await handler(_message("383838383838383414", "another answer"))

    assert ignored.status == "ignored"
    assert gateway.calls == 3
    post_bodies = [
        json.loads(request.content)["content"] for request in requests if request.method == "POST"
    ]
    assert "automatic clarification limit was reached" in post_bodies[-1]
    with Session(engine) as session:
        discourse = session.scalar(select(AcademicDiscourseSession))
        turns = list(session.scalars(select(AcademicDiscourseTurn)))
        checkins = list(session.scalars(select(AcademicCheckIn)))
        proposal = session.scalar(select(AcademicProposedChange))
    assert discourse is not None
    assert discourse.state == "completed"
    assert discourse.partial_state["attempt_number"] == 3
    assert discourse.partial_state["outcome"] == "exhausted"
    assert discourse.partial_state["context_artifact_key"] is None
    assert len(turns) == 3
    assert len(checkins) == 3
    assert proposal is None


@pytest.mark.asyncio
async def test_mentioned_cancel_closes_clarification_without_extra_readiness_or_model(
    engine: Engine,
    tmp_path,
) -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke_structured(self, *, prompt, response_model):
            del prompt, response_model
            self.calls += 1
            return SimpleNamespace(
                output=AcademicAgentDecision(question="Which assessment do you mean?")
            )

    class EmptyCatalog:
        def search_courses(self, query: str):
            del query
            return ()

        def search_assessments(self, query: str, course_id: str | None = None):
            del query, course_id
            return ()

    now = datetime(2026, 9, 6, 22, tzinfo=UTC)
    service = AcademicAgentClarificationService(
        engine=engine,
        artifact_store=ArtifactStore(
            tmp_path / "artifacts",
            retention_days_by_class={"academic_agent_context": 1},
            clock=lambda: now,
        ),
        clock=lambda: now,
    )
    runtime = _ReadyRuntime()
    responses: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        responses.append(json.loads(request.content)["content"])
        return httpx.Response(200, json={"id": str(131111111111111111 + len(responses))})

    gateway = Gateway()
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        handler = _handler(
            engine,
            client,
            agent_gateway=gateway,
            agent_catalog=EmptyCatalog(),
            assistant_user_id=ASSISTANT,
            ollama_runtime=runtime,
            agent_clarification_service=service,
        )
        await handler(_message("383838383838383421", f"<@{ASSISTANT}> update my work"))
        mentionless_cancel = await handler(_message("383838383838383422", "cancel"))
        cancelled = await handler(_message("383838383838383423", f"<@{ASSISTANT}> cancel"))

    assert mentionless_cancel.status == "handled"
    assert cancelled.status == "handled"
    assert gateway.calls == 1
    assert runtime.calls == 1
    assert responses[-2:] == [
        "No problem. Nothing was changed.",
        "No active clarification was waiting. Nothing was changed.",
    ]
    with Session(engine) as session:
        discourse = session.scalar(select(AcademicDiscourseSession))
        turns = list(session.scalars(select(AcademicDiscourseTurn)))
    assert discourse is not None
    assert discourse.state == "completed"
    assert discourse.partial_state["outcome"] == "cancelled"
    assert discourse.partial_state["context_artifact_key"] is None
    assert len(turns) == 2


@pytest.mark.asyncio
async def test_configured_assistant_ignores_unmentioned_natural_language(
    engine: Engine,
) -> None:
    class NeverGateway:
        async def invoke_structured(self, **_kwargs):
            raise AssertionError("an unmentioned message must not reach Qwen")

    class EmptyCatalog:
        def search_courses(self, _query: str):
            return ()

        def search_assessments(self, _query: str, _course_id: str | None = None):
            return ()

    def respond(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unmentioned message must not trigger a Discord response")

    runtime = _ReadyRuntime()
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            agent_gateway=NeverGateway(),
            agent_catalog=EmptyCatalog(),
            assistant_user_id=ASSISTANT,
            ollama_runtime=runtime,
        )(_message("353535353535353535", "please create an assignment"))

    assert result.status == "ignored"
    assert runtime.calls == 0
    with Session(engine) as session:
        assert session.scalar(select(AcademicCheckIn)) is None


@pytest.mark.asyncio
async def test_mention_text_without_discord_mention_metadata_is_ignored(engine: Engine) -> None:
    runtime = _ReadyRuntime()

    def respond(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("unverified mention text must not trigger Discord delivery")

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        handler = _handler(
            engine,
            client,
            assistant_user_id=ASSISTANT,
            agent_gateway=SimpleNamespace(),
            agent_catalog=SimpleNamespace(),
            ollama_runtime=runtime,
        )
        result = await handler(
            DiscordAcademicMessageCreate(
                message_id="353535353535353537",
                channel_id=CHANNEL,
                author_id=OWNER,
                timestamp=datetime(2026, 9, 6, 22, tzinfo=UTC),
                content=SecretStr(f"<@{ASSISTANT}> create an assignment"),
                mentioned_user_ids=(),
            )
        )

    assert result.status == "ignored"
    assert runtime.calls == 0


@pytest.mark.asyncio
async def test_runtime_failure_sends_safe_response_without_calling_qwen_or_persisting(
    engine: Engine,
) -> None:
    class UnavailableRuntime:
        async def ensure_ready(self) -> object:
            raise RuntimeError("http://secret-host/private-runtime-detail")

    class NeverGateway:
        async def invoke_structured(self, **_kwargs: object) -> object:
            raise AssertionError("Qwen must not run when readiness fails")

    class EmptyCatalog:
        def search_courses(self, _query: str):
            return ()

        def search_assessments(self, _query: str, _course_id: str | None = None):
            return ()

    responses: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        responses.append(json.loads(request.content)["content"])
        return httpx.Response(200, json={"id": "111111111111111111"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            agent_gateway=NeverGateway(),
            agent_catalog=EmptyCatalog(),
            assistant_user_id=ASSISTANT,
            ollama_runtime=UnavailableRuntime(),
        )(_message("353535353535353536", f"<@{ASSISTANT}> create an assignment"))

    assert result.status == "failed"
    assert responses[0] == "- Waking Qwen."
    assert "Academic request stopped safely." in responses[1]
    assert responses[-1] == (
        "Qwen is unavailable on this Mac; run scripts/ollama_qwen_start.sh and try again."
    )
    assert "secret-host" not in str(responses)
    with Session(engine) as session:
        assert session.scalar(select(AcademicCheckIn)) is None


@pytest.mark.asyncio
async def test_unmentioned_reflection_is_ignored_before_memory_or_delivery(
    engine: Engine,
) -> None:
    class MemoryService:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def handle_reflection(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                status="applied",
                response="Added recursion and scheduled a separate practice block.",
            )

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "111111111111111111"})

    memory = MemoryService()
    runtime = _ReadyRuntime()
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            assistant_user_id=ASSISTANT,
            memory_service=memory,
            ollama_runtime=runtime,
        )(
            _message(
                "363636363636363636",
                "I really struggled with ECE 250 recursion.",
            )
        )

    assert result.status == "ignored"
    assert memory.calls == []
    assert runtime.calls == 0
    assert requests == []
    with Session(engine) as session:
        assert session.scalar(select(AcademicCheckIn)) is None


@pytest.mark.asyncio
async def test_study_session_request_is_independently_declined_by_semantic_memory_agent(
    engine: Engine,
) -> None:
    class MemoryService:
        async def handle_memory_review(self, **_kwargs):
            return SimpleNamespace(status="not_applicable", response=None)

        async def handle_reflection(self, **_kwargs):
            return SimpleNamespace(status="not_applicable", response=None)

    class Gateway:
        async def invoke_structured(self, *, prompt, response_model):
            del prompt, response_model
            return SimpleNamespace(
                output=AcademicAgentDecision(
                    question="When should I schedule this, for how long, and separate or combined?"
                )
            )

    class EmptyCatalog:
        def search_courses(self, _query: str):
            return ()

        def search_assessments(self, _query: str, _course_id: str | None = None):
            return ()

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": str(191111111111111111 + len(requests))})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            assistant_user_id=ASSISTANT,
            memory_service=MemoryService(),
            agent_gateway=Gateway(),
            agent_catalog=EmptyCatalog(),
        )(
            _message(
                "393939393939393939",
                f"<@{ASSISTANT}> I need to study for ECE 250, specifically race conditions.",
            )
        )

    assert result.status == "handled"
    assert (
        "When should I schedule this, for how long, and separate or combined?"
        in json.loads(requests[-1].content)["content"]
    )


@pytest.mark.asyncio
async def test_mixed_delete_and_memory_request_is_semantically_split_by_qwen(
    engine: Engine,
) -> None:
    memory_calls: list[str] = []

    class MixedRouter:
        async def invoke_structured(self, *, prompt, response_model):
            del prompt
            assert response_model is AcademicRequestRouteDecision
            return SimpleNamespace(
                output=AcademicRequestRouteDecision(
                    calendar_request="Delete both study sessions tomorrow.",
                    memory_request=(
                        "remember that I am confident on recursion and insertion sort now"
                    ),
                )
            )

    class MemoryService:
        async def handle_memory_review(self, **_kwargs):
            return SimpleNamespace(status="not_applicable", response=None)

        async def handle_reflection(self, **kwargs):
            memory_calls.append(kwargs["raw_text"])
            return SimpleNamespace(
                status="applied",
                response="I updated your learning memory for recursion and insertion sort.",
            )

    class Gateway:
        def __init__(self) -> None:
            self.decisions = iter(
                (
                    AcademicAgentDecision(
                        tool_calls=(
                            SearchAssessmentsCall(
                                tool="search_assessments",
                                query="both study sessions tomorrow",
                            ),
                        ),
                    ),
                    AcademicAgentDecision(
                        tool_calls=(
                            ArchiveAssessmentCall(
                                tool="archive_assessment", assessment_id="study-race"
                            ),
                            ArchiveAssessmentCall(
                                tool="archive_assessment", assessment_id="study-sort"
                            ),
                        ),
                    ),
                )
            )

        async def invoke_structured(self, *, prompt, response_model):
            del prompt, response_model
            return SimpleNamespace(output=next(self.decisions))

    class Catalog:
        def search_courses(self, _query: str):
            return ()

        def search_assessments(self, _query: str, _course_id: str | None = None):
            return tuple(
                AcademicAssessmentOption(
                    assessment_id=assessment_id,
                    course_id="course-250",
                    course_code="ECE 250",
                    title=title,
                    due_at=datetime(2026, 9, 7, 23, tzinfo=UTC) + timedelta(hours=offset),
                    assessment_type=AssessmentType.STUDYING_BLOCK,
                    expected_last_edited_at=datetime(2026, 9, 6, 20, tzinfo=UTC),
                )
                for assessment_id, title, offset in (
                    ("study-race", "Studying Block — race conditions", 0),
                    ("study-sort", "Studying Block — insertion sort", 1),
                )
            )

    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": str(191111111111111111 + len(requests))})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            assistant_user_id=ASSISTANT,
            memory_service=MemoryService(),
            agent_gateway=Gateway(),
            semantic_router_gateway=MixedRouter(),
            agent_catalog=Catalog(),
        )(
            _message(
                "393939393939393941",
                f"<@{ASSISTANT}> Delete both study sessions tomorrow and remember that "
                "I'm confident on recursion and insertion sort now.",
            )
        )

    assert result.status == "handled"
    assert memory_calls == ["remember that I am confident on recursion and insertion sort now"]
    contents = [
        json.loads(request.content)["content"] for request in requests if request.method == "POST"
    ]
    assert any("updated your learning memory" in content for content in contents)
    proposal = next(content for content in contents if "Proposed academic updates" in content)
    assert proposal.count("Notion delete") == 2


@pytest.mark.asyncio
async def test_exact_confirmation_applies_once_and_replay_does_not_patch(engine: Engine) -> None:
    class Writer:
        def __init__(self) -> None:
            self.calls = 0

        async def apply_confirmed_changes(self, changes, *, proposal_id, confirmation_event):
            self.calls += 1

    writer = Writer()
    runtime = _ReadyRuntime()

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "111111111111111111"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        public_id = _seed_pending_assignment_proposal(engine, "444444444444444444")
        command = f"confirm {public_id}"
        command_handler = _handler(
            engine,
            client,
            writer=writer,
            agent_gateway=SimpleNamespace(),
            agent_catalog=SimpleNamespace(),
            assistant_user_id=ASSISTANT,
            ollama_runtime=runtime,
        )
        await command_handler(_message("555555555555555555", command))
        await command_handler(_message("666666666666666666", command))

    assert writer.calls == 1
    assert runtime.calls == 0


@pytest.mark.asyncio
async def test_reject_is_terminal_audited_and_never_calls_writer(engine: Engine) -> None:
    class Writer:
        async def apply_confirmed_changes(self, changes, *, proposal_id, confirmation_event):
            raise AssertionError("rejected proposal must never reach Notion")

    runtime = _ReadyRuntime()

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "111111111111111111"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        public_id = _seed_pending_assignment_proposal(engine, "777777777777777777")
        command_handler = _handler(
            engine,
            client,
            writer=Writer(),
            agent_gateway=SimpleNamespace(),
            agent_catalog=SimpleNamespace(),
            assistant_user_id=ASSISTANT,
            ollama_runtime=runtime,
        )
        await command_handler(_message("888888888888888888", f"reject {public_id}"))
        await command_handler(_message("999999999999999999", f"confirm {public_id}"))

    with Session(engine) as session:
        proposal = session.scalar(select(AcademicProposedChange))
        audits = list(session.scalars(select(AuditEvent)))
    assert proposal is not None
    assert proposal.state == "rejected"
    assert [event.action for event in audits] == ["academic_proposal.rejected"]
    assert runtime.calls == 0


@pytest.mark.parametrize(
    "content",
    [
        "confirm 01234567-89ab-4def-8123-456789abcdef extra",
        "confirm 0123456789ab4def8123456789abcdef",
        "confirm 01234567-89AB-4def-8123-456789abcdef",
        "reject 01234567-89ab-4def-7123-456789abcdef",
        "CONFIRM 01234567-89ab-4def-8123-456789abcdef",
    ],
)
def test_command_parser_requires_exact_canonical_form(content: str) -> None:
    assert parse_academic_command(content) is None


def test_command_parser_accepts_exact_canonical_form() -> None:
    proposal_id = "01234567-89ab-4def-8123-456789abcdef"
    assert parse_academic_command(f"confirm {proposal_id}") == (
        "confirm",
        __import__("uuid").UUID(proposal_id),
    )


@pytest.mark.asyncio
async def test_malformed_command_is_rejected_without_readiness_or_qwen(engine: Engine) -> None:
    runtime = _ReadyRuntime()
    responses: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        responses.append(json.loads(request.content)["content"])
        return httpx.Response(200, json={"id": "111111111111111111"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            agent_gateway=SimpleNamespace(),
            agent_catalog=SimpleNamespace(),
            assistant_user_id=ASSISTANT,
            ollama_runtime=runtime,
        )(_message("999999999999999998", "confirm not-a-uuid"))

    assert result.status == "ignored"
    assert runtime.calls == 0
    assert responses == []
