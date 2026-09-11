from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import SecretStr

from app.agents.academic_planner.contracts import AcademicCourseOption
from app.agents.academic_planner.discord_harness import (
    NativeAcademicDiscordHandler,
    _localize_wall_time,
    _render_event,
)
from app.agents.harness import AgentHarnessEvent, ToolExecutionError
from app.connectors.discord_gateway import DiscordAcademicMessageCreate

NOW = datetime(2026, 9, 9, 14, tzinfo=UTC)


class _Gateway:
    def __init__(self, messages: Sequence[AIMessage]) -> None:
        self.messages = list(messages)
        self.inputs: list[tuple[BaseMessage, ...]] = []

    async def invoke_tools(
        self,
        messages: Sequence[BaseMessage],
        tools: Sequence[Mapping[str, object]],
    ) -> AIMessage:
        assert tools
        self.inputs.append(tuple(messages))
        return self.messages.pop(0)


class _Runtime:
    async def ensure_ready(self) -> object:
        return object()


@pytest.mark.asyncio
async def test_empty_model_response_is_failed_not_completed() -> None:
    delivery = _Delivery()
    store = _Store()
    result = await _handler(_Gateway([AIMessage(content="")]), store, delivery)(
        _message("Tell me something interesting.")
    )
    assert result.status == "failed"
    assert delivery.responses == ["The model returned no visible response. Please try again."]
    assert store.proposals == []


class _Catalog:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events

    def search_courses(self, query: str):
        assert query == "ECE 202"
        if self.events is not None:
            self.events.append("search_courses")
        return (
            AcademicCourseOption(
                course_id="course-1",
                course_code="ECE 202",
                title="Circuits",
            ),
        )

    def search_assessments(self, query: str, course_id: str | None = None):
        if self.events is not None:
            self.events.append("search_assessments")
        return ()


class _Syncer:
    def __init__(
        self,
        events: list[str] | None = None,
        *,
        status: str = "succeeded",
        diagnostic_codes: Sequence[str] = (),
    ) -> None:
        self.events = events
        self.status = status
        self.diagnostic_codes = tuple(diagnostic_codes)
        self.calls = 0

    async def sync(self, *, now: datetime | None = None):
        assert now == NOW
        self.calls += 1
        if self.events is not None:
            self.events.append("sync")
        return SimpleNamespace(status=self.status, diagnostic_codes=self.diagnostic_codes)


class _Store:
    confirmation_ttl_hours = 24

    def __init__(self) -> None:
        self.proposals = []

    def get_latest_daily_plan(self):
        return None

    def save_discord_checkin(self, proposal, **_kwargs):
        self.proposals.append(proposal)
        return SimpleNamespace(status="created")


class _Delivery:
    def __init__(self, *, fail_once_keys: Sequence[str] = ()) -> None:
        self.responses: list[str] = []
        self.response_keys: list[str] = []
        self.confirmations = []
        self._seen_keys: set[str] = set()
        self._fail_once_keys = set(fail_once_keys)

    async def send_response(self, content: str, *, idempotency_key: str) -> object:
        assert idempotency_key
        if idempotency_key in self._fail_once_keys:
            self._fail_once_keys.remove(idempotency_key)
            raise RuntimeError("connector_transient")
        if idempotency_key in self._seen_keys:
            return object()
        self._seen_keys.add(idempotency_key)
        self.response_keys.append(idempotency_key)
        self.responses.append(content)
        return object()

    async def send_confirmation(self, proposal, *, idempotency_key: str) -> object:
        assert idempotency_key
        self.confirmations.append(proposal)
        return object()


def _message(content: str) -> DiscordAcademicMessageCreate:
    return DiscordAcademicMessageCreate(
        message_id="111111111111111111",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        timestamp=NOW,
        content=SecretStr(content),
        mentioned_user_ids=("444444444444444444",),
    )


def _handler(
    gateway: _Gateway,
    store: _Store,
    delivery: _Delivery,
    *,
    catalog: _Catalog | None = None,
    syncer: _Syncer | None = None,
):
    return NativeAcademicDiscordHandler(
        store=store,  # type: ignore[arg-type]
        delivery=delivery,  # type: ignore[arg-type]
        allowed_channel_ids={"222222222222222222"},
        authorized_user_ids={"333333333333333333"},
        writer_provider=lambda: None,
        ollama_runtime=_Runtime(),
        agent_gateway=gateway,  # type: ignore[arg-type]
        agent_catalog=catalog or _Catalog(),
        assistant_user_id="444444444444444444",
        catalog_syncer=syncer or _Syncer(),
        catalog_sync_timeout_seconds=1.0,
    )


@pytest.mark.asyncio
async def test_configured_harness_answers_arbitrary_input_without_semantic_router() -> None:
    gateway = _Gateway([AIMessage(content="Here is a direct answer.")])
    store = _Store()
    delivery = _Delivery()

    result = await _handler(gateway, store, delivery)(_message("<@444444444444444444> hello"))

    assert result.status == "handled"
    assert delivery.responses == ["Here is a direct answer."]
    assert delivery.response_keys == [
        "academic-discord-message:111111111111111111:final-response:v1"
    ]
    assert store.proposals == []
    assert gateway.inputs[0][1].content == "hello"


@pytest.mark.asyncio
async def test_system_message_supplies_current_owner_local_time() -> None:
    gateway = _Gateway([AIMessage(content="Ready.")])

    await _handler(gateway, _Store(), _Delivery())(_message("What date is it?"))

    system_content = str(gateway.inputs[0][0].content)
    assert "America/Toronto" in system_content
    assert "2026-09-09T10:00:00-04:00" in system_content
    assert "without Z or a UTC offset" in system_content
    assert "next matching date that is not in the past" in system_content
    assert "Follow explicit response-format requests exactly" in system_content
    assert "Keep calculations, scratch work, and" in system_content
    assert "do not call academic tools" in system_content


@pytest.mark.asyncio
async def test_study_session_wall_time_is_converted_from_owner_timezone() -> None:
    gateway = _Gateway(
        [
            AIMessage(
                content="I will verify the course.",
                tool_calls=[
                    {
                        "id": "search-1",
                        "name": "search_courses",
                        "args": {"query": "ECE 202"},
                    }
                ],
            ),
            AIMessage(
                content="I will prepare the requested local-time session.",
                tool_calls=[
                    {
                        "id": "create-1",
                        "name": "create_study_session",
                        "args": {
                            "course_id": "course-1",
                            "topic": "filters",
                            "starts_at": "2026-09-15T18:00:00",
                            "duration_minutes": 60,
                        },
                    }
                ],
            ),
            AIMessage(content="The 6 PM session is ready for review."),
        ]
    )
    delivery = _Delivery()

    result = await _handler(gateway, _Store(), delivery)(
        _message("Schedule ECE 202 filters for September 15 at 6 PM.")
    )

    assert result.status == "handled"
    assert len(delivery.confirmations) == 1
    change = delivery.confirmations[0].changes[0]
    assert change.due_at == datetime(2026, 9, 15, 22, tzinfo=UTC)
    assert change.ends_at == datetime(2026, 9, 15, 23, tzinfo=UTC)


@pytest.mark.asyncio
async def test_study_session_rejects_model_supplied_utc_timestamp() -> None:
    gateway = _Gateway(
        [
            AIMessage(
                content="I will verify the course.",
                tool_calls=[
                    {
                        "id": "search-1",
                        "name": "search_courses",
                        "args": {"query": "ECE 202"},
                    }
                ],
            ),
            AIMessage(
                content="I will prepare the session.",
                tool_calls=[
                    {
                        "id": "create-1",
                        "name": "create_study_session",
                        "args": {
                            "course_id": "course-1",
                            "topic": "filters",
                            "starts_at": "2026-09-15T18:00:00Z",
                            "duration_minutes": 60,
                        },
                    }
                ],
            ),
            AIMessage(content="I need to retry with a local wall-clock time."),
        ]
    )
    store = _Store()
    delivery = _Delivery()

    result = await _handler(gateway, store, delivery)(_message("Schedule it at 6 PM."))

    assert result.status == "handled"
    assert store.proposals == []
    assert delivery.confirmations == []
    assert "A tool call failed. I will adjust and continue." in delivery.responses
    assert delivery.responses[-1] == "I need to retry with a local wall-clock time."


@pytest.mark.asyncio
async def test_new_assessment_rejects_past_year_from_model() -> None:
    gateway = _Gateway(
        [
            AIMessage(
                content="I will verify the course.",
                tool_calls=[
                    {
                        "id": "search-1",
                        "name": "search_courses",
                        "args": {"query": "ECE 202"},
                    }
                ],
            ),
            AIMessage(
                content="I will prepare the quiz.",
                tool_calls=[
                    {
                        "id": "create-1",
                        "name": "create_assessment",
                        "args": {
                            "course_id": "course-1",
                            "title": "Quiz",
                            "due_at": "2025-09-19T23:59:00",
                            "assessment_type": "quiz",
                        },
                    }
                ],
            ),
            AIMessage(content="That year is in the past, so I did not prepare it."),
        ]
    )
    store = _Store()
    delivery = _Delivery()

    result = await _handler(gateway, store, delivery)(_message("Add a quiz due September 19."))

    assert result.status == "handled"
    assert store.proposals == []
    assert delivery.confirmations == []
    assert "New assessments must be due in the future." in delivery.responses


def test_local_wall_time_rejects_ambiguous_and_nonexistent_dst_times() -> None:
    timezone = ZoneInfo("America/Toronto")

    with pytest.raises(ToolExecutionError, match="does not exist"):
        _localize_wall_time(datetime(2026, 3, 8, 2, 30), timezone)  # noqa: DTZ001
    with pytest.raises(ToolExecutionError, match="ambiguous"):
        _localize_wall_time(datetime(2026, 11, 1, 1, 30), timezone)  # noqa: DTZ001


@pytest.mark.asyncio
async def test_mention_only_input_is_not_replaced_with_an_invented_prompt() -> None:
    gateway = _Gateway([AIMessage(content="What would you like help with?")])
    store = _Store()
    delivery = _Delivery()

    result = await _handler(gateway, store, delivery)(_message("<@444444444444444444>"))

    assert result.status == "handled"
    assert gateway.inputs[0][1].content == ""
    assert delivery.responses == ["What would you like help with?"]


@pytest.mark.asyncio
async def test_completed_final_response_replay_uses_one_stable_delivery_key() -> None:
    store = _Store()
    delivery = _Delivery()

    first = await _handler(_Gateway([AIMessage(content="Same answer.")]), store, delivery)(
        _message("<@444444444444444444> hello")
    )
    second = await _handler(_Gateway([AIMessage(content="Same answer.")]), store, delivery)(
        _message("<@444444444444444444> hello")
    )

    assert first.status == "handled"
    assert second.status == "handled"
    assert delivery.responses == ["Same answer."]
    assert delivery.response_keys == [
        "academic-discord-message:111111111111111111:final-response:v1"
    ]


def test_missing_tool_event_content_is_not_replaced_with_canned_text() -> None:
    assert _render_event(AgentHarnessEvent(kind="tool_call", turn=1)) is None
    assert _render_event(AgentHarnessEvent(kind="tool_error", turn=1)) is None


def test_render_event_suppresses_raw_tool_result_json() -> None:
    rendered = _render_event(
        AgentHarnessEvent(
            kind="tool_result",
            turn=1,
            result_json='{"content":{"course_id":"internal-uuid"},"status":"succeeded"}',
        )
    )

    assert rendered is None


def test_render_event_replaces_raw_tool_error_json_with_safe_progress() -> None:
    rendered = _render_event(
        AgentHarnessEvent(
            kind="tool_error",
            turn=1,
            error='{"error":"ValidationError: internal-uuid","status":"error"}',
            result_json='{"error":"ValidationError: internal-uuid","status":"error"}',
        )
    )

    assert rendered == "A tool response was not usable. I will adjust and continue."
    assert "internal-uuid" not in rendered
    assert "ValidationError" not in rendered


def test_render_event_keeps_safe_tool_error_progress_text() -> None:
    rendered = _render_event(
        AgentHarnessEvent(
            kind="tool_error",
            turn=1,
            error=(
                "Notion academic catalog sync returned partial before search, "
                "so I cannot trust cached catalog rows."
            ),
        )
    )

    assert rendered == (
        "Notion academic catalog sync returned partial before search, "
        "so I cannot trust cached catalog rows."
    )


@pytest.mark.asyncio
async def test_general_answer_skips_lazy_notion_sync() -> None:
    gateway = _Gateway([AIMessage(content="I can answer without Notion.")])
    store = _Store()
    delivery = _Delivery()
    syncer = _Syncer()

    result = await _handler(gateway, store, delivery, syncer=syncer)(
        _message("<@444444444444444444> explain how semaphores work")
    )

    assert result.status == "handled"
    assert delivery.responses == ["I can answer without Notion."]
    assert syncer.calls == 0


@pytest.mark.asyncio
async def test_notion_sync_happens_before_first_catalog_search_once() -> None:
    gateway = _Gateway(
        [
            AIMessage(
                content="I will check your courses.",
                tool_calls=[
                    {
                        "id": "search-courses",
                        "name": "search_courses",
                        "args": {"query": "ECE 202"},
                    }
                ],
            ),
            AIMessage(
                content="I will check matching assessments.",
                tool_calls=[
                    {
                        "id": "search-assessments",
                        "name": "search_assessments",
                        "args": {"query": "Lab 2"},
                    }
                ],
            ),
            AIMessage(content="I checked the current catalog."),
        ]
    )
    store = _Store()
    delivery = _Delivery()
    events: list[str] = []
    syncer = _Syncer(events)
    catalog = _Catalog(events)

    result = await _handler(gateway, store, delivery, catalog=catalog, syncer=syncer)(
        _message("<@444444444444444444> what ECE 202 lab do I have?")
    )

    assert result.status == "handled"
    assert syncer.calls == 1
    assert events == ["sync", "search_courses", "search_assessments"]
    assert "I checked the current catalog." in delivery.responses


@pytest.mark.parametrize(
    ("status", "diagnostic"),
    [
        ("partial", "assessment_calendar_missing"),
        ("setup_required", "notion_configuration_missing"),
    ],
)
@pytest.mark.asyncio
async def test_catalog_sync_failure_is_truthful_and_does_not_search_stale_rows(
    status: str,
    diagnostic: str,
) -> None:
    gateway = _Gateway(
        [
            AIMessage(
                content="I will check your courses.",
                tool_calls=[
                    {
                        "id": "search-courses",
                        "name": "search_courses",
                        "args": {"query": "ECE 202"},
                    }
                ],
            ),
            AIMessage(content="I cannot use your academic catalog until Notion sync is healthy."),
        ]
    )
    store = _Store()
    delivery = _Delivery()
    events: list[str] = []
    syncer = _Syncer(events, status=status, diagnostic_codes=(diagnostic,))
    catalog = _Catalog(events)

    result = await _handler(gateway, store, delivery, catalog=catalog, syncer=syncer)(
        _message("<@444444444444444444> list ECE 202")
    )

    transcript = "\n".join(delivery.responses)
    assert result.status == "handled"
    assert syncer.calls == 1
    assert events == ["sync"]
    assert f"Notion academic catalog sync returned {status}" in transcript
    assert "cannot trust cached catalog rows" in transcript
    assert diagnostic in transcript
    assert "I cannot use your academic catalog until Notion sync is healthy." in transcript


@pytest.mark.asyncio
async def test_tool_calls_and_results_are_visible_and_notion_create_waits_for_review() -> None:
    gateway = _Gateway(
        [
            AIMessage(
                content="I will check your courses.",
                tool_calls=[
                    {
                        "id": "search-1",
                        "name": "search_courses",
                        "args": {"query": "ECE 202"},
                    }
                ],
            ),
            AIMessage(
                content="I can prepare that addition.",
                tool_calls=[
                    {
                        "id": "create-1",
                        "name": "create_assessment",
                        "args": {
                            "course_id": "course-1",
                            "title": "Lab 2",
                            "due_at": "2026-09-12T17:00:00",
                            "assessment_type": "assignment",
                        },
                    }
                ],
            ),
            AIMessage(content="The addition is ready for your review."),
        ]
    )
    store = _Store()
    delivery = _Delivery()

    result = await _handler(gateway, store, delivery)(
        _message("Please add Lab 2 to ECE 202 for Saturday at 5 PM.")
    )

    assert result.status == "handled"
    assert len(store.proposals) == 1
    assert len(delivery.confirmations) == 1
    assert store.proposals[0].changes[0].field == "create_assessment"
    transcript = "\n".join(delivery.responses)
    assert "I will check your courses." in transcript
    assert "I can prepare that addition." in transcript
    assert "Tool call ·" not in transcript
    assert "Tool result ·" not in transcript
    assert '"course_code":"ECE 202"' not in transcript
    assert '"review":"required"' not in transcript
    assert "The addition is ready for your review." in transcript


@pytest.mark.asyncio
async def test_retry_final_response_is_not_suppressed_by_prior_streamed_events() -> None:
    final_key = "academic-discord-message:111111111111111111:final-response:v1"
    delivery = _Delivery(fail_once_keys=(final_key,))
    store = _Store()
    first_gateway = _Gateway(
        [
            AIMessage(
                content="I will check your courses.",
                tool_calls=[
                    {
                        "id": "search-1",
                        "name": "search_courses",
                        "args": {"query": "ECE 202"},
                    }
                ],
            ),
            AIMessage(content="First attempt final."),
        ]
    )

    with pytest.raises(RuntimeError, match="connector_transient"):
        await _handler(first_gateway, store, delivery)(
            _message("<@444444444444444444> what assignments are due next week?")
        )

    assert delivery.response_keys == [
        "academic-discord-message:111111111111111111:harness-event-1:v1",
    ]
    assert delivery.responses[0] == "I will check your courses."

    second = await _handler(
        _Gateway([AIMessage(content="Second attempt final answer.")]),
        store,
        delivery,
    )(_message("<@444444444444444444> what assignments are due next week?"))

    assert second.status == "handled"
    assert delivery.responses[-1] == "Second attempt final answer."
    assert delivery.response_keys[-1] == final_key


@pytest.mark.asyncio
@pytest.mark.parametrize("final_turn", [11, 50])
async def test_native_handler_can_answer_after_ten_turns(final_turn: int) -> None:
    calls = [
        AIMessage(
            content="I will verify the course details.",
            tool_calls=[
                {
                    "id": f"search-{index}",
                    "name": "search_courses",
                    "args": {"query": "ECE 202"},
                }
            ],
        )
        for index in range(1, final_turn)
    ]
    calls.append(AIMessage(content="Your course is ECE 202: Circuits."))
    gateway = _Gateway(calls)
    delivery = _Delivery()

    result = await _handler(gateway, _Store(), delivery)(_message("Check my course."))

    assert result.status == "handled"
    assert len(gateway.inputs) == final_turn
    assert delivery.responses[-1] == "Your course is ECE 202: Circuits."
    assert delivery.response_keys[-1].endswith(":final-response:v1")


@pytest.mark.asyncio
async def test_turn_limit_discards_accumulated_notion_proposal() -> None:
    calls = [
        AIMessage(
            content="I am preparing the requested addition.",
            tool_calls=[
                {
                    "id": "create-1",
                    "name": "create_assessment",
                    "args": {
                        "course_id": "course-1",
                        "title": "Lab 2",
                        "due_at": "2026-09-12T17:00:00-04:00",
                        "assessment_type": "assignment",
                    },
                }
            ],
        )
    ]
    calls.extend(
        AIMessage(
            content="I will verify the course details.",
            tool_calls=[
                {
                    "id": f"search-{index}",
                    "name": "search_courses",
                    "args": {"query": "ECE 202"},
                }
            ],
        )
        for index in range(2, 51)
    )
    gateway = _Gateway(calls)
    store = _Store()
    delivery = _Delivery()

    result = await _handler(gateway, store, delivery)(
        _message("Please add Lab 2, then verify its course.")
    )

    assert result.status == "failed"
    assert len(gateway.inputs) == 50
    assert store.proposals == []
    assert delivery.confirmations == []
    assert delivery.responses[-1] == (
        "The model harness reached its turn limit before finishing. No Notion change was made."
    )
