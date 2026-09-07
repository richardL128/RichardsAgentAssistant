from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agents.academic_planner.contracts import (
    Assessment,
    AssessmentType,
    AvailabilityWindow,
    PlanCritique,
    PlannerFacts,
    ProposedChange,
)
from app.agents.academic_planner.workflow import (
    LLMPlannerModel,
    build_daily_plan,
    confirm_checkin_proposal,
    create_checkin_proposal,
    run_end_of_day_checkin,
    run_morning_plan,
)


class Store:
    def __init__(self, facts: PlannerFacts | None = None) -> None:
        self.facts = facts or PlannerFacts()
        self.plan = None
        self.proposal = None
        self.applied = False
        self.applying = False

    def load_planner_facts(self, *, now, horizon_days):
        return self.facts

    def save_daily_plan(self, plan):
        self.plan = plan

    def get_latest_daily_plan(self):
        return self.plan

    def save_checkin_proposal(self, proposal):
        self.proposal = proposal

    def get_checkin_proposal(self, proposal_id):
        return self.proposal if self.proposal and self.proposal.proposal_id == proposal_id else None

    def prepare_checkin_application(self, proposal_id, confirmation_event):
        proposal = self.get_checkin_proposal(proposal_id)
        if proposal is None:
            return "not_found", None
        if self.applied:
            return "already_applied", proposal
        if self.applying:
            return "in_progress", proposal
        if confirmation_event != proposal.confirmation_event:
            return "confirmation_required", proposal
        self.applying = True
        return "ready", proposal

    def mark_checkin_applied(self, proposal_id, confirmation_event=None):
        self.applied = True
        self.applying = False


class Delivery:
    def __init__(self):
        self.calls = []

    async def send_morning_plan(self, plan, *, idempotency_key):
        self.calls.append(("morning", idempotency_key))

    async def send_checkin(self, *, plan, idempotency_key):
        self.calls.append(("checkin", idempotency_key))

    async def send_ambiguity_question(self, fact, *, idempotency_key):
        self.calls.append(("question", idempotency_key))

    async def send_confirmation(self, proposal, *, idempotency_key):
        self.calls.append(("confirmation", idempotency_key))

    async def send_focus_reviews(self, reviews, *, idempotency_key):
        self.calls.append(("focus_reviews", idempotency_key, tuple(reviews)))


class Writer:
    def __init__(self):
        self.calls = []

    async def apply_confirmed_changes(self, changes, *, proposal_id, confirmation_event):
        self.calls.append((changes, proposal_id, confirmation_event))


class Model:
    def __init__(self):
        self.breakdowns = 0

    async def breakdown(self, assessment):
        self.breakdowns += 1
        return {
            "assessment_id": assessment.id,
            "steps": ("work",),
            "estimated_minutes": 30,
            "rationale": "fact",
        }

    async def critique(self, plan):
        return PlanCritique(acceptable=True, concerns=())

    async def extract_checkin(self, reply):
        return (ProposedChange(field="completed", value="true", assessment_id="essay"),)


@pytest.mark.asyncio
async def test_morning_plan_persists_deterministic_plan_and_critique() -> None:
    assessment = Assessment(
        id="essay",
        course="CSC",
        title="Essay",
        assessment_type=AssessmentType.ASSIGNMENT,
        due_at=datetime(2026, 9, 5, 20, tzinfo=UTC),
        estimated_minutes=60,
        weight_percent=20,
    )
    store = Store(
        PlannerFacts(
            assessments=(assessment,),
            availability=(
                AvailabilityWindow(
                    start_at=datetime(2026, 9, 3, 14, tzinfo=UTC),
                    end_at=datetime(2026, 9, 3, 18, tzinfo=UTC),
                ),
            ),
        )
    )
    delivery = Delivery()
    model = Model()
    result = await run_morning_plan(
        store=store, delivery=delivery, model=model, now=datetime(2026, 9, 3, 14, tzinfo=UTC)
    )
    assert result["status"] == "succeeded"
    assert store.plan is not None
    assert store.plan.critique is not None
    assert model.breakdowns == 1
    assert delivery.calls[0][0] == "morning"


@pytest.mark.asyncio
async def test_setup_failure_stops_before_facts_model_schedule_or_delivery() -> None:
    class UnusedStore(Store):
        def load_planner_facts(self, *, now, horizon_days):
            raise AssertionError("planner facts must not load during a setup failure")

    class SetupSync:
        async def sync(self, *, now=None):
            return SimpleNamespace(
                status="setup_required",
                as_dict=lambda: {
                    "status": "setup_required",
                    "diagnostic_codes": ["notion_configuration_missing"],
                },
            )

    store, delivery, model = UnusedStore(), Delivery(), Model()
    result = await run_morning_plan(
        store=store,
        syncer=SetupSync(),
        delivery=delivery,
        model=model,
        now=datetime(2026, 9, 3, 14, tzinfo=UTC),
    )

    assert result["status"] == "setup_required"
    assert store.plan is None
    assert model.breakdowns == 0
    assert delivery.calls == []


@pytest.mark.asyncio
async def test_checkin_requires_exact_confirmation_before_notion_write() -> None:
    store = Store()
    delivery = Delivery()
    writer = Writer()
    proposal = await create_checkin_proposal(
        store=store, reply="done", model=Model(), delivery=delivery
    )
    denied = await confirm_checkin_proposal(
        store=store,
        writer=writer,
        proposal_id=proposal.proposal_id,
        confirmation_event=proposal.confirmation_event + " ",
    )
    assert denied["status"] == "confirmation_required"
    assert writer.calls == []
    applied = await confirm_checkin_proposal(
        store=store,
        writer=writer,
        proposal_id=proposal.proposal_id,
        confirmation_event=proposal.confirmation_event,
    )
    assert applied["status"] == "applied"
    assert len(writer.calls) == 1
    replay = await confirm_checkin_proposal(
        store=store,
        writer=writer,
        proposal_id=proposal.proposal_id,
        confirmation_event=proposal.confirmation_event,
    )
    assert replay["status"] == "applied"
    assert len(writer.calls) == 1


@pytest.mark.asyncio
async def test_end_of_day_checkin_has_explicit_send_path() -> None:
    delivery = Delivery()
    result = await run_end_of_day_checkin(
        plan=None, delivery=delivery, idempotency_key="academic-eod"
    )
    assert result["status"] == "sent"
    assert delivery.calls == [("checkin", "academic-eod")]


@pytest.mark.asyncio
async def test_end_of_day_checkin_advances_and_delivers_focus_review_lifecycle() -> None:
    class FocusStore(Store):
        def prepare_learning_focus_checkin(self, **kwargs):
            assert kwargs["snooze_after_missed"] == 2
            assert kwargs["delete_after_reminders"] == 5
            assert kwargs["next_review_at"] == datetime(2026, 9, 9, 1, tzinfo=UTC)
            return (
                {
                    "focus_id": "focus-recursion",
                    "course_code": "ECE 250",
                    "topic": "recursion",
                    "kind": "review",
                    "reminder_count": 0,
                },
            )

    delivery = Delivery()
    result = await run_end_of_day_checkin(
        plan=None,
        delivery=delivery,
        store=FocusStore(),
        idempotency_key="academic-eod-2026-09-07",
        now=datetime(2026, 9, 8, 1, tzinfo=UTC),
    )

    assert result["focus_review_count"] == 1
    assert result["focus_deleted_count"] == 0
    assert delivery.calls[0] == ("checkin", "academic-eod-2026-09-07")
    assert delivery.calls[1][0:2] == (
        "focus_reviews",
        "academic-eod-2026-09-07:learning-focus",
    )


def test_plan_identity_uses_toronto_date_at_utc_boundary() -> None:
    plan = build_daily_plan(PlannerFacts(), now=datetime(2026, 9, 3, 3, 30, tzinfo=UTC))
    prior = build_daily_plan(PlannerFacts(), now=datetime(2026, 9, 2, 23, 30, tzinfo=UTC))
    assert plan.plan_id == prior.plan_id


@pytest.mark.asyncio
async def test_llm_prompts_use_only_bounded_normalized_planner_fields() -> None:
    prompts: list[str] = []

    class Gateway:
        async def invoke_structured(self, *, prompt, response_model):
            prompts.append(prompt)
            if response_model is PlanCritique:
                return SimpleNamespace(output=PlanCritique(acceptable=True, concerns=()))
            return SimpleNamespace(output=None)

    assessment = Assessment(
        id="opaque-event-id",
        course="CSC",
        title="Essay",
        assessment_type=AssessmentType.ASSIGNMENT,
        due_at=datetime(2026, 9, 5, 20, tzinfo=UTC),
        estimated_minutes=60,
        weight_percent=20,
        citations=("raw-notion-envelope-must-not-cross",),
    )
    model = LLMPlannerModel(Gateway())
    await model.breakdown(assessment)
    plan = build_daily_plan(
        PlannerFacts(
            assessments=(assessment,),
            availability=(
                AvailabilityWindow(
                    start_at=datetime(2026, 9, 3, 14, tzinfo=UTC),
                    end_at=datetime(2026, 9, 3, 18, tzinfo=UTC),
                ),
            ),
        ),
        now=datetime(2026, 9, 3, 14, tzinfo=UTC),
    )
    plan = plan.model_copy(
        update={
            "blocks": tuple(
                block.model_copy(update={"rationale": "raw-notion-envelope-must-not-cross"})
                for block in plan.blocks
            )
        }
    )
    await model.critique(plan)
    changes = await model.extract_checkin("completed raw-notion-envelope-must-not-cross")

    assert len(changes) == 1
    assert len(prompts) == 2
    assert all("raw-notion-envelope-must-not-cross" not in prompt for prompt in prompts)
    assert '"assessment_id":"opaque-event-id"' in prompts[0]
    assert '"deferred_assessment_ids"' in prompts[1]
