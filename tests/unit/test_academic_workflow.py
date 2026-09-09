from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.agents.academic_planner.contracts import (
    AcademicPerformanceSignal,
    Assessment,
    AssessmentType,
    AvailabilityWindow,
    GroundedAssessmentInsight,
    MorningBriefing,
    MorningBriefingContext,
    PlanCritique,
    PlannerFacts,
    PracticeNeed,
    ProposedChange,
)
from app.agents.academic_planner.workflow import (
    LLMPlannerModel,
    build_daily_plan,
    build_morning_briefing_context,
    confirm_checkin_proposal,
    create_checkin_proposal,
    relative_date_label,
    run_end_of_day_checkin,
    run_morning_plan,
    validate_morning_briefing,
)
from app.core.errors import ErrorCode, LifeAgentError

TORONTO = ZoneInfo("America/Toronto")


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
        self.morning_briefing = None

    async def send_morning_plan(self, briefing, *, idempotency_key):
        self.morning_briefing = briefing
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
    def __init__(self, briefing=None):
        self.breakdowns = 0
        self.morning_contexts: list[MorningBriefingContext] = []
        self.briefing = briefing

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

    async def morning_briefing(self, context):
        self.morning_contexts.append(context)
        if callable(self.briefing):
            return self.briefing(context)
        if self.briefing is not None:
            return self.briefing
        if not context.assessments and not context.scheduled_blocks:
            return MorningBriefing(
                message_text="Good morning, Richard. It is a light academic day. Have a good day!"
            )
        assessment = context.assessments[0]
        block = context.scheduled_blocks[0]
        return MorningBriefing(
            message_text=(
                f"Good morning, Richard. {assessment.course_code} {assessment.title} is due "
                f"{assessment.exact_date_label} ({assessment.relative_date_label}). Study "
                f"{block.duration_minutes} minutes. Have a good day!"
            ),
            referenced_assessment_ids=(assessment.assessment_id,),
            referenced_block_ids=(block.block_id,),
        )

    async def extract_checkin(self, reply):
        return (ProposedChange(field="completed", value="true", assessment_id="essay"),)


class MaterialReasoner:
    def __init__(self, *insights: GroundedAssessmentInsight) -> None:
        self.insights = insights
        self.calls = []

    async def generate_insights(self, assessment, scheduled_blocks, *, breakdown=None):
        self.calls.append((assessment.id, tuple(block.id for block in scheduled_blocks), breakdown))
        return tuple(insight for insight in self.insights if insight.assessment_id == assessment.id)


class SemanticValidator:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid
        self.calls: list[tuple[MorningBriefing, MorningBriefingContext]] = []

    async def validate_morning_briefing(
        self, briefing: MorningBriefing, context: MorningBriefingContext
    ) -> bool:
        self.calls.append((briefing, context))
        return self.valid


def _assessment(
    identifier: str,
    *,
    course: str = "CSC 101",
    title: str = "Essay",
    assessment_type: AssessmentType = AssessmentType.ASSIGNMENT,
    due_at: datetime,
    minutes: int = 60,
) -> Assessment:
    return Assessment(
        id=identifier,
        course=course,
        title=title,
        assessment_type=assessment_type,
        due_at=due_at,
        estimated_minutes=minutes,
        weight_percent=20,
        course_priority=80,
    )


def _availability(start: datetime, hours: int = 4) -> AvailabilityWindow:
    return AvailabilityWindow(start_at=start, end_at=start + timedelta(hours=hours))


def _briefing_with_all_context(context: MorningBriefingContext) -> MorningBriefing:
    assessment_text = " ".join(
        (
            f"{item.title} for {item.course_code} on "
            f"{item.exact_date_label} ({item.relative_date_label})."
        )
        for item in context.assessments
    )
    duration_text = " ".join(
        f"Study {item.duration_minutes} minutes for {item.title}."
        for item in context.scheduled_blocks
    )
    focus_text = " ".join(
        f"Keep practicing {item.course_code} {item.topic}."
        if item.course_code
        else f"Keep practicing {item.topic}."
        for item in context.learning_focuses
    )
    performance_text = " ".join(
        f"You performed well in {item.course_code}."
        if item.outcome == "positive"
        else f"{item.course_code} needs attention."
        for item in context.performance_signals
    )
    return MorningBriefing(
        message_text=(
            "Good morning, Richard. "
            + " ".join(
                part
                for part in (assessment_text, focus_text, performance_text, duration_text)
                if part
            )
            + " Have a good day!"
        ),
        referenced_assessment_ids=tuple(item.assessment_id for item in context.assessments),
        referenced_block_ids=tuple(item.block_id for item in context.scheduled_blocks),
        referenced_focus_ids=tuple(item.focus_id for item in context.learning_focuses),
        referenced_performance_signal_ids=tuple(
            item.signal_id for item in context.performance_signals
        ),
    )


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
    assert len(model.morning_contexts) == 1
    assert delivery.calls[0] == ("morning", f"academic-plan:{store.plan.plan_id}:v2")
    assert delivery.morning_briefing is not None
    assert delivery.morning_briefing.message_text.startswith("Good morning, Richard.")


@pytest.mark.asyncio
async def test_morning_plan_uses_verified_material_insights_for_scheduled_blocks_only() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    scheduled = _assessment(
        "assignment-1",
        course="ECE 222",
        title="Assignment 1",
        due_at=datetime(2026, 9, 10, 20, 0, tzinfo=TORONTO),
        minutes=60,
    )
    deferred = _assessment(
        "assignment-2",
        course="HIST 201",
        title="Essay",
        due_at=datetime(2026, 9, 18, 20, 0, tzinfo=TORONTO),
        minutes=90,
    )
    insight = GroundedAssessmentInsight(
        insight_id="insight-circuits",
        assessment_id="assignment-1",
        text="Assignment 1 is worth 40%; focus first on linear circuits and AC/DC contrast.",
        evidence_chunk_ids=("chunk-circuits",),
    )

    def briefing_with_material(context: MorningBriefingContext) -> MorningBriefing:
        assessment = context.assessments[0]
        block = context.scheduled_blocks[0]
        assert tuple(item.insight_id for item in context.material_insights) == ("insight-circuits",)
        assert context.work_breakdowns[0].assessment_id == "assignment-1"
        return MorningBriefing(
            message_text=(
                f"Good morning, Richard. {assessment.title} for {assessment.course_code} is due "
                f"{assessment.exact_date_label} ({assessment.relative_date_label}). "
                f"It is worth 40%; study {block.duration_minutes} minutes and focus first "
                "on linear circuits and AC/DC contrast. Have a good day!"
            ),
            referenced_assessment_ids=("assignment-1",),
            referenced_block_ids=(block.block_id,),
            referenced_insight_ids=("insight-circuits",),
        )

    delivery = Delivery()
    material_reasoner = MaterialReasoner(insight)
    semantic_validator = SemanticValidator()
    result = await run_morning_plan(
        store=Store(
            PlannerFacts(
                assessments=(scheduled, deferred),
                availability=(_availability(now, hours=1),),
            )
        ),
        delivery=delivery,
        model=Model(briefing_with_material),
        material_reasoner=material_reasoner,
        semantic_validator=semantic_validator,
        now=now,
    )

    assert result["status"] == "succeeded"
    assert result["material_insight_count"] == 1
    assert [call[0] for call in material_reasoner.calls] == ["assignment-1"]
    assert len(semantic_validator.calls) == 1
    assert delivery.morning_briefing.referenced_insight_ids == ("insight-circuits",)


@pytest.mark.asyncio
async def test_semantic_material_validator_blocks_delivery() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    assignment = _assessment(
        "assignment-1",
        course="ECE 222",
        title="Assignment 1",
        due_at=datetime(2026, 9, 10, 20, 0, tzinfo=TORONTO),
        minutes=45,
    )
    insight = GroundedAssessmentInsight(
        insight_id="insight-circuits",
        assessment_id="assignment-1",
        text="Assignment 1 is worth 40%; focus first on linear circuits.",
        evidence_chunk_ids=("chunk-circuits",),
    )

    def briefing_with_material(context: MorningBriefingContext) -> MorningBriefing:
        assessment = context.assessments[0]
        block = context.scheduled_blocks[0]
        return MorningBriefing(
            message_text=(
                f"Good morning, Richard. {assessment.title} for {assessment.course_code} is due "
                f"{assessment.exact_date_label} ({assessment.relative_date_label}). "
                f"It is worth 40%; study {block.duration_minutes} minutes and focus first "
                "on linear circuits. Have a good day!"
            ),
            referenced_assessment_ids=("assignment-1",),
            referenced_block_ids=(block.block_id,),
            referenced_insight_ids=("insight-circuits",),
        )

    delivery = Delivery()
    with pytest.raises(LifeAgentError, match="analysis_invalid_output"):
        await run_morning_plan(
            store=Store(
                PlannerFacts(
                    assessments=(assignment,),
                    availability=(_availability(now),),
                )
            ),
            delivery=delivery,
            model=Model(briefing_with_material),
            material_reasoner=MaterialReasoner(insight),
            semantic_validator=SemanticValidator(valid=False),
            now=now,
        )

    assert delivery.calls == []


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
async def test_morning_briefing_context_covers_quiz_tomorrow_and_assignment_next_week() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    quiz = _assessment(
        "quiz-1",
        course="ECE 222",
        title="Linear Circuits quiz",
        assessment_type=AssessmentType.QUIZ,
        due_at=datetime(2026, 9, 10, 10, 0, tzinfo=TORONTO),
        minutes=60,
    )
    assignment = _assessment(
        "assignment-1",
        course="MATH 137",
        title="Calculus assignment",
        due_at=datetime(2026, 9, 17, 20, 0, tzinfo=TORONTO),
        minutes=30,
    )
    delivery = Delivery()
    model = Model(_briefing_with_all_context)

    result = await run_morning_plan(
        store=Store(
            PlannerFacts(
                assessments=(quiz, assignment),
                availability=(_availability(now, hours=3),),
                horizon_days=14,
            )
        ),
        delivery=delivery,
        model=model,
        now=now,
        horizon_days=14,
    )

    assert result["status"] == "succeeded"
    context = model.morning_contexts[0]
    assert [(item.assessment_id, item.relative_date_label) for item in context.assessments] == [
        ("quiz-1", "tomorrow"),
        ("assignment-1", "next week"),
    ]
    assert {item.duration_minutes for item in context.scheduled_blocks} == {30, 60}
    assert set(delivery.morning_briefing.referenced_assessment_ids) == {
        "quiz-1",
        "assignment-1",
    }


@pytest.mark.asyncio
async def test_assignment_only_morning_uses_flexible_fact_mix() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    assignment = _assessment(
        "essay",
        course="HIST 201",
        title="Archive analysis",
        due_at=datetime(2026, 9, 11, 20, 0, tzinfo=TORONTO),
        minutes=45,
    )
    model = Model(_briefing_with_all_context)
    delivery = Delivery()

    result = await run_morning_plan(
        store=Store(PlannerFacts(assessments=(assignment,), availability=(_availability(now),))),
        delivery=delivery,
        model=model,
        now=now,
    )

    assert result["status"] == "succeeded"
    assert tuple(item.assessment_type for item in model.morning_contexts[0].assessments) == (
        AssessmentType.ASSIGNMENT,
    )
    assert "Archive analysis" in delivery.morning_briefing.message_text


@pytest.mark.asyncio
async def test_practice_briefing_uses_active_learning_focus_as_struggle_evidence() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    raw_reflection = "raw private reflection body must not cross"
    facts = PlannerFacts(
        practice_needs=(
            PracticeNeed(
                focus_id="focus-recursion",
                course_code="ECE 250",
                topic="recursion",
                target_minutes=30,
                next_review_at=now + timedelta(hours=4),
                source_action="reinforce_focus",
                rationale=raw_reflection,
            ),
        ),
        availability=(_availability(now),),
    )
    plan = build_daily_plan(facts, now=now)
    context = build_morning_briefing_context(facts, plan, now=now)

    assert context.learning_focuses[0].verified_context_label == (
        "Richard explicitly identified recursion as an academic struggle."
    )
    assert raw_reflection not in context.model_dump_json()
    briefing = MorningBriefing(
        message_text=(
            "Good morning, Richard. You have been struggling with ECE 250 recursion, "
            "so practice 30 minutes for Practice ECE 250 recursion. Have a good day!"
        ),
        referenced_block_ids=(context.scheduled_blocks[0].block_id,),
        referenced_focus_ids=("focus-recursion",),
    )
    assert validate_morning_briefing(briefing, context) == briefing


def test_positive_performance_requires_explicit_signal() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    facts = PlannerFacts(
        performance_signals=(
            AcademicPerformanceSignal(
                signal_id="signal-calculus-test",
                course_code="MATH 137",
                assessment_id="calculus-test",
                outcome="positive",
                observed_at=datetime(2026, 9, 8, 18, 0, tzinfo=TORONTO),
                verified_summary="Richard reported a strong result on the calculus test.",
            ),
        ),
    )
    context = build_morning_briefing_context(facts, build_daily_plan(facts, now=now), now=now)
    valid = MorningBriefing(
        message_text=(
            "Good morning, Richard. You performed well in MATH 137, and today is light. "
            "Have a good day!"
        ),
        referenced_performance_signal_ids=("signal-calculus-test",),
    )
    assert validate_morning_briefing(valid, context) == valid

    invalid = valid.model_copy(update={"referenced_performance_signal_ids": ()})
    with pytest.raises(LifeAgentError) as raised:
        validate_morning_briefing(invalid, context)
    assert raised.value.record.code is ErrorCode.ANALYSIS_INVALID_OUTPUT
    assert raised.value.record.retryable is True

    wrong_polarity_facts = PlannerFacts(
        performance_signals=(
            AcademicPerformanceSignal(
                signal_id="signal-needs-attention",
                course_code="MATH 137",
                outcome="needs_attention",
                observed_at=now,
                verified_summary="Richard explicitly reported difficulty.",
            ),
        )
    )
    wrong_polarity_context = build_morning_briefing_context(
        wrong_polarity_facts,
        build_daily_plan(wrong_polarity_facts, now=now),
        now=now,
    )
    with pytest.raises(LifeAgentError, match="analysis_invalid_output"):
        validate_morning_briefing(
            valid.model_copy(
                update={
                    "referenced_performance_signal_ids": ("signal-needs-attention",),
                }
            ),
            wrong_polarity_context,
        )


def test_no_deadline_practice_and_empty_day_messages_are_grounded() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    practice_facts = PlannerFacts(
        practice_needs=(
            PracticeNeed(
                focus_id="focus-circuits",
                course_code="ECE 222",
                topic="op amps",
                target_minutes=30,
                next_review_at=now + timedelta(hours=3),
                source_action="create_focus",
                rationale="Bounded local evidence.",
            ),
        ),
        availability=(_availability(now),),
    )
    practice_plan = build_daily_plan(practice_facts, now=now)
    practice_context = build_morning_briefing_context(practice_facts, practice_plan, now=now)
    practice_briefing = MorningBriefing(
        message_text=(
            "Good morning, Richard. No major deadlines are in the supplied facts; "
            "practice 30 minutes for ECE 222 op amps. Have a good day!"
        ),
        referenced_block_ids=(practice_context.scheduled_blocks[0].block_id,),
        referenced_focus_ids=("focus-circuits",),
    )
    assert validate_morning_briefing(practice_briefing, practice_context) == practice_briefing

    empty_context = build_morning_briefing_context(
        PlannerFacts(),
        build_daily_plan(PlannerFacts(), now=now),
        now=now,
    )
    light_day = MorningBriefing(
        message_text="Good morning, Richard. It is a light academic day. Have a good day!"
    )
    assert validate_morning_briefing(light_day, empty_context) == light_day

    sample_message = MorningBriefing(
        message_text=("Good morning, Richard. Test message using sample data. Have a good day!")
    )
    with pytest.raises(LifeAgentError, match="analysis_invalid_output"):
        validate_morning_briefing(sample_message, empty_context)

    invented_quiz = MorningBriefing(
        message_text=("Good morning, Richard. You have a calculus quiz tomorrow. Have a good day!")
    )
    with pytest.raises(LifeAgentError, match="analysis_invalid_output"):
        validate_morning_briefing(invented_quiz, empty_context)


def test_deferred_assessment_can_be_mentioned_without_fabricated_duration() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    assignment = _assessment(
        "assignment-1",
        course="HIST 201",
        title="Archive analysis",
        due_at=datetime(2026, 9, 12, 20, 0, tzinfo=TORONTO),
    )
    facts = PlannerFacts(assessments=(assignment,))
    plan = build_daily_plan(facts, now=now)
    context = build_morning_briefing_context(facts, plan, now=now)

    assert context.scheduled_blocks == ()
    assert context.deferred_assessment_ids == ("assignment-1",)
    valid = MorningBriefing(
        message_text=(
            "Good morning, Richard. Archive analysis for HIST 201 is due "
            "September 12, 2026 (this Saturday), but there is no planned block "
            "in the supplied plan. Have a good day!"
        ),
        referenced_assessment_ids=("assignment-1",),
    )
    assert validate_morning_briefing(valid, context) == valid

    invalid = valid.model_copy(
        update={
            "message_text": (
                "Good morning, Richard. Archive analysis for HIST 201 is due "
                "September 12, 2026 (this Saturday). Study 45 minutes for it. "
                "Have a good day!"
            )
        }
    )
    with pytest.raises(LifeAgentError):
        validate_morning_briefing(invalid, context)


def test_material_insight_references_are_assessment_block_and_percent_grounded() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    assignment = _assessment(
        "assignment-1",
        course="ECE 222",
        title="Assignment 1",
        due_at=datetime(2026, 9, 10, 20, 0, tzinfo=TORONTO),
        minutes=30,
    )
    plan = build_daily_plan(
        PlannerFacts(assessments=(assignment,), availability=(_availability(now),)),
        now=now,
    )
    insight = GroundedAssessmentInsight(
        insight_id="insight-circuits",
        assessment_id="assignment-1",
        text="Assignment 1 is worth 40%; practice linear circuits.",
        evidence_chunk_ids=("chunk-circuits",),
    )
    context = build_morning_briefing_context(
        PlannerFacts(assessments=(assignment,), availability=(_availability(now),)),
        plan,
        now=now,
        material_insights=(insight,),
    )
    block_id = context.scheduled_blocks[0].block_id
    valid = MorningBriefing(
        message_text=(
            "Good morning, Richard. Assignment 1 for ECE 222 is due September 10, 2026 "
            "(tomorrow). It is worth 40%; study 30 minutes for linear circuits. "
            "Have a good day!"
        ),
        referenced_assessment_ids=("assignment-1",),
        referenced_block_ids=(block_id,),
        referenced_insight_ids=("insight-circuits",),
    )
    assert validate_morning_briefing(valid, context) == valid

    with pytest.raises(LifeAgentError) as unknown_insight:
        validate_morning_briefing(
            valid.model_copy(update={"referenced_insight_ids": ("missing-insight",)}),
            context,
        )
    assert "unknown material insight" in unknown_insight.value.record.diagnostic
    with pytest.raises(LifeAgentError) as invented_percent:
        validate_morning_briefing(
            valid.model_copy(
                update={
                    "message_text": valid.message_text.replace("40%", "80%"),
                }
            ),
            context,
        )
    assert "percentage lacked verified insight" in invented_percent.value.record.diagnostic


def test_relative_date_labels_use_toronto_calendar_across_midnight_and_dst() -> None:
    assert (
        relative_date_label(
            date(2026, 9, 3),
            current_local_date=date(2026, 9, 2),
        )
        == "tomorrow"
    )
    assert (
        relative_date_label(
            date(2026, 9, 4),
            current_local_date=date(2026, 9, 2),
        )
        == "this Friday"
    )
    assert (
        relative_date_label(
            date(2026, 9, 10),
            current_local_date=date(2026, 9, 2),
        )
        == "next week"
    )

    utc_boundary_now = datetime(2026, 9, 3, 3, 30, tzinfo=UTC)
    utc_boundary_due = _assessment(
        "quiz",
        due_at=datetime(2026, 9, 3, 10, 0, tzinfo=TORONTO),
    )
    boundary_context = build_morning_briefing_context(
        PlannerFacts(assessments=(utc_boundary_due,)),
        build_daily_plan(PlannerFacts(assessments=(utc_boundary_due,)), now=utc_boundary_now),
        now=utc_boundary_now,
    )
    assert boundary_context.current_local_date == "2026-09-02"
    assert boundary_context.assessments[0].relative_date_label == "tomorrow"

    spring_now = datetime(2026, 3, 8, 0, 30, tzinfo=TORONTO)
    spring_assessment = _assessment(
        "spring",
        due_at=datetime(2026, 3, 8, 3, 30, tzinfo=TORONTO),
    )
    spring_context = build_morning_briefing_context(
        PlannerFacts(assessments=(spring_assessment,)),
        build_daily_plan(PlannerFacts(assessments=(spring_assessment,)), now=spring_now),
        now=spring_now,
    )
    assert spring_context.assessments[0].relative_date_label == "today"

    fall_now = datetime(2026, 11, 1, 0, 30, tzinfo=TORONTO)
    fall_assessment = _assessment(
        "fall",
        due_at=datetime(2026, 11, 2, 9, 0, tzinfo=TORONTO),
    )
    fall_context = build_morning_briefing_context(
        PlannerFacts(assessments=(fall_assessment,)),
        build_daily_plan(PlannerFacts(assessments=(fall_assessment,)), now=fall_now),
        now=fall_now,
    )
    assert fall_context.assessments[0].relative_date_label == "tomorrow"


@pytest.mark.parametrize(
    ("update", "match"),
    [
        ({"referenced_assessment_ids": ("unknown-assessment",)}, "unknown assessment"),
        ({"referenced_block_ids": ("unknown-block",)}, "unknown block"),
        ({"referenced_focus_ids": ("unknown-focus",)}, "unknown focus"),
        (
            {"referenced_performance_signal_ids": ("unknown-signal",)},
            "unknown performance signal",
        ),
        (
            {
                "message_text": (
                    "Good morning, Richard. Quiz for CSC 101 is due September 11, 2026 "
                    "(tomorrow). Study 30 minutes. Have a good day!"
                )
            },
            "changed or invented a calendar date",
        ),
        (
            {
                "message_text": (
                    "Good morning, Richard. Quiz for CSC 101 is due September 10, 2026 "
                    "(tomorrow). Study 45 minutes. Have a good day!"
                )
            },
            "changed or invented a scheduled duration",
        ),
        (
            {
                "message_text": (
                    "Good morning, Richard. Quiz for CSC 101 is due September 10, 2026 "
                    "(tomorrow). Study 30 minutes at 9:30 AM. Have a good day!"
                )
            },
            "changed or invented a scheduled time",
        ),
    ],
)
def test_morning_briefing_grounding_rejects_unknown_ids_dates_and_durations(
    update: dict[str, object],
    match: str,
) -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    quiz = _assessment(
        "quiz-1",
        title="Quiz",
        due_at=datetime(2026, 9, 10, 10, 0, tzinfo=TORONTO),
        minutes=30,
    )
    facts = PlannerFacts(assessments=(quiz,), availability=(_availability(now),))
    plan = build_daily_plan(facts, now=now)
    context = build_morning_briefing_context(facts, plan, now=now)
    valid = MorningBriefing(
        message_text=(
            "Good morning, Richard. Quiz for CSC 101 is due September 10, 2026 "
            "(tomorrow). Study 30 minutes. Have a good day!"
        ),
        referenced_assessment_ids=("quiz-1",),
        referenced_block_ids=(context.scheduled_blocks[0].block_id,),
    )

    with pytest.raises(LifeAgentError) as raised:
        validate_morning_briefing(valid.model_copy(update=update), context)
    assert match in raised.value.record.diagnostic


@pytest.mark.asyncio
async def test_invalid_morning_briefing_fails_workflow_before_delivery() -> None:
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    assessment = _assessment(
        "quiz-1",
        title="Quiz",
        due_at=datetime(2026, 9, 10, 10, 0, tzinfo=TORONTO),
        minutes=30,
    )
    delivery = Delivery()
    model = Model(
        MorningBriefing(
            message_text=(
                "Good morning, Richard. Quiz for CSC 101 is due September 10, 2026 "
                "(tomorrow). Study 30 minutes. Have a good day!"
            ),
            referenced_assessment_ids=("quiz-1",),
            referenced_block_ids=("unknown-block",),
        )
    )

    with pytest.raises(LifeAgentError) as raised:
        await run_morning_plan(
            store=Store(
                PlannerFacts(assessments=(assessment,), availability=(_availability(now),))
            ),
            delivery=delivery,
            model=model,
            now=now,
        )

    assert raised.value.record.code is ErrorCode.ANALYSIS_INVALID_OUTPUT
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


@pytest.mark.asyncio
async def test_llm_morning_prompt_uses_only_bounded_normalized_briefing_fields() -> None:
    prompts: list[str] = []

    class Gateway:
        async def invoke_structured(self, *, prompt, response_model):
            prompts.append(prompt)
            if response_model is MorningBriefing:
                return SimpleNamespace(
                    output=MorningBriefing(
                        message_text=(
                            "Good morning, Richard. It is a light academic day. Have a good day!"
                        )
                    )
                )
            return SimpleNamespace(output=None)

    raw_secret = "raw-notion-envelope-reflection-citation-must-not-cross"
    now = datetime(2026, 9, 9, 8, 0, tzinfo=TORONTO)
    assessment = _assessment(
        "opaque-event-id",
        course="CSC",
        title="Essay",
        due_at=datetime(2026, 9, 10, 20, tzinfo=TORONTO),
    ).model_copy(update={"citations": (raw_secret,)})
    facts = PlannerFacts(
        assessments=(assessment,),
        practice_needs=(
            PracticeNeed(
                focus_id="focus-secret",
                course_code="CSC",
                topic="recursion",
                target_minutes=30,
                next_review_at=now + timedelta(hours=2),
                source_action="reinforce_focus",
                rationale=raw_secret,
            ),
        ),
        availability=(_availability(now),),
    )
    plan = build_daily_plan(facts, now=now)
    context = build_morning_briefing_context(facts, plan, now=now)
    model = LLMPlannerModel(Gateway())

    await model.morning_briefing(context)

    assert len(prompts) == 1
    assert raw_secret not in prompts[0]
    assert '"assessment_id":"opaque-event-id"' in prompts[0]
    assert "Linear Circuits" not in prompts[0]
    assert "Calculus tutorial" not in prompts[0]
    assert "sample data" not in prompts[0].casefold()
    assert (
        '"verified_context_label":"Richard explicitly identified recursion as an '
        'academic struggle."' in prompts[0]
    )


@pytest.mark.asyncio
async def test_llm_morning_invalid_schema_output_is_retryable_failure() -> None:
    class Gateway:
        async def invoke_structured(self, *, prompt, response_model):
            return SimpleNamespace(output=None, error_code="analysis_invalid_output")

    with pytest.raises(LifeAgentError) as raised:
        await LLMPlannerModel(Gateway()).morning_briefing(
            MorningBriefingContext(current_local_date="2026-09-09")
        )
    assert raised.value.record.code is ErrorCode.ANALYSIS_INVALID_OUTPUT
    assert raised.value.record.retryable is True
