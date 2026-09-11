from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.agents.job_interviews.contracts import PreparationPlanSnapshot
from app.agents.job_interviews.preparation import (
    PreparationPlanContent,
    PreparationPlanDecision,
    VerifiedRequirement,
    snapshot_from_plan,
    validate_plan_decision,
)


def _plan() -> PreparationPlanContent:
    return PreparationPlanContent(
        interview_id="interview-1",
        application_row_id="row-1",
        role_and_company_summary="Shopify backend developer interview.",
        interview_stage="technical",
        verified_requirements=(
            VerifiedRequirement(
                fact="Design and operate HTTP APIs.",
                source_ids=("posting:requirements",),
            ),
        ),
        preparation_topics=("API design", "behavioral examples"),
        daily_actions=("Practice one API design prompt for 45 minutes.",),
        source_ids=("posting:requirements", "notion:interview-title"),
        research_fingerprint="research-hash",
    )


def test_grounded_plan_accepts_only_supplied_source_ids() -> None:
    plan = validate_plan_decision(
        PreparationPlanDecision(outcome="ready", plan=_plan()),
        interview_id="interview-1",
        application_row_id="row-1",
        allowed_source_ids={"posting:requirements", "notion:interview-title"},
    )
    assert plan is not None
    assert plan.daily_actions[0].startswith("Practice")


def test_unknown_research_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown source"):
        validate_plan_decision(
            PreparationPlanDecision(outcome="ready", plan=_plan()),
            interview_id="interview-1",
            application_row_id="row-1",
            allowed_source_ids={"notion:interview-title"},
        )


def test_clarification_cannot_include_generic_plan() -> None:
    with pytest.raises(ValueError, match="cannot smuggle"):
        PreparationPlanDecision(
            outcome="needs_clarification",
            plan=_plan(),
            clarification_question="Please paste the posting.",
        )


def test_unchanged_plan_reuses_revision_and_material_change_versions() -> None:
    timestamp = datetime(2026, 9, 10, tzinfo=UTC)
    first, changed = snapshot_from_plan(
        _plan(), previous=None, generated_at=timestamp, material_change_reason="Initial plan"
    )
    assert changed
    assert first.revision == 1

    unchanged, changed = snapshot_from_plan(
        _plan(), previous=first, generated_at=timestamp, material_change_reason="Not material"
    )
    assert not changed
    assert unchanged.revision == 1

    revised_content = _plan().model_copy(
        update={"daily_actions": ("Practice two API design prompts.",)}
    )
    revised, changed = snapshot_from_plan(
        revised_content,
        previous=first,
        generated_at=timestamp,
        material_change_reason="Interview is closer",
    )
    assert changed
    assert revised.revision == 2


def test_snapshot_keeps_structured_plan_and_bounded_next_actions() -> None:
    snapshot, _ = snapshot_from_plan(
        _plan(),
        previous=PreparationPlanSnapshot(
            interview_page_id="interview-1",
            revision=1,
            generated_at=datetime(2026, 9, 9, tzinfo=UTC),
            plan_hash="old",
            summary="Old",
        ),
        generated_at=datetime(2026, 9, 10, tzinfo=UTC),
        material_change_reason="New posting evidence",
    )
    assert snapshot.plan["interview_id"] == "interview-1"
    assert snapshot.next_actions == ("Practice one API design prompt for 45 minutes.",)
