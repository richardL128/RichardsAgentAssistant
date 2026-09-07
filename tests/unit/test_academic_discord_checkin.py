from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents.academic_planner.contracts import (
    AcademicAgentDecision,
    AcademicAssessmentOption,
    AcademicCourseOption,
    ArchiveAssessmentCall,
    AssessmentType,
    CreateAssessmentCall,
    SearchAssessmentsCall,
    SearchCoursesCall,
)
from app.agents.academic_planner.discord_checkin import (
    AcademicDiscordCheckinHandler,
    parse_academic_command,
)
from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    DiscordAcademicResponseDelivery,
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
    AcademicProposedChange,
    AuditEvent,
    Base,
    Delivery,
)

CHANNEL = "987654321012345678"
OWNER = "123456789012345678"


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
    return DiscordAcademicMessageCreate(
        message_id=message_id,
        channel_id=CHANNEL,
        author_id=author_id,
        timestamp=datetime(2026, 9, 6, 22, tzinfo=UTC),
        content=SecretStr(content),
    )


def _handler(
    engine: Engine,
    client: httpx.AsyncClient,
    *,
    writer: object | None = None,
    agent_gateway: object | None = None,
    agent_catalog: object | None = None,
    assistant_user_id: str | None = None,
    memory_service: object | None = None,
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
        agent_gateway=agent_gateway,  # type: ignore[arg-type]
        agent_catalog=agent_catalog,  # type: ignore[arg-type]
        assistant_user_id=assistant_user_id,
        memory_service=memory_service,  # type: ignore[arg-type]
    )


class _UnusedClarificationHandler:
    async def __call__(
        self, interaction: DiscordClarificationInteraction
    ) -> DiscordClarificationCallbackResult:
        raise AssertionError(f"unexpected clarification interaction {interaction.interaction_id}")


@pytest.mark.asyncio
async def test_authorized_message_is_durable_and_replay_sends_once(engine: Engine) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "111111111111111111", "guild_id": "42"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        handler = _handler(engine, client)
        event = _message("222222222222222222", "  completed assignment-page-1  ")
        first = await handler(event)
        replay = await handler(event)

    assert first.status == "handled"
    assert replay.status == "duplicate"
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["enforce_nonce"] is True
    assert body["allowed_mentions"] == {"parse": []}
    assert "Expires:" in body["content"]
    assert "Confirm exactly: confirm " in body["content"]
    assert "Reject exactly: reject " in body["content"]
    with Session(engine) as session:
        checkins = list(session.scalars(select(AcademicCheckIn)))
        proposals = list(session.scalars(select(AcademicProposedChange)))
        deliveries = list(session.scalars(select(Delivery)))
    assert len(checkins) == len(proposals) == len(deliveries) == 1
    assert checkins[0].external_event_id == event.message_id
    assert checkins[0].content_artifact_key is None
    assert event.content.get_secret_value() not in str(checkins[0].redacted_summary)
    assert event.content.get_secret_value() not in repr(event)


@pytest.mark.asyncio
async def test_gateway_to_repository_path_creates_exactly_one_proposal(engine: Engine) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "111111111111111111"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        message_handler = _handler(engine, client)
        listener = DiscordGatewayListener(
            token=SecretStr("test-token"),
            api_base_url="https://discord.com/api/v10",
            allowed_channel_ids={CHANNEL},
            authorized_user_ids={OWNER},
            handler=_UnusedClarificationHandler(),
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
                "content": "completed assignment-page-1",
            },
        }
        assert await listener.handle_gateway_payload(payload) == "handled"
        assert await listener.handle_gateway_payload(payload) == "duplicate"
        await listener.drain_message_tasks()

    with Session(engine) as session:
        assert len(list(session.scalars(select(AcademicCheckIn)))) == 1
        assert len(list(session.scalars(select(AcademicProposedChange)))) == 1
        assert len(list(session.scalars(select(Delivery)))) == 1


@pytest.mark.asyncio
async def test_unsupported_message_questions_without_write_capable_proposal(
    engine: Engine,
) -> None:
    private_text = "I did a bunch of secret unstructured work today"

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "111111111111111111"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(engine, client)(_message("333333333333333333", private_text))

    assert result.status == "handled"
    with Session(engine) as session:
        checkin = session.scalar(select(AcademicCheckIn))
        assert checkin is not None
        assert checkin.status == "questioned"
        assert private_text not in str(checkin.redacted_summary)
        assert session.scalar(select(AcademicProposedChange)) is None


@pytest.mark.asyncio
async def test_qwen_agent_batches_create_and_archive_from_one_mentioned_message(
    engine: Engine,
) -> None:
    class Gateway:
        def __init__(self) -> None:
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
            assistant_user_id="777777777777777777",
        )(
            _message(
                "343434343434343434",
                "<@777777777777777777> create an assignment in ECE 202 and delete "
                "old assignment in ECE 250",
            )
        )

    assert result.status == "handled"
    assert len(requests) == 2
    assert "<@777777777777777777>" not in gateway.prompts[0]
    with Session(engine) as session:
        proposal = session.scalar(select(AcademicProposedChange))
        assert proposal is not None
        assert [item["field"] for item in proposal.payload["changes"]] == [
            "create_assessment",
            "archive_assessment",
        ]
    confirmation = json.loads(requests[1].content)["content"]
    assert "Create assignment `Assignment — New assignment` in ECE 202" in confirmation
    assert "Archive `Old assignment` (Notion delete)" in confirmation


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

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            agent_gateway=NeverGateway(),
            agent_catalog=EmptyCatalog(),
            assistant_user_id="777777777777777777",
        )(_message("353535353535353535", "please create an assignment"))

    assert result.status == "ignored"
    with Session(engine) as session:
        assert session.scalar(select(AcademicCheckIn)) is None


@pytest.mark.asyncio
async def test_unmentioned_reflection_routes_to_durable_memory_service(
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
    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        result = await _handler(
            engine,
            client,
            assistant_user_id="777777777777777777",
            memory_service=memory,
        )(
            _message(
                "363636363636363636",
                "I really struggled with ECE 250 recursion.",
            )
        )

    assert result.status == "handled"
    assert len(memory.calls) == 1
    assert memory.calls[0]["user_id"] == OWNER
    assert len(requests) == 1
    assert "separate practice block" in json.loads(requests[0].content)["content"]


@pytest.mark.asyncio
async def test_exact_confirmation_applies_once_and_replay_does_not_patch(engine: Engine) -> None:
    class Writer:
        def __init__(self) -> None:
            self.calls = 0

        async def apply_confirmed_changes(self, changes, *, proposal_id, confirmation_event):
            self.calls += 1

    writer = Writer()

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "111111111111111111"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        handler = _handler(engine, client, writer=writer)
        await handler(_message("444444444444444444", "completed assignment-page-1"))
        with Session(engine) as session:
            proposal = session.scalar(select(AcademicProposedChange))
            assert proposal is not None
            public_id = proposal.target_id
        command = f"confirm {public_id}"
        await handler(_message("555555555555555555", command))
        await handler(_message("666666666666666666", command))

    assert writer.calls == 1


@pytest.mark.asyncio
async def test_reject_is_terminal_audited_and_never_calls_writer(engine: Engine) -> None:
    class Writer:
        async def apply_confirmed_changes(self, changes, *, proposal_id, confirmation_event):
            raise AssertionError("rejected proposal must never reach Notion")

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "111111111111111111"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10", transport=httpx.MockTransport(respond)
    ) as client:
        handler = _handler(engine, client, writer=Writer())
        await handler(_message("777777777777777777", "completed assignment-page-1"))
        with Session(engine) as session:
            proposal = session.scalar(select(AcademicProposedChange))
            assert proposal is not None
            public_id = proposal.target_id
        await handler(_message("888888888888888888", f"reject {public_id}"))
        await handler(_message("999999999999999999", f"confirm {public_id}"))

    with Session(engine) as session:
        proposal = session.scalar(select(AcademicProposedChange))
        audits = list(session.scalars(select(AuditEvent)))
    assert proposal is not None
    assert proposal.state == "rejected"
    assert [event.action for event in audits] == ["academic_proposal.rejected"]


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
