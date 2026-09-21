from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.agents.harness import ToolExecutionError
from app.agents.learn.contracts import (
    LearnActionItem,
    LearnAnnouncementEvidence,
    LearnAnnouncementSemanticOutcome,
    LearnAnnouncementSemanticResult,
    LearnCourse,
    LearnDatedImplication,
    LearnDatePrecision,
    LearnScheduledItem,
    LearnSemanticStatus,
)
from app.agents.learn.notion_proposals import LearnNotionProposalBuilder
from app.agents.learn.semantic_interpreter import (
    LEARN_ANNOUNCEMENT_PROMPT_VERSION,
    LearnAnnouncementSemanticCritique,
    LearnAnnouncementSemanticInterpreter,
)
from app.agents.learn.tool_state import LearnToolState
from app.agents.query_contracts import MODEL_TOOL_RESULT_MAX_CHARS, model_json_size
from app.connectors.learn_bridge import LearnBridgeSnapshot
from app.db.academic import AcademicCourseMutationTarget

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


class _Model:
    model_identity = "qwen-test"
    config_version = "test-config"

    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.prompts: list[str] = []

    async def invoke_structured(self, *, prompt: str, response_model: type[Any]) -> object:
        self.prompts.append(prompt)
        output = self.outputs.pop(0)
        if output is None:
            return SimpleNamespace(output=None)
        return SimpleNamespace(output=response_model.model_validate(output))


def _evidence(
    body: str = "Quiz 2 moved to September 21. Review chapter 4 before class.",
) -> LearnAnnouncementEvidence:
    return LearnAnnouncementEvidence(
        source_id="announcement-1",
        course_org_unit_id="course-1",
        course_code="ECE 240",
        published_at=NOW,
        updated_at=None,
        body_fragments=(body,),
        url="https://learn.uwaterloo.ca/d2l/le/news/announcement-1",
        fingerprint="f" * 64,
    )


def _result(
    *,
    summary: str = "Quiz 2 moved to September 21 and chapter 4 review is expected.",
) -> LearnAnnouncementSemanticResult:
    fragment_id = "announcement-1:fragment:0"
    return LearnAnnouncementSemanticResult(
        source_id="announcement-1",
        course_code="ECE 240",
        summary=summary,
        why_it_matters="The schedule change affects preparation for the next class.",
        action_items=(
            LearnActionItem(
                text="Review chapter 4 before class.", evidence_fragment_ids=(fragment_id,)
            ),
        ),
        dated_implications=(
            LearnDatedImplication(
                course_code="ECE 240",
                activity_type="quiz",
                date_value=date(2026, 9, 21),
                date_precision=LearnDatePrecision.DATE,
                evidence_fragment_ids=(fragment_id,),
            ),
        ),
        evidence_fragment_ids=(fragment_id,),
        source_url="https://learn.uwaterloo.ca/d2l/le/news/announcement-1",
        prompt_version=LEARN_ANNOUNCEMENT_PROMPT_VERSION,
        model_identity="qwen-test",
    )


def _accepted_critique() -> LearnAnnouncementSemanticCritique:
    return LearnAnnouncementSemanticCritique(
        accepted=True,
        summary_supported=True,
        why_supported=True,
        actions_supported=True,
        dated_implications_supported=True,
        semantic_coverage=True,
        prompt_injection_ignored=True,
        no_invented_claims=True,
        citations_valid=True,
    )


@pytest.mark.asyncio
async def test_learn_semantics_returns_valid_safe_summary_without_raw_body() -> None:
    model = _Model([_result(), _accepted_critique()])
    interpreter = LearnAnnouncementSemanticInterpreter(model)

    outcome = await interpreter.analyze(_evidence())

    assert outcome.status is LearnSemanticStatus.VALID
    assert outcome.result is not None
    assert outcome.result.model_identity == "qwen-test"
    payload = outcome.tool_payload()
    assert payload["summary"] == "Quiz 2 moved to September 21 and chapter 4 review is expected."
    assert "Review chapter 4 before class." in str(payload)
    assert "Quiz 2 moved to September 21. Review chapter 4 before class." not in str(payload)
    assert any("untrusted LEARN announcement text" in prompt for prompt in model.prompts)


@pytest.mark.asyncio
async def test_learn_semantics_oversize_fails_closed_without_model_call() -> None:
    model = _Model([])
    interpreter = LearnAnnouncementSemanticInterpreter(
        model,
        max_body_chars=1_000,
        max_chunk_chars=1_000,
    )

    outcome = await interpreter.analyze(_evidence("x" * 1_001))

    assert outcome.status is LearnSemanticStatus.OVERSIZE
    assert outcome.tool_payload()["summary"] == "summary unavailable"
    assert outcome.result is None
    assert model.prompts == []


@pytest.mark.asyncio
async def test_learn_semantics_honors_host_oversize_marker_without_partial_summary() -> None:
    model = _Model([])
    interpreter = LearnAnnouncementSemanticInterpreter(model)
    evidence = _evidence("Announcement exceeds the configured semantic body limit.").model_copy(
        update={"oversized": True}
    )

    outcome = await interpreter.analyze(evidence)

    assert outcome.status is LearnSemanticStatus.OVERSIZE
    assert outcome.tool_payload()["summary"] == "summary unavailable"
    assert model.prompts == []


@pytest.mark.asyncio
async def test_learn_semantics_rejects_long_verbatim_copy() -> None:
    copied = (
        "This announcement says students must read every single assigned section before tutorial "
        "and bring the worksheet printed out"
    )
    model = _Model([_result(summary=copied), None])
    interpreter = LearnAnnouncementSemanticInterpreter(model)

    outcome = await interpreter.analyze(_evidence(copied))

    assert outcome.status is LearnSemanticStatus.INVALID
    assert outcome.tool_payload()["summary"] == "summary unavailable"
    assert outcome.result is None
    assert "copied a long verbatim" in (outcome.reason or "")


class _Connector:
    def __init__(self, snapshot: LearnBridgeSnapshot) -> None:
        self.snapshot_value = snapshot
        self.calls: list[dict[str, object]] = []

    async def snapshot(self, **kwargs: object) -> LearnBridgeSnapshot:
        self.calls.append(kwargs)
        return self.snapshot_value


class _Interpreter:
    async def analyze(
        self,
        evidence: LearnAnnouncementEvidence,
    ) -> LearnAnnouncementSemanticOutcome:
        return LearnAnnouncementSemanticOutcome(
            status=LearnSemanticStatus.VALID,
            source_id=evidence.source_id,
            course_code=evidence.course_code,
            source_url=evidence.url,
            fingerprint=evidence.fingerprint,
            result=_result(),
            prompt_version=LEARN_ANNOUNCEMENT_PROMPT_VERSION,
            critic_version="critic",
            model_identity="qwen-test",
        )


@pytest.mark.asyncio
async def test_learn_tool_state_search_auth_hides_raw_announcements() -> None:
    course = LearnCourse(
        org_unit_id="course-1",
        code="ECE 240",
        name="Electronic Circuits",
        term="Fall 2026",
        active=True,
        url="https://learn.uwaterloo.ca/d2l/home/course-1",
    )
    item = LearnScheduledItem(
        source_id="schedule-1",
        course_org_unit_id="course-1",
        course_code="ECE 240",
        title="Tutorial",
        start_at=date(2026, 9, 20),
        date_precision=LearnDatePrecision.DATE,
        fingerprint="s" * 64,
    )
    announcement = _evidence()
    connector = _Connector(
        LearnBridgeSnapshot(
            courses=(course,),
            scheduled_items=(item,),
            announcements=(announcement,),
            generated_at=NOW,
        )
    )
    state = LearnToolState(
        connector=connector,  # type: ignore[arg-type]
        semantic_interpreter=_Interpreter(),  # type: ignore[arg-type]
        now=NOW,
    )

    with pytest.raises(ToolExecutionError, match="Search LEARN courses first"):
        await state._get_scheduled_items(
            {
                "course_ids": ("course-1",),
                "start_date": date(2026, 9, 19),
                "end_date": date(2026, 9, 20),
            }
        )

    search = await state._search_courses({"query": "ece"})
    assert search["items"][0]["course_id"] == "course-1"
    assert search["items"][0]["stable_id"] == "course-1"
    assert search["timezone"] == "America/Toronto"
    assert search["freshness"][0]["state"] == "fresh_complete"

    scheduled = await state._get_scheduled_items(
        {
            "course_ids": ("course-1",),
            "start_date": date(2026, 9, 19),
            "end_date": date(2026, 9, 20),
        }
    )
    assert scheduled["items"][0]["title"] == "Tutorial"
    assert scheduled["items"][0]["stable_id"] == "schedule-1"
    assert scheduled["applied_filters"]["completion"] == "incomplete"

    announcements = await state._get_announcements(
        {
            "course_ids": ("course-1",),
            "since": NOW - timedelta(days=1),
            "until": NOW,
        }
    )
    assert announcements["items"][0]["summary"] == _result().summary
    assert announcements["items"][0]["source_id"] == "announcement-1"
    assert "Quiz 2 moved to September 21. Review chapter 4 before class." not in str(announcements)
    zone = ZoneInfo("America/Toronto")
    assert connector.calls[0]["start_at"] == datetime(2026, 9, 19, tzinfo=zone)
    assert connector.calls[0]["end_at"] == datetime(2026, 9, 20, tzinfo=zone)
    assert connector.calls[1]["start_at"] == datetime(2026, 9, 19, tzinfo=zone)
    assert connector.calls[1]["end_at"] == datetime(2026, 9, 21, tzinfo=zone)
    assert connector.calls[2]["start_at"] == datetime(2026, 9, 18, tzinfo=zone)
    assert connector.calls[2]["end_at"] == datetime(2026, 9, 20, tzinfo=zone)


@pytest.mark.asyncio
async def test_learn_checkpoint_restores_course_capability_for_resumed_lookup() -> None:
    course = LearnCourse(
        org_unit_id="course-1",
        code="ECE 240",
        name="Electronic Circuits",
        active=True,
        url="https://learn.uwaterloo.ca/d2l/home/course-1",
    )
    item = LearnScheduledItem(
        source_id="schedule-1",
        course_org_unit_id="course-1",
        course_code="ECE 240",
        title="Tutorial",
        start_at=date(2026, 9, 20),
        date_precision=LearnDatePrecision.DATE,
        fingerprint="s" * 64,
    )
    connector = _Connector(
        LearnBridgeSnapshot(courses=(course,), scheduled_items=(item,), generated_at=NOW)
    )
    initial = LearnToolState(
        connector=connector,  # type: ignore[arg-type]
        semantic_interpreter=_Interpreter(),  # type: ignore[arg-type]
        now=NOW,
    )
    search = await initial._search_courses({"query": "ece"})

    resumed = LearnToolState(
        connector=connector,  # type: ignore[arg-type]
        semantic_interpreter=_Interpreter(),  # type: ignore[arg-type]
        now=NOW,
    )
    resumed.restore_checkpoint(initial.export_checkpoint())
    scheduled = await resumed._get_scheduled_items(
        {
            "course_ids": ("course-1",),
            "start_date": date(2026, 9, 19),
            "end_date": date(2026, 9, 20),
        }
    )

    assert resumed.query_envelope(search["query_id"]) is not None
    assert [entry["source_id"] for entry in scheduled["items"]] == ["schedule-1"]


@pytest.mark.asyncio
async def test_learn_tool_state_uses_toronto_windows_and_bounded_envelopes() -> None:
    courses = tuple(
        LearnCourse(
            org_unit_id=f"course-{index}",
            code=f"ECE {index:03d}",
            name=f"Course {index}",
            active=True,
            url=f"https://learn.example/course-{index}",
        )
        for index in range(25)
    )
    previous_local_day = LearnScheduledItem(
        source_id="schedule-previous",
        course_org_unit_id="course-1",
        course_code="ECE 001",
        title="Previous local day",
        start_at=datetime(2026, 9, 20, 3, 30, tzinfo=UTC),
        date_precision=LearnDatePrecision.DATETIME,
        fingerprint="p" * 64,
    )
    included = LearnScheduledItem(
        source_id="schedule-included",
        course_org_unit_id="course-1",
        course_code="ECE 001",
        title="Included local day",
        start_at=datetime(2026, 9, 20, 4, 30, tzinfo=UTC),
        date_precision=LearnDatePrecision.DATETIME,
        fingerprint="i" * 64,
    )
    completed = included.model_copy(
        update={
            "source_id": "schedule-completed",
            "title": "Completed local day",
            "completed": True,
        }
    )
    connector = _Connector(
        LearnBridgeSnapshot(
            courses=courses,
            scheduled_items=(previous_local_day, included, completed),
            generated_at=datetime(2026, 9, 20, 4, tzinfo=UTC),
        )
    )
    state = LearnToolState(
        connector=connector,  # type: ignore[arg-type]
        semantic_interpreter=_Interpreter(),  # type: ignore[arg-type]
        now=datetime(2026, 9, 20, 3, 30, tzinfo=UTC),
        timezone="America/Toronto",
    )

    search = await state._search_courses({"query": "", "limit": 20})
    scheduled = await state._get_scheduled_items(
        {
            "course_ids": ("course-1",),
            "start_date": date(2026, 9, 20),
            "end_date": date(2026, 9, 20),
        }
    )

    assert len(search["items"]) == 20
    assert search["has_more"] is True
    assert search["next_cursor"]
    assert (
        model_json_size({"content": search, "status": "succeeded"}) <= MODEL_TOOL_RESULT_MAX_CHARS
    )
    assert [item["source_id"] for item in scheduled["items"]] == ["schedule-included"]
    assert scheduled["has_more"] is False
    assert model_json_size({"content": scheduled, "status": "succeeded"}) <= (
        MODEL_TOOL_RESULT_MAX_CHARS
    )
    zone = ZoneInfo("America/Toronto")
    assert connector.calls[0]["start_at"] == datetime(2026, 9, 19, tzinfo=zone)
    assert connector.calls[0]["end_at"] == datetime(2026, 9, 20, tzinfo=zone)
    assert connector.calls[1]["start_at"] == datetime(2026, 9, 20, tzinfo=zone)
    assert connector.calls[1]["end_at"] == datetime(2026, 9, 21, tzinfo=zone)


@pytest.mark.asyncio
async def test_learn_tool_prepares_reserved_calendar_proposal_without_writing() -> None:
    class ProposalStore:
        def resolve_learn_calendar_targets(self):
            return (
                AcademicCourseMutationTarget(
                    course_id="reserved-course",
                    course_code="Classes + Tutorials + Labs",
                    data_source_id="reserved-source",
                    title_property_id="title-property",
                    date_property_id="date-property",
                    learn_context_property_id="learn-context-property",
                ),
            )

        def load_learn_calendar_candidates(self, **kwargs: object):
            return ()

    course = LearnCourse(
        org_unit_id="course-1",
        code="ECE 240",
        name="Electronic Circuits",
        active=True,
        url="https://learn.uwaterloo.ca/d2l/home/course-1",
    )
    item = LearnScheduledItem(
        source_id="schedule-1",
        course_org_unit_id="course-1",
        course_code="ECE 240",
        title="Tutorial",
        start_at=date(2026, 9, 20),
        date_precision=LearnDatePrecision.DATE,
        fingerprint="s" * 64,
    )
    connector = _Connector(
        LearnBridgeSnapshot(courses=(course,), scheduled_items=(item,), generated_at=NOW)
    )
    builder = LearnNotionProposalBuilder(
        model=_Model([]),
        store=ProposalStore(),
    )
    state = LearnToolState(
        connector=connector,  # type: ignore[arg-type]
        semantic_interpreter=_Interpreter(),  # type: ignore[arg-type]
        proposal_builder=builder,
        now=NOW,
    )

    await state._search_courses({"query": "ece"})
    result = await state._get_scheduled_items(
        {
            "course_ids": ("course-1",),
            "start_date": date(2026, 9, 19),
            "end_date": date(2026, 9, 20),
        }
    )
    source_id = result["items"][0]["proposal_source_id"]
    preview = await state._propose_calendar_change({"proposal_source_id": source_id})

    assert preview["review"] == "required"
    changes = state.proposed_changes()
    assert len(changes) == 1
    assert changes[0].field == "create_learn_calendar_event"
    assert changes[0].learn_date == date(2026, 9, 20)
    assert changes[0].learn_context == (
        "ECE 240: Tutorial\nSource: https://learn.uwaterloo.ca/d2l/home/course-1"
    )


@pytest.mark.asyncio
async def test_learn_tool_state_caps_arbitrary_windows() -> None:
    connector = _Connector(LearnBridgeSnapshot(generated_at=NOW))
    state = LearnToolState(
        connector=connector,  # type: ignore[arg-type]
        semantic_interpreter=_Interpreter(),  # type: ignore[arg-type]
        now=NOW,
    )
    state._known_courses["course-1"] = LearnCourse(
        org_unit_id="course-1",
        code="ECE 240",
        name="Electronic Circuits",
        active=True,
        url="https://learn.uwaterloo.ca/d2l/home/course-1",
    )

    with pytest.raises(ToolExecutionError, match="31-day window"):
        await state._get_scheduled_items(
            {
                "course_ids": ("course-1",),
                "start_date": date(2026, 9, 1),
                "end_date": date(2026, 10, 3),
            }
        )
