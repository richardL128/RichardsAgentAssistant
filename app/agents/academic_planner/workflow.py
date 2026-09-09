"""Academic planning orchestration with deterministic state-changing boundaries."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from app.agents.academic_planner.allocator import allocate_plan_with_deferred, priority_score
from app.agents.academic_planner.contracts import (
    AmbiguousFact,
    CheckinProposal,
    DailyPlan,
    GroundedAssessmentInsight,
    MorningBriefing,
    MorningBriefingAssessmentFact,
    MorningBriefingBlockFact,
    MorningBriefingContext,
    MorningBriefingLearningFocusFact,
    MorningBriefingMaterialInsightFact,
    MorningBriefingPerformanceFact,
    MorningBriefingWorkBreakdownFact,
    PlanCritique,
    PlannerFacts,
    PracticeNeed,
    ProposedChange,
    WorkBreakdown,
)
from app.core.errors import ErrorCode, transient_error

TORONTO = ZoneInfo("America/Toronto")

_MORNING_BRIEFING_PROMPT = """Write a concise, conversational morning academic briefing for Richard.

Use only the supplied briefing facts. The deterministic schedule is authoritative:
do not change deadlines, dates, block durations, or planned start times.

Requirements:
- Begin naturally with "Good morning, Richard."
- Mention a flexible selection of the most relevant approaching tests, quizzes,
  assignments, and tutorials. Do not force every category into every briefing.
- Prefer imminent and high-priority deadlines, but mention a later deadline when
  it explains a useful review block today.
- Express dates conversationally, such as "tomorrow," "this Friday," or
  "next week," while retaining the exact supplied calendar date.
- Connect recommendations to the supplied scheduled study blocks.
- Include exact study durations from the schedule.
- Use supplied material insights when they make today's scheduled work more
  actionable, but do not force every assessment to mention source material.
- Treat material insights as already verified summaries; do not invent
  percentages, topics, requirements, or source claims beyond those insights.
- Mention a recent struggle or positive performance only when an explicit,
  verified performance signal is supplied.
- A supplied learning focus is explicit, verified evidence of that struggle;
  use only its bounded verified_context_label. Positive performance still
  requires a supplied performance signal.
- Do not infer performance from task completion, priority scores, confidence
  gaps, or missing data.
- Omit sections that have no relevant facts.
- Keep the result warm, direct, and brief.
- End naturally, such as "Have a good day!"
- Never label the briefing as a test, demo, sample, placeholder, or example.
- Do not include any course, event, date, duration, or performance claim unless
  it appears in the supplied briefing facts.

Return one JSON object matching the supplied MorningBriefing schema.

Briefing facts:
"""

_DATE_PATTERN = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}(?:,\s+\d{4})?\b",
    re.IGNORECASE,
)
_ISO_DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_COURSE_CODE_PATTERN = re.compile(r"\b[A-Z]{2,6}(?:[- ]?\d{2,4})[A-Z]?\b")
_TIME_PATTERN = re.compile(
    r"\b(?:(?:[01]?\d|2[0-3]):[0-5]\d|(?:1[0-2]|[1-9])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?))\b",
    re.IGNORECASE,
)
_NUMERIC_DURATION_PATTERN = re.compile(
    r"\b(\d{1,4})\s*(?:-|\s)?(minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)
_WORD_HOUR_PATTERN = re.compile(
    r"\b(a|an|one|two|three|four|half)\s+(?:-|\s)?hours?\b",
    re.IGNORECASE,
)
_PERCENT_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\s*%(?=\W|$)")
_RECOMMENDATION_PATTERN = re.compile(
    r"\b(?:recommend|study|review|practice|work on|spend)\b", re.IGNORECASE
)
_ASSESSMENT_TERM_PATTERN = re.compile(
    r"\b(?:assignment|exam|final|lab|midterm|quiz|test|tutorial)\b",
    re.IGNORECASE,
)
_POSITIVE_PERFORMANCE_PATTERN = re.compile(
    r"\b(?:performed well|did well|improved|succeeded|strong result|good result)\b",
    re.IGNORECASE,
)
_STRUGGLE_PATTERN = re.compile(
    r"\b(?:struggle|struggled|struggling|had difficulty|needs attention)\b",
    re.IGNORECASE,
)
_NON_PRODUCTION_BRIEFING_PATTERN = re.compile(
    r"\b(?:test message|sample data|demo message|placeholder message)\b",
    re.IGNORECASE,
)


class AcademicPlannerStore(Protocol):
    """Persistence seam for facts, plans, and confirmation proposals."""

    def load_planner_facts(self, *, now: datetime, horizon_days: int) -> PlannerFacts: ...

    def save_daily_plan(self, plan: DailyPlan) -> None: ...

    def get_latest_daily_plan(self) -> DailyPlan | None: ...

    def save_checkin_proposal(self, proposal: CheckinProposal) -> None: ...

    def get_checkin_proposal(self, proposal_id: uuid.UUID) -> CheckinProposal | None: ...

    def prepare_checkin_application(
        self,
        proposal_id: uuid.UUID,
        confirmation_event: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, CheckinProposal | None]: ...

    def mark_checkin_applied(
        self, proposal_id: uuid.UUID, confirmation_event: str | None = None
    ) -> None: ...

    def reject_checkin_proposal(
        self,
        proposal_id: uuid.UUID,
        *,
        actor: str = "academic_planner",
        now: datetime | None = None,
    ) -> tuple[str, CheckinProposal | None]: ...


class NotionAcademicWriter(Protocol):
    """Narrow write seam; only exact confirmed changes reach this protocol."""

    async def apply_confirmed_changes(
        self,
        changes: Sequence[ProposedChange],
        *,
        proposal_id: uuid.UUID,
        confirmation_event: str,
    ) -> None: ...


class PlannerModelGateway(Protocol):
    async def breakdown(self, assessment: Any) -> WorkBreakdown: ...

    async def critique(self, plan: DailyPlan) -> PlanCritique: ...

    async def morning_briefing(self, context: MorningBriefingContext) -> MorningBriefing: ...

    async def extract_checkin(self, reply: str) -> Sequence[ProposedChange]: ...


class AcademicMaterialReasoner(Protocol):
    """Read-only semantic assessment-material reasoning boundary."""

    async def generate_insights(
        self,
        assessment: Any,
        scheduled_blocks: Sequence[Any],
        *,
        breakdown: WorkBreakdown | None = None,
    ) -> Sequence[GroundedAssessmentInsight]: ...


class MorningBriefingSemanticValidator(Protocol):
    """Optional final-message critic for semantic claims deterministic checks cannot prove."""

    async def validate_morning_briefing(
        self,
        briefing: MorningBriefing,
        context: MorningBriefingContext,
    ) -> bool: ...


class AcademicSynchronizer(Protocol):
    """Pre-planning ingestion seam with a bounded, non-secret result."""

    async def sync(self, *, now: datetime | None = None) -> Any: ...


class LLMPlannerModel:
    """Adapter that keeps Qwen output advisory and schema-validated."""

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    async def breakdown(self, assessment: Any) -> WorkBreakdown:
        payload = {
            "assessment_id": assessment.id,
            "course_code": assessment.course,
            "title": assessment.title,
            "assessment_type": assessment.assessment_type.value,
            "due_at": assessment.due_at.isoformat(),
            "estimated_minutes": assessment.estimated_minutes,
            "weight_percent": assessment.weight_percent,
            "scope_size": assessment.scope_size,
        }
        result = await self._gateway.invoke_structured(
            prompt=(
                "Propose a concise work breakdown for this assessment. "
                "Do not change dates or calendar state. The identifier is opaque.\n"
                + json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            ),
            response_model=WorkBreakdown,
        )
        if result.output is None:
            return WorkBreakdown(
                assessment_id=assessment.id,
                steps=("Review requirements",),
                estimated_minutes=assessment.estimated_minutes,
                rationale="No model breakdown was available.",
            )
        return result.output

    async def critique(self, plan: DailyPlan) -> PlanCritique:
        payload = {
            "plan_id": str(plan.plan_id),
            "blocks": [
                {
                    "block_id": block.id,
                    "assessment_id": block.assessment_id,
                    "title": block.title,
                    "start_at": block.start_at.isoformat(),
                    "end_at": block.end_at.isoformat(),
                    "carried_over": block.carried_over,
                }
                for block in plan.blocks
            ],
            "deferred_assessment_ids": list(plan.deferred_assessment_ids),
        }
        result = await self._gateway.invoke_structured(
            prompt="Critique this candidate plan for conflicts and unrealistic load.\n"
            + json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
            response_model=PlanCritique,
        )
        return result.output or PlanCritique(acceptable=True, concerns=())

    async def morning_briefing(self, context: MorningBriefingContext) -> MorningBriefing:
        payload = json.dumps(
            context.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        result = await self._gateway.invoke_structured(
            prompt=_MORNING_BRIEFING_PROMPT + payload,
            response_model=MorningBriefing,
        )
        if result.output is None:
            error_code = str(getattr(result, "error_code", ""))
            code = (
                ErrorCode.MODEL_TRANSIENT
                if error_code.startswith("model_")
                else ErrorCode.ANALYSIS_INVALID_OUTPUT
            )
            raise transient_error(code, "Morning briefing model output was unavailable or invalid")
        return result.output

    async def extract_checkin(self, reply: str) -> Sequence[ProposedChange]:
        # Check-in replies may originate in a private Discord channel. Keep
        # their content out of model prompts and accept only the conservative,
        # explicit local grammar below.
        return _fallback_extract(reply)


class PlannerDelivery(Protocol):
    async def send_morning_plan(
        self, briefing: MorningBriefing, *, idempotency_key: str
    ) -> object: ...

    async def send_checkin(self, *, plan: DailyPlan | None, idempotency_key: str) -> object: ...

    async def send_ambiguity_question(
        self, fact: AmbiguousFact, *, idempotency_key: str
    ) -> object: ...

    async def send_confirmation(
        self, proposal: CheckinProposal, *, idempotency_key: str
    ) -> object: ...


def _plan_id(local_date: date) -> uuid.UUID:
    return uuid.uuid5(uuid.UUID("cbdb9cb0-e304-48ad-a093-174aac3903ad"), local_date.isoformat())


def build_daily_plan(
    facts: PlannerFacts,
    *,
    now: datetime,
    critique: PlanCritique | None = None,
) -> DailyPlan:
    """Build a deterministic plan; ambiguous facts become questions only."""

    blocks, deferred_practice = allocate_plan_with_deferred(facts, now=now)
    deferred = tuple(
        assessment.id
        for assessment in facts.assessments
        if not assessment.completed
        and not assessment.ambiguous
        and assessment.id not in {block.assessment_id for block in blocks}
    )
    current = now if now.tzinfo and now.utcoffset() is not None else now.replace(tzinfo=UTC)
    return DailyPlan(
        plan_id=_plan_id(current.astimezone(TORONTO).date()),
        created_at=current,
        blocks=blocks,
        deferred_assessment_ids=deferred,
        deferred_practice_focus_ids=deferred_practice,
        ambiguous_questions=facts.ambiguous_facts,
        critique=critique,
    )


def _exact_date_label(value: date) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def relative_date_label(value: date, *, current_local_date: date) -> str:
    """Describe a date in Toronto calendar terms without model calculation."""

    delta = (value - current_local_date).days
    if delta < 0:
        return "overdue"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    current_week = current_local_date - timedelta(days=current_local_date.weekday())
    value_week = value - timedelta(days=value.weekday())
    if value_week == current_week:
        return f"this {value.strftime('%A')}"
    if value_week == current_week + timedelta(days=7):
        return "next week"
    return f"in {delta} days"


def build_morning_briefing_context(
    facts: PlannerFacts,
    plan: DailyPlan,
    *,
    now: datetime,
    material_insights: Sequence[GroundedAssessmentInsight] = (),
    breakdowns: Sequence[WorkBreakdown] = (),
) -> MorningBriefingContext:
    """Build the bounded Toronto-local facts that may cross the model boundary."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local_date = now.astimezone(TORONTO).date()
    ranked = sorted(
        (
            assessment
            for assessment in facts.assessments
            if not assessment.completed and not assessment.ambiguous
        ),
        key=lambda item: (-priority_score(item, now=now), item.due_at, item.id),
    )[:50]
    assessment_facts = tuple(
        MorningBriefingAssessmentFact(
            assessment_id=assessment.id,
            course_code=assessment.course,
            title=assessment.title,
            assessment_type=assessment.assessment_type,
            due_at_local=assessment.due_at.astimezone(TORONTO).isoformat(timespec="minutes"),
            exact_date_label=_exact_date_label(assessment.due_at.astimezone(TORONTO).date()),
            relative_date_label=relative_date_label(
                assessment.due_at.astimezone(TORONTO).date(),
                current_local_date=local_date,
            ),
            priority_rank=rank,
        )
        for rank, assessment in enumerate(ranked, start=1)
    )
    allowed_assessment_ids = {item.assessment_id for item in assessment_facts}
    selected_focus_needs: list[PracticeNeed] = []
    selected_focus_ids: set[str] = set()
    for need in facts.practice_needs:
        if need.focus_id is None or need.focus_id in selected_focus_ids:
            continue
        selected_focus_needs.append(need)
        selected_focus_ids.add(need.focus_id)
        if len(selected_focus_needs) == 20:
            break
    block_facts: list[MorningBriefingBlockFact] = []
    for block in plan.blocks:
        if block.block_kind == "assessment" and block.assessment_id not in allowed_assessment_ids:
            continue
        if block.block_kind == "practice" and block.learning_focus_id not in selected_focus_ids:
            continue
        seconds = int((block.end_at - block.start_at).total_seconds())
        if seconds <= 0 or seconds % 60:
            raise ValueError("scheduled block duration must be a positive whole minute")
        block_facts.append(
            MorningBriefingBlockFact(
                block_id=block.id,
                assessment_id=block.assessment_id,
                learning_focus_id=block.learning_focus_id,
                block_kind=block.block_kind,
                title=block.title,
                start_at_local=block.start_at.astimezone(TORONTO).isoformat(timespec="minutes"),
                duration_minutes=seconds // 60,
                carried_over=block.carried_over,
            )
        )
        if len(block_facts) == 100:
            break
    focus_by_id: dict[str, MorningBriefingLearningFocusFact] = {}
    for need in selected_focus_needs:
        assert need.focus_id is not None
        focus_by_id[need.focus_id] = MorningBriefingLearningFocusFact(
            focus_id=need.focus_id,
            course_code=need.course_code,
            topic=need.topic,
            verified_context_label=(
                f"Richard explicitly identified {need.topic} as an academic struggle."
            ),
        )
    performance = tuple(
        MorningBriefingPerformanceFact(
            signal_id=signal.signal_id,
            course_code=signal.course_code,
            assessment_id=signal.assessment_id,
            outcome=signal.outcome,
            observed_at=signal.observed_at.astimezone(TORONTO).isoformat(timespec="minutes"),
            verified_summary=signal.verified_summary,
        )
        for signal in sorted(
            facts.performance_signals,
            key=lambda item: (item.observed_at, item.signal_id),
            reverse=True,
        )[:20]
    )
    scheduled_assessment_ids = {
        item.assessment_id for item in block_facts if item.block_kind == "assessment"
    }
    insight_ids: set[str] = set()
    insight_facts: list[MorningBriefingMaterialInsightFact] = []
    for insight in material_insights:
        if insight.insight_id in insight_ids:
            continue
        if insight.assessment_id not in scheduled_assessment_ids:
            continue
        insight_ids.add(insight.insight_id)
        insight_facts.append(
            MorningBriefingMaterialInsightFact(
                insight_id=insight.insight_id,
                assessment_id=insight.assessment_id,
                text=insight.text,
                evidence_chunk_ids=insight.evidence_chunk_ids,
            )
        )
        if len(insight_facts) == 30:
            break
    breakdown_ids: set[str] = set()
    breakdown_facts: list[MorningBriefingWorkBreakdownFact] = []
    for breakdown in breakdowns:
        if breakdown.assessment_id in breakdown_ids:
            continue
        if breakdown.assessment_id not in scheduled_assessment_ids:
            continue
        breakdown_ids.add(breakdown.assessment_id)
        breakdown_facts.append(
            MorningBriefingWorkBreakdownFact(
                assessment_id=breakdown.assessment_id,
                steps=tuple(breakdown.steps[:8]),
                rationale=breakdown.rationale[:700],
            )
        )
        if len(breakdown_facts) == 30:
            break
    return MorningBriefingContext(
        current_local_date=local_date.isoformat(),
        assessments=assessment_facts,
        scheduled_blocks=tuple(block_facts),
        learning_focuses=tuple(focus_by_id.values()),
        performance_signals=performance,
        material_insights=tuple(insight_facts),
        work_breakdowns=tuple(breakdown_facts),
        deferred_assessment_ids=tuple(
            item for item in plan.deferred_assessment_ids if item in allowed_assessment_ids
        )[:50],
        deferred_practice_focus_ids=tuple(
            item for item in plan.deferred_practice_focus_ids if item in focus_by_id
        )[:20],
    )


def _duration_minutes(message: str) -> tuple[int, ...]:
    values: list[int] = []
    for match in _NUMERIC_DURATION_PATTERN.finditer(message):
        amount = int(match.group(1))
        unit = match.group(2).lower()
        values.append(amount * 60 if unit.startswith(("hour", "hr")) else amount)
    word_hours = {
        "a": 60,
        "an": 60,
        "one": 60,
        "two": 120,
        "three": 180,
        "four": 240,
        "half": 30,
    }
    values.extend(
        word_hours[match.group(1).lower()] for match in _WORD_HOUR_PATTERN.finditer(message)
    )
    return tuple(values)


def _duration_sums(values: Sequence[int]) -> set[int]:
    sums = {0}
    for value in values:
        sums.update({existing + value for existing in tuple(sums)})
    sums.discard(0)
    return sums


def _normalized_course_code(value: str) -> str:
    return re.sub(r"[- ]", "", value).casefold()


def _clock_minutes(value: str) -> int:
    normalized = value.casefold().replace(".", "").replace(" ", "")
    suffix = normalized[-2:] if normalized.endswith(("am", "pm")) else None
    if suffix is not None:
        normalized = normalized[:-2]
    hour_text, separator, minute_text = normalized.partition(":")
    hour = int(hour_text)
    minute = int(minute_text) if separator else 0
    if suffix == "am":
        hour %= 12
    elif suffix == "pm":
        hour = hour % 12 + 12
    return hour * 60 + minute


def validate_morning_briefing(
    briefing: MorningBriefing,
    context: MorningBriefingContext,
) -> MorningBriefing:
    """Reject model prose that is not grounded in the supplied briefing facts."""

    assessments = {item.assessment_id: item for item in context.assessments}
    blocks = {item.block_id: item for item in context.scheduled_blocks}
    focuses = {item.focus_id: item for item in context.learning_focuses}
    signals = {item.signal_id: item for item in context.performance_signals}
    insights = {item.insight_id: item for item in context.material_insights}
    reference_groups = (
        (briefing.referenced_assessment_ids, assessments, "assessment"),
        (briefing.referenced_block_ids, blocks, "block"),
        (briefing.referenced_focus_ids, focuses, "focus"),
        (briefing.referenced_performance_signal_ids, signals, "performance signal"),
        (briefing.referenced_insight_ids, insights, "material insight"),
    )
    for referenced, supplied, label in reference_groups:
        unknown = sorted(set(referenced) - set(supplied))
        if unknown:
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                f"Morning briefing referenced an unknown {label}",
            )

    message = briefing.message_text
    if not message.startswith("Good morning, Richard."):
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing did not use the required greeting",
        )
    if _NON_PRODUCTION_BRIEFING_PATTERN.search(message):
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing contained a non-production marker",
        )

    current_date = date.fromisoformat(context.current_local_date)
    allowed_dates = {_exact_date_label(current_date).casefold()}
    allowed_dates.update(item.exact_date_label.casefold() for item in context.assessments)
    mentioned_dates = {match.group(0).casefold() for match in _DATE_PATTERN.finditer(message)}
    if mentioned_dates - allowed_dates:
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing changed or invented a calendar date",
        )
    allowed_iso_dates = {context.current_local_date}
    allowed_iso_dates.update(
        datetime.fromisoformat(item.due_at_local).date().isoformat() for item in context.assessments
    )
    mentioned_iso_dates = {match.group(0) for match in _ISO_DATE_PATTERN.finditer(message)}
    if mentioned_iso_dates - allowed_iso_dates:
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing changed or invented an ISO calendar date",
        )
    allowed_courses = {_normalized_course_code(item.course_code) for item in context.assessments}
    allowed_courses.update(
        _normalized_course_code(item.course_code)
        for item in context.learning_focuses
        if item.course_code is not None
    )
    allowed_courses.update(
        _normalized_course_code(item.course_code) for item in context.performance_signals
    )
    mentioned_courses = {
        _normalized_course_code(match.group(0)) for match in _COURSE_CODE_PATTERN.finditer(message)
    }
    if mentioned_courses - allowed_courses:
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing invented a course code",
        )
    for assessment_id in briefing.referenced_assessment_ids:
        assessment = assessments[assessment_id]
        lowered = message.casefold()
        if assessment.exact_date_label.casefold() not in lowered:
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                "Morning briefing omitted the exact date for a referenced assessment",
            )
        if assessment.relative_date_label.casefold() not in lowered:
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                "Morning briefing omitted the relative date for a referenced assessment",
            )
        if not any(
            label.casefold() in lowered for label in (assessment.title, assessment.course_code)
        ):
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                "Morning briefing did not identify a referenced assessment",
            )
    if _ASSESSMENT_TERM_PATTERN.search(message) and not briefing.referenced_assessment_ids:
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing assessment claim lacked a referenced assessment",
        )

    referenced_blocks = [blocks[item] for item in briefing.referenced_block_ids]
    allowed_clock_minutes = {
        datetime.fromisoformat(item.start_at_local).hour * 60
        + datetime.fromisoformat(item.start_at_local).minute
        for item in referenced_blocks
    }
    allowed_clock_minutes.update(
        datetime.fromisoformat(assessments[item].due_at_local).hour * 60
        + datetime.fromisoformat(assessments[item].due_at_local).minute
        for item in briefing.referenced_assessment_ids
    )
    mentioned_clock_minutes = {
        _clock_minutes(match.group(0)) for match in _TIME_PATTERN.finditer(message)
    }
    if mentioned_clock_minutes - allowed_clock_minutes:
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing changed or invented a scheduled time",
        )
    durations = _duration_minutes(message)
    if referenced_blocks and not durations:
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing omitted scheduled study durations",
        )
    if durations and not referenced_blocks:
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing included a duration without a scheduled block",
        )
    allowed_duration_sums = _duration_sums([item.duration_minutes for item in referenced_blocks])
    if any(value not in allowed_duration_sums for value in durations):
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing changed or invented a scheduled duration",
        )
    if _RECOMMENDATION_PATTERN.search(message) and not referenced_blocks:
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing recommendation did not reference a scheduled block",
        )
    referenced_assessments = set(briefing.referenced_assessment_ids)
    referenced_focuses = set(briefing.referenced_focus_ids)
    referenced_block_assessments = {
        block.assessment_id for block in referenced_blocks if block.block_kind == "assessment"
    }
    for block in referenced_blocks:
        if block.block_kind == "assessment" and block.assessment_id not in referenced_assessments:
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                "Morning briefing block was not tied to its assessment",
            )
        if block.block_kind == "practice" and block.learning_focus_id not in referenced_focuses:
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                "Morning briefing practice block was not tied to its learning focus",
            )
    referenced_insights = [insights[item] for item in briefing.referenced_insight_ids]
    for insight in referenced_insights:
        if insight.assessment_id not in referenced_assessments:
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                "Morning briefing insight was not tied to its assessment",
            )
        if insight.assessment_id not in referenced_block_assessments:
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                "Morning briefing material insight lacked a scheduled assessment block",
            )
    mentioned_percentages = {
        match.group(0).replace(" ", "").casefold() for match in _PERCENT_PATTERN.finditer(message)
    }
    if mentioned_percentages:
        allowed_percentages = {
            match.group(0).replace(" ", "").casefold()
            for insight in referenced_insights
            for match in _PERCENT_PATTERN.finditer(insight.text)
        }
        if mentioned_percentages - allowed_percentages:
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                "Morning briefing material percentage lacked verified insight evidence",
            )

    positive_signal_ids = {
        item.signal_id
        for item in context.performance_signals
        if item.outcome in {"positive", "improved"}
    }
    if _POSITIVE_PERFORMANCE_PATTERN.search(message) and not positive_signal_ids.intersection(
        briefing.referenced_performance_signal_ids
    ):
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing performance claim lacked a verified signal",
        )
    negative_signal_ids = {
        item.signal_id for item in context.performance_signals if item.outcome == "needs_attention"
    }
    if _STRUGGLE_PATTERN.search(message) and not (
        briefing.referenced_focus_ids
        or negative_signal_ids.intersection(briefing.referenced_performance_signal_ids)
    ):
        raise transient_error(
            ErrorCode.ANALYSIS_INVALID_OUTPUT,
            "Morning briefing struggle claim lacked verified evidence",
        )
    for focus_id in briefing.referenced_focus_ids:
        focus = focuses[focus_id]
        if not any(
            label and label.casefold() in message.casefold()
            for label in (focus.topic, focus.course_code)
        ):
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                "Morning briefing did not identify a referenced learning focus",
            )
    for signal_id in briefing.referenced_performance_signal_ids:
        if signals[signal_id].course_code.casefold() not in message.casefold():
            raise transient_error(
                ErrorCode.ANALYSIS_INVALID_OUTPUT,
                "Morning briefing did not identify a referenced performance signal",
            )
    return briefing


async def run_morning_plan(
    *,
    store: AcademicPlannerStore,
    syncer: AcademicSynchronizer | None = None,
    delivery: PlannerDelivery | None = None,
    model: PlannerModelGateway | None = None,
    material_reasoner: AcademicMaterialReasoner | None = None,
    semantic_validator: MorningBriefingSemanticValidator | None = None,
    now: datetime | None = None,
    horizon_days: int = 7,
) -> dict[str, object]:
    """Create, persist, and optionally deliver the morning deterministic plan."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not 7 <= horizon_days <= 14:
        raise ValueError("horizon_days must be between 7 and 14")
    sync_result = await syncer.sync(now=current) if syncer is not None else None
    sync_status = getattr(sync_result, "status", None)
    if sync_result is not None and sync_status == "setup_required":
        summary = (
            sync_result.as_dict()
            if callable(getattr(sync_result, "as_dict", None))
            else {"status": "setup_required"}
        )
        return {
            "status": "setup_required",
            "sync": summary,
            "block_count": 0,
            "deferred_count": 0,
            "deferred_practice_count": 0,
            "ambiguous_count": 0,
            "delivery_count": 0,
        }
    facts = store.load_planner_facts(now=current, horizon_days=horizon_days)
    plan = build_daily_plan(facts, now=current)
    breakdowns_by_assessment: dict[str, WorkBreakdown] = {}
    if model is not None:
        # Breakdowns are advisory context only; allocation remains entirely
        # deterministic and cannot be changed by model output.
        for assessment in facts.assessments:
            if not assessment.completed and not assessment.ambiguous:
                breakdown = WorkBreakdown.model_validate(await model.breakdown(assessment))
                if breakdown.assessment_id == assessment.id:
                    breakdowns_by_assessment[assessment.id] = breakdown
        critique = await model.critique(plan)
    else:
        critique = None
    if critique is not None:
        plan = plan.model_copy(update={"critique": critique})
    store.save_daily_plan(plan)
    briefing = None
    material_insights: list[GroundedAssessmentInsight] = []
    blocks_by_assessment: dict[str, list[Any]] = {}
    for block in plan.blocks:
        if block.block_kind == "assessment":
            blocks_by_assessment.setdefault(block.assessment_id, []).append(block)
    if material_reasoner is not None:
        assessments_by_id = {
            assessment.id: assessment
            for assessment in facts.assessments
            if not assessment.completed and not assessment.ambiguous
        }
        for assessment_id, scheduled_blocks in blocks_by_assessment.items():
            assessment = assessments_by_id.get(assessment_id)
            if assessment is None:
                continue
            generated = await material_reasoner.generate_insights(
                assessment,
                tuple(scheduled_blocks),
                breakdown=breakdowns_by_assessment.get(assessment_id),
            )
            for insight in generated:
                if insight.assessment_id == assessment_id:
                    material_insights.append(insight)
                if len(material_insights) == 30:
                    break
            if len(material_insights) == 30:
                break
    if model is not None:
        context = build_morning_briefing_context(
            facts,
            plan,
            now=current,
            material_insights=tuple(material_insights),
            breakdowns=tuple(breakdowns_by_assessment.values()),
        )
        briefing = validate_morning_briefing(await model.morning_briefing(context), context)
        if context.material_insights and semantic_validator is not None:
            valid = await semantic_validator.validate_morning_briefing(briefing, context)
            if not valid:
                raise transient_error(
                    ErrorCode.ANALYSIS_INVALID_OUTPUT,
                    "Morning briefing failed semantic material grounding",
                )
    delivery_count = 0
    if delivery is not None:
        if briefing is None:
            raise transient_error(
                ErrorCode.MODEL_TRANSIENT,
                "Morning briefing model is unavailable for configured delivery",
            )
        await delivery.send_morning_plan(
            briefing,
            idempotency_key=f"academic-plan:{plan.plan_id}:v2",
        )
        delivery_count = 1
        for fact in plan.ambiguous_questions:
            await delivery.send_ambiguity_question(
                fact,
                idempotency_key=f"academic-ambiguity:{fact.id}:v1",
            )
    result: dict[str, object] = {
        "status": "succeeded",
        "plan_id": str(plan.plan_id),
        "block_count": len(plan.blocks),
        "deferred_count": len(plan.deferred_assessment_ids),
        "deferred_practice_count": len(plan.deferred_practice_focus_ids),
        "ambiguous_count": len(plan.ambiguous_questions),
        "delivery_count": delivery_count,
        "material_insight_count": len(material_insights),
    }
    if sync_status is not None:
        result["sync_status"] = sync_status
    return result


def _fallback_extract(reply: str) -> tuple[ProposedChange, ...]:
    """Conservative fallback parser; it never infers completion from silence."""

    changes: list[ProposedChange] = []
    for line in reply.splitlines():
        match = re.fullmatch(r"\s*completed\s+([A-Za-z0-9_.:-]+)\s*", line, re.IGNORECASE)
        if match:
            changes.append(ProposedChange(field="completed", value="true", assessment_id=match[1]))
            continue
        match = re.fullmatch(
            r"\s*(?:logged|actual)\s+([A-Za-z0-9_.:-]+)\s+(\d+)\s*(?:m|min|minutes)?\s*",
            line,
            re.IGNORECASE,
        )
        if match:
            changes.append(
                ProposedChange(field="actual_minutes", value=match[2], assessment_id=match[1])
            )
    return tuple(changes)


def extract_checkin_changes(reply: str) -> tuple[ProposedChange, ...]:
    """Extract only the local, conservative grammar used for private replies."""

    return _fallback_extract(reply)


async def create_checkin_proposal(
    *,
    store: AcademicPlannerStore,
    reply: str,
    plan_id: uuid.UUID | None = None,
    model: PlannerModelGateway | None = None,
    delivery: PlannerDelivery | None = None,
    now: datetime | None = None,
) -> CheckinProposal:
    """Turn a reply into a proposal without performing any Notion write."""

    if not reply.strip():
        raise ValueError("check-in reply must not be empty")
    changes = (
        tuple(await model.extract_checkin(reply)) if model is not None else _fallback_extract(reply)
    )
    proposal_id = uuid.uuid4()
    confirmation = f"confirm {proposal_id}"
    current = now or datetime.now(UTC)
    ttl_hours = int(getattr(store, "confirmation_ttl_hours", 24))
    proposal = CheckinProposal(
        proposal_id=proposal_id,
        confirmation_event=confirmation,
        changes=changes,
        source_plan_id=plan_id,
        expires_at=current + timedelta(hours=ttl_hours),
        question=(
            "No confirmed changes were extracted; tell me what to update." if not changes else None
        ),
    )
    store.save_checkin_proposal(proposal)
    if delivery is not None:
        await delivery.send_confirmation(
            proposal,
            idempotency_key=f"academic-proposal:{proposal_id}:v1",
        )
    return proposal


async def run_end_of_day_checkin(
    *,
    plan: DailyPlan | None,
    delivery: PlannerDelivery,
    idempotency_key: str,
    store: AcademicPlannerStore | None = None,
    now: datetime | None = None,
    end_of_day_time: time = time(21, 0),
    timezone: str = "America/Toronto",
    snooze_after_missed: int = 2,
    delete_after_reminders: int = 5,
) -> dict[str, object]:
    """Send the daily reflection and advance due learning-focus reminders."""

    await delivery.send_checkin(plan=plan, idempotency_key=idempotency_key)
    focus_updates: tuple[dict[str, Any], ...] = ()
    prepare = getattr(store, "prepare_learning_focus_checkin", None)
    send_focus_reviews = getattr(delivery, "send_focus_reviews", None)
    if callable(prepare):
        current = now or datetime.now(UTC)
        zone = ZoneInfo(timezone)
        local = current.astimezone(zone)
        next_local = datetime.combine(
            local.date() + timedelta(days=1),
            end_of_day_time,
            tzinfo=zone,
        )
        focus_updates = tuple(
            cast(Any, prepare)(
                now=current,
                next_review_at=next_local.astimezone(UTC),
                idempotency_key=idempotency_key,
                snooze_after_missed=snooze_after_missed,
                delete_after_reminders=delete_after_reminders,
            )
        )
        if focus_updates and callable(send_focus_reviews):
            await cast(Any, send_focus_reviews)(
                focus_updates,
                idempotency_key=f"{idempotency_key}:learning-focus",
            )
    return {
        "status": "sent",
        "plan_id": str(plan.plan_id) if plan is not None else None,
        "focus_review_count": sum(item.get("kind") != "deleted" for item in focus_updates),
        "focus_deleted_count": sum(item.get("kind") == "deleted" for item in focus_updates),
    }


async def confirm_checkin_proposal(
    *,
    store: AcademicPlannerStore,
    writer: NotionAcademicWriter,
    proposal_id: uuid.UUID,
    confirmation_event: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Apply changes only when the confirmation event matches byte-for-byte."""

    if confirmation_event != f"confirm {proposal_id}":
        return {"status": "confirmation_required", "proposal_id": str(proposal_id)}

    if now is None:
        status, proposal = store.prepare_checkin_application(proposal_id, confirmation_event)
    else:
        status, proposal = store.prepare_checkin_application(
            proposal_id, confirmation_event, now=now
        )
    if status == "not_found" or proposal is None:
        return {"status": "not_found", "proposal_id": str(proposal_id)}
    if status in {"confirmation_required", "expired"}:
        return {"status": "confirmation_required", "proposal_id": str(proposal_id)}
    if status == "already_applied":
        return {
            "status": "applied",
            "proposal_id": str(proposal_id),
            "change_count": len(proposal.changes),
        }
    if status == "in_progress":
        return {"status": "confirmation_required", "proposal_id": str(proposal_id)}
    if status != "ready":
        raise RuntimeError("academic proposal entered an unknown confirmation state")
    await writer.apply_confirmed_changes(
        proposal.changes,
        proposal_id=proposal_id,
        confirmation_event=confirmation_event,
    )
    store.mark_checkin_applied(proposal_id, confirmation_event)
    return {
        "status": "applied",
        "proposal_id": str(proposal_id),
        "change_count": len(proposal.changes),
    }


def reject_checkin_proposal(
    *,
    store: AcademicPlannerStore,
    proposal_id: uuid.UUID,
    rejection_event: str,
    now: datetime | None = None,
) -> dict[str, object]:
    """Terminally reject exactly one pending proposal without an external write."""

    if rejection_event != f"reject {proposal_id}":
        return {"status": "rejection_required", "proposal_id": str(proposal_id)}
    if now is None:
        status, proposal = store.reject_checkin_proposal(
            proposal_id,
            actor="discord_authorized_user",
        )
    else:
        status, proposal = store.reject_checkin_proposal(
            proposal_id,
            actor="discord_authorized_user",
            now=now,
        )
    if proposal is None:
        status = "not_found"
    elif status not in {"rejected", "already_rejected"}:
        status = "rejection_required"
    result: dict[str, object] = {
        "status": status,
        "proposal_id": str(proposal_id),
    }
    if proposal is not None:
        result["change_count"] = len(proposal.changes)
    return result


async def run_academic_planner(run_id: str, idempotency_key: str) -> dict[str, object]:
    """Worker entry point; host runtime injection is intentionally explicit."""

    runtime = _runtime or _load_default_runtime(uuid.UUID(run_id))
    if "end-of-day" in idempotency_key or ":eod:" in idempotency_key:
        plan = runtime.store.get_latest_daily_plan()
        if runtime.delivery is None:
            raise RuntimeError("academic Discord delivery is not configured")
        result = await run_end_of_day_checkin(
            plan=plan,
            delivery=runtime.delivery,
            idempotency_key=idempotency_key,
            store=runtime.store,
            end_of_day_time=runtime.end_of_day_time,
            timezone=runtime.timezone,
            snooze_after_missed=runtime.snooze_after_missed,
            delete_after_reminders=runtime.delete_after_reminders,
        )
    else:
        result = await run_morning_plan(
            store=runtime.store,
            syncer=runtime.syncer,
            delivery=runtime.delivery,
            model=runtime.model,
            material_reasoner=runtime.material_reasoner,
            semantic_validator=runtime.semantic_validator,
            horizon_days=runtime.horizon_days,
        )
    result.update({"run_id": run_id, "idempotency_key": idempotency_key})
    return result


class _Runtime:
    def __init__(
        self,
        store: AcademicPlannerStore,
        delivery: PlannerDelivery | None,
        model: PlannerModelGateway | None,
        syncer: AcademicSynchronizer | None,
        material_reasoner: AcademicMaterialReasoner | None,
        semantic_validator: MorningBriefingSemanticValidator | None,
        horizon_days: int,
        end_of_day_time: time = time(21, 0),
        timezone: str = "America/Toronto",
        snooze_after_missed: int = 2,
        delete_after_reminders: int = 5,
    ) -> None:
        self.store = store
        self.delivery = delivery
        self.model = model
        self.syncer = syncer
        self.material_reasoner = material_reasoner
        self.semantic_validator = semantic_validator
        self.horizon_days = horizon_days
        self.end_of_day_time = end_of_day_time
        self.timezone = timezone
        self.snooze_after_missed = snooze_after_missed
        self.delete_after_reminders = delete_after_reminders


_runtime: _Runtime | None = None


def _load_default_runtime(run_id: uuid.UUID) -> _Runtime:
    """Load the host-provided SQL runtime lazily at worker execution time."""

    from app.core.config import get_settings
    from app.db.session import Database

    settings = get_settings()
    engine = Database(settings).engine
    try:
        from app.db import academic
    except ImportError:
        raise RuntimeError("academic SQL persistence integration is unavailable") from None
    store_factory = getattr(academic, "SQLAlchemyAcademicPlannerStore", None)
    if not callable(store_factory):
        raise RuntimeError("academic SQL persistence integration is unavailable")
    from app.llm.embeddings import AcademicEmbeddingGateway
    from app.llm.gateway import LLMGateway

    gateway = LLMGateway(settings)
    embedding_gateway = AcademicEmbeddingGateway(settings)
    model = LLMPlannerModel(gateway)
    delivery = None
    adapter = None
    channel_id = getattr(settings, "discord_academic_channel_id", None)
    if channel_id is None:
        channels = getattr(settings, "discord_target_channels", ())
        channel_id = channels[0] if channels else None
    token = getattr(settings, "discord_bot_token", None)
    if token is not None and channel_id is not None:
        from app.connectors.discord import (
            DiscordAcademicPlannerAdapter,
            DiscordAcademicPlannerDelivery,
        )

        adapter = DiscordAcademicPlannerAdapter(
            token=token,
            allowed_channel_ids={channel_id},
        )
        delivery = DiscordAcademicPlannerDelivery(
            engine=engine,
            run_id=run_id,
            channel_id=channel_id,
            adapter=adapter,
        )
    store = cast(Any, store_factory)(
        engine,
        confirmation_ttl_hours=settings.academic_confirmation_ttl_hours,
        embedding_gateway=embedding_gateway,
        default_practice_minutes=settings.academic_memory_default_practice_minutes,
    )
    from app.agents.academic_planner.material_reasoning import (
        AssessmentMaterialMorningValidator,
        AssessmentMaterialReasonerService,
    )

    material_reasoner = AssessmentMaterialReasonerService(
        model=gateway,
        store=store,
        max_turns=settings.academic_material_agent_max_turns,
        retrieval_limit=settings.academic_material_retrieval_limit,
        max_prompt_chars=settings.academic_material_prompt_max_chars,
    )
    semantic_validator = AssessmentMaterialMorningValidator(
        gateway,
        max_prompt_chars=settings.academic_material_prompt_max_chars,
    )
    from app.agents.academic_planner.sync import AcademicNotionSync
    from app.connectors.notion import NotionConnector
    from app.core.errors import LifeAgentError

    connector = None
    notion_setup_condition = "notion_configuration_missing"
    if settings.notion_token is not None and settings.notion_courses_database_id is not None:
        try:
            connector = NotionConnector(
                token=settings.notion_token,
                courses_database_id=settings.notion_courses_database_id,
                timeout_seconds=settings.connector_timeout_seconds,
            )
        except (LifeAgentError, ValueError):
            notion_setup_condition = "notion_configuration_invalid"

    async def enqueue_material(page_id: str, fingerprint: str) -> object:
        from app.queue.tasks import defer_academic_material_ingestion

        return await defer_academic_material_ingestion(page_id, fingerprint)

    syncer = AcademicNotionSync(
        connector=connector,
        store=store,
        discord=adapter,
        discord_channel_id=channel_id,
        timezone=settings.app_timezone,
        clarification_ttl_hours=settings.academic_confirmation_ttl_hours,
        setup_condition_code=notion_setup_condition,
        material_enqueuer=enqueue_material if connector is not None else None,
    )
    return _Runtime(
        store,
        delivery,
        model,
        syncer,
        material_reasoner,
        semantic_validator,
        settings.academic_plan_horizon_days,
        settings.academic_end_of_day_schedule,
        settings.app_timezone,
        settings.academic_memory_snooze_after_missed_checkins,
        settings.academic_memory_delete_after_missed_checkins,
    )


def configure_academic_runtime(
    store: AcademicPlannerStore,
    *,
    delivery: PlannerDelivery | None = None,
    model: PlannerModelGateway | None = None,
    syncer: AcademicSynchronizer | None = None,
    material_reasoner: AcademicMaterialReasoner | None = None,
    semantic_validator: MorningBriefingSemanticValidator | None = None,
    horizon_days: int = 7,
    end_of_day_time: time = time(21, 0),
    timezone: str = "America/Toronto",
    snooze_after_missed: int = 2,
    delete_after_reminders: int = 5,
) -> None:
    """Inject host integrations for the worker process."""

    if not 7 <= horizon_days <= 14:
        raise ValueError("horizon_days must be between 7 and 14")
    global _runtime
    _runtime = _Runtime(
        store,
        delivery,
        model,
        syncer,
        material_reasoner,
        semantic_validator,
        horizon_days,
        end_of_day_time,
        timezone,
        snooze_after_missed,
        delete_after_reminders,
    )


__all__ = [
    "AcademicPlannerStore",
    "AcademicSynchronizer",
    "LLMPlannerModel",
    "NotionAcademicWriter",
    "PlannerDelivery",
    "PlannerModelGateway",
    "build_daily_plan",
    "build_morning_briefing_context",
    "configure_academic_runtime",
    "confirm_checkin_proposal",
    "create_checkin_proposal",
    "extract_checkin_changes",
    "reject_checkin_proposal",
    "relative_date_label",
    "run_academic_planner",
    "run_end_of_day_checkin",
    "run_morning_plan",
    "validate_morning_briefing",
]
