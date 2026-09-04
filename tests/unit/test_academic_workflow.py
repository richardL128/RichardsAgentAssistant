from __future__ import annotations

from datetime import UTC, datetime

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
    build_daily_plan,
    confirm_checkin_proposal,
    create_checkin_proposal,
    run_end_of_day_checkin,
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


class Writer:
    def __init__(self):
        self.calls = []

    async def apply_confirmed_changes(self, changes, *, proposal_id):
        self.calls.append((changes, proposal_id))


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
    from app.agents.academic_planner.workflow import run_morning_plan

    result = await run_morning_plan(
        store=store, delivery=delivery, model=model, now=datetime(2026, 9, 3, 14, tzinfo=UTC)
    )
    assert result["status"] == "succeeded"
    assert store.plan is not None
    assert store.plan.critique is not None
    assert model.breakdowns == 1
    assert delivery.calls[0][0] == "morning"


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


def test_plan_identity_uses_toronto_date_at_utc_boundary() -> None:
    plan = build_daily_plan(PlannerFacts(), now=datetime(2026, 9, 3, 3, 30, tzinfo=UTC))
    prior = build_daily_plan(PlannerFacts(), now=datetime(2026, 9, 2, 23, 30, tzinfo=UTC))
    assert plan.plan_id == prior.plan_id
