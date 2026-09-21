from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.agents.academic_planner.nightly_task_semantics import (
    NightlyTaskEligibilityDecision,
    NightlyTaskEligibilityResult,
    NightlyTaskEligibilityStatus,
    NightlyTaskEvidenceFragment,
    NightlyTaskEvidenceSourceKind,
    NightlyTaskSemanticCritique,
    NightlyTaskSemanticInput,
    NightlyTaskSemanticInterpreter,
    with_nightly_task_title_evidence,
)


class _FakeGateway:
    model_identity = "qwen-test"
    config_version = "nightly-test-config"

    def __init__(self, outputs: Sequence[Any], *, delay_seconds: float = 0) -> None:
        self.outputs = list(outputs)
        self.delay_seconds = delay_seconds
        self.prompts: list[str] = []
        self.response_models: list[type[Any]] = []

    async def invoke_structured(self, *, prompt: str, response_model: type[Any]) -> Any:
        self.prompts.append(prompt)
        self.response_models.append(response_model)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if not self.outputs:
            raise AssertionError("fake gateway received an unexpected model call")
        return SimpleNamespace(output=self.outputs.pop(0))


def _fragment(
    fragment_id: str,
    text: str,
    *,
    event_id: str = "event-1",
    source_label: str = "Validated semantic overview",
    ordinal: int = 1,
) -> NightlyTaskEvidenceFragment:
    return NightlyTaskEvidenceFragment(
        fragment_id=fragment_id,
        event_id=event_id,
        source_kind=NightlyTaskEvidenceSourceKind.VALIDATED_SEMANTIC_CONTEXT,
        source_label=source_label,
        text=text,
        ordinal=ordinal,
    )


def _item(
    *,
    title: str = "Study for the midterm",
    fragments: tuple[NightlyTaskEvidenceFragment, ...] | None = None,
    local_start_label: str | None = None,
    local_end_label: str | None = None,
    is_all_day: bool = True,
    is_range: bool = False,
) -> NightlyTaskSemanticInput:
    supplied = with_nightly_task_title_evidence(
        event_id="event-1",
        title=title,
        fragments=fragments
        if fragments is not None
        else (_fragment("frag-1", "Owner-created course work session for exam preparation."),),
    )
    return NightlyTaskSemanticInput(
        event_id="event-1",
        course_id="course-1",
        course_code="ECE 250",
        course_title="Data Structures and Algorithms",
        title=title,
        local_date_label="Sunday, September 20, 2026",
        local_start_label=local_start_label,
        local_end_label=local_end_label,
        is_all_day=is_all_day,
        is_range=is_range,
        source_fingerprint="sha256:" + ("f" * 64),
        source_last_edited_at=datetime(2026, 9, 20, 12, tzinfo=UTC),
        evidence_fragments=supplied,
    )


def _result(
    decision: NightlyTaskEligibilityDecision = NightlyTaskEligibilityDecision.MOVABLE_WORK_TASK,
    *,
    event_id: str = "event-1",
    rationale: str = "The evidence describes an owner-performable study/work session.",
    evidence_fragment_ids: tuple[str, ...] = ("event-1:host:title", "frag-1"),
) -> NightlyTaskEligibilityResult:
    return NightlyTaskEligibilityResult(
        event_id=event_id,
        decision=decision,
        rationale=rationale,
        evidence_fragment_ids=evidence_fragment_ids,
    )


def _critique(
    accepted: bool = True,
    *,
    reason: str | None = None,
    decision_supported: bool | None = None,
    rationale_supported: bool | None = None,
    common_supported: bool | None = None,
    boundary_supported: bool | None = None,
) -> NightlyTaskSemanticCritique:
    common = accepted if common_supported is None else common_supported
    return NightlyTaskSemanticCritique(
        accepted=accepted,
        decision_supported=accepted if decision_supported is None else decision_supported,
        rationale_supported=accepted if rationale_supported is None else rationale_supported,
        no_invented_claims=common,
        no_instruction_following=common,
        same_event=common,
        cites_only_supplied_fragments=common,
        movable_fixed_boundary_respected=(
            accepted if boundary_supported is None else boundary_supported
        ),
        reason=reason,
    )


def test_result_schema_requires_strict_citations() -> None:
    with pytest.raises(ValidationError, match="citations must be unique"):
        _result(evidence_fragment_ids=("event-1:host:title", "event-1:host:title"))

    schema = NightlyTaskEligibilityResult.model_json_schema()
    assert {"event_id", "decision", "rationale", "evidence_fragment_ids"}.issubset(
        schema["required"]
    )


def test_input_rejects_cross_event_fragments() -> None:
    with pytest.raises(ValueError, match="cannot mix events"):
        _item(fragments=(_fragment("frag-1", "Other event text", event_id="other-event"),))


@pytest.mark.parametrize(
    ("title", "evidence", "decision", "expected_status"),
    [
        (
            "Study for the midterm",
            "Owner-created review block for preparing midterm topics.",
            NightlyTaskEligibilityDecision.MOVABLE_WORK_TASK,
            NightlyTaskEligibilityStatus.ELIGIBLE,
        ),
        (
            "ECE 250 midterm",
            "Scheduled midterm sitting from 7:00 PM to 9:00 PM.",
            NightlyTaskEligibilityDecision.FIXED_COMMITMENT,
            NightlyTaskEligibilityStatus.NOT_ELIGIBLE,
        ),
        (
            "Review assignment feedback",
            "Task to read comments and revise understanding after grading.",
            NightlyTaskEligibilityDecision.MOVABLE_WORK_TASK,
            NightlyTaskEligibilityStatus.ELIGIBLE,
        ),
        (
            "Assignment 3 due",
            "Hard due date for submitting Assignment 3.",
            NightlyTaskEligibilityDecision.FIXED_COMMITMENT,
            NightlyTaskEligibilityStatus.NOT_ELIGIBLE,
        ),
    ],
)
@pytest.mark.asyncio
async def test_contrast_examples_are_model_semantic_decisions_not_title_rules(
    title: str,
    evidence: str,
    decision: NightlyTaskEligibilityDecision,
    expected_status: NightlyTaskEligibilityStatus,
) -> None:
    item = _item(title=title, fragments=(_fragment("frag-1", evidence),))
    gateway = _FakeGateway([_result(decision), _critique()])

    outcome = await NightlyTaskSemanticInterpreter(gateway).analyze(item)

    assert outcome.status is expected_status
    assert outcome.result is not None
    assert outcome.result.decision is decision
    assert outcome.movable is (decision is NightlyTaskEligibilityDecision.MOVABLE_WORK_TASK)
    assert len(gateway.prompts) == 2
    assert f'"text":"{title}"' in gateway.prompts[0]
    assert "not keywords, regexes, field names" in gateway.prompts[0]
    assert "assessment type labels" in gateway.prompts[0]
    assert "placement alone does not make it a fixed commitment" in gateway.prompts[0]
    assert "schedule placement, not by itself evidence" in gateway.prompts[1]


@pytest.mark.asyncio
async def test_uncertain_is_accepted_but_not_eligible() -> None:
    gateway = _FakeGateway(
        [
            _result(
                NightlyTaskEligibilityDecision.UNCERTAIN,
                rationale="The evidence names an item but does not establish work or hard date.",
            ),
            _critique(),
        ]
    )

    outcome = await NightlyTaskSemanticInterpreter(gateway).analyze(_item(title="Project item"))

    assert outcome.status is NightlyTaskEligibilityStatus.NOT_ELIGIBLE
    assert outcome.result is not None
    assert outcome.result.decision is NightlyTaskEligibilityDecision.UNCERTAIN
    assert outcome.movable is False


@pytest.mark.asyncio
async def test_missing_evidence_fails_closed_without_model_call() -> None:
    item = NightlyTaskSemanticInput(
        event_id="event-1",
        course_id="course-1",
        course_code="ECE 250",
        title="Study for the midterm",
        local_date_label="Sunday, September 20, 2026",
        source_fingerprint="sha256:" + ("f" * 64),
        evidence_fragments=(),
    )
    gateway = _FakeGateway([])

    outcome = await NightlyTaskSemanticInterpreter(gateway).analyze(item)

    assert outcome.status is NightlyTaskEligibilityStatus.UNAVAILABLE
    assert outcome.result is None
    assert outcome.movable is False
    assert outcome.error_code == "nightly_task_semantic_no_evidence"
    assert gateway.prompts == []


@pytest.mark.asyncio
async def test_model_unavailable_fails_closed() -> None:
    gateway = _FakeGateway([None])

    outcome = await NightlyTaskSemanticInterpreter(gateway).analyze(_item())

    assert outcome.status is NightlyTaskEligibilityStatus.UNAVAILABLE
    assert outcome.result is None
    assert outcome.movable is False
    assert outcome.error_code == "nightly_task_semantic_model_unavailable"
    assert len(gateway.prompts) == 1


@pytest.mark.asyncio
async def test_model_timeout_fails_closed() -> None:
    gateway = _FakeGateway([], delay_seconds=0.02)

    outcome = await NightlyTaskSemanticInterpreter(
        gateway,
        model_timeout_seconds=0.001,
    ).analyze(_item())

    assert outcome.status is NightlyTaskEligibilityStatus.UNAVAILABLE
    assert outcome.result is None
    assert outcome.movable is False
    assert outcome.error_code == "nightly_task_semantic_model_unavailable"
    assert len(gateway.prompts) == 1


@pytest.mark.asyncio
async def test_host_validation_rejects_changed_event_id_and_repairs_once() -> None:
    gateway = _FakeGateway(
        [
            _result(event_id="other-event"),
            _result(),
            _critique(),
        ]
    )

    outcome = await NightlyTaskSemanticInterpreter(gateway).analyze(_item())

    assert outcome.status is NightlyTaskEligibilityStatus.ELIGIBLE
    assert outcome.result is not None
    assert outcome.result.event_id == "event-1"
    assert len(gateway.prompts) == 3
    assert "Repair the rejected result" in gateway.prompts[1]


@pytest.mark.asyncio
async def test_unknown_citation_remains_invalid_after_one_repair_attempt() -> None:
    gateway = _FakeGateway(
        [
            _result(evidence_fragment_ids=("event-1:host:title", "missing-frag")),
            _result(evidence_fragment_ids=("event-1:host:title", "still-missing")),
        ]
    )

    outcome = await NightlyTaskSemanticInterpreter(gateway).analyze(_item())

    assert outcome.status is NightlyTaskEligibilityStatus.INVALID
    assert outcome.result is None
    assert outcome.movable is False
    assert outcome.error_code == "nightly_task_semantic_critic_rejected"
    assert outcome.reason == "nightly task semantic result cited unknown evidence fragments"
    assert len(gateway.prompts) == 2


@pytest.mark.asyncio
async def test_critic_rejection_allows_one_repair_then_invalid() -> None:
    gateway = _FakeGateway(
        [
            _result(),
            _critique(False, reason="rationale treats a hard due date as movable"),
            _result(),
            _critique(False, reason="repair still misses the hard-date boundary"),
        ]
    )

    outcome = await NightlyTaskSemanticInterpreter(gateway).analyze(_item())

    assert outcome.status is NightlyTaskEligibilityStatus.INVALID
    assert outcome.error_code == "nightly_task_semantic_critic_rejected"
    assert outcome.reason == "repair still misses the hard-date boundary"
    assert len(gateway.prompts) == 4
    assert "Repair the rejected result" in gateway.prompts[2]


@pytest.mark.asyncio
async def test_critic_unavailable_fails_closed() -> None:
    gateway = _FakeGateway([_result(), None])

    outcome = await NightlyTaskSemanticInterpreter(gateway).analyze(_item())

    assert outcome.status is NightlyTaskEligibilityStatus.UNAVAILABLE
    assert outcome.result is None
    assert outcome.movable is False
    assert outcome.error_code == "nightly_task_semantic_critic_unavailable"
    assert len(gateway.prompts) == 2
