"""Grounded, versioned interview preparation plan orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.job_interviews.contracts import PreparationPlanSnapshot


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class VerifiedRequirement(_StrictModel):
    fact: str = Field(min_length=1, max_length=500)
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=8)


class PreparationPlanContent(_StrictModel):
    interview_id: str = Field(min_length=1, max_length=255)
    application_row_id: str = Field(min_length=1, max_length=255)
    role_and_company_summary: str = Field(min_length=1, max_length=1_000)
    interview_stage: str | None = Field(default=None, max_length=255)
    verified_requirements: tuple[VerifiedRequirement, ...] = Field(default=(), max_length=30)
    preparation_topics: tuple[str, ...] = Field(min_length=1, max_length=30)
    behavioral_prompts: tuple[str, ...] = Field(default=(), max_length=20)
    questions_to_ask: tuple[str, ...] = Field(default=(), max_length=20)
    daily_actions: tuple[str, ...] = Field(min_length=1, max_length=30)
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=50)
    uncertainties: tuple[str, ...] = Field(default=(), max_length=20)
    research_fingerprint: str | None = Field(default=None, max_length=128)


class PreparationPlanDecision(_StrictModel):
    outcome: Literal["ready", "needs_clarification"]
    plan: PreparationPlanContent | None = None
    clarification_question: str | None = Field(default=None, max_length=1_000)
    material_change_reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def outcome_has_exact_payload(self) -> PreparationPlanDecision:
        if self.outcome == "ready" and self.plan is None:
            raise ValueError("ready preparation outcome requires a plan")
        if self.outcome == "needs_clarification" and not self.clarification_question:
            raise ValueError("clarification outcome requires a focused question")
        if self.outcome == "needs_clarification" and self.plan is not None:
            raise ValueError("clarification outcome cannot smuggle an ungrounded plan")
        return self


class PreparationOutcome(_StrictModel):
    status: Literal["created", "updated", "unchanged", "needs_clarification"]
    plan: PreparationPlanSnapshot | None = None
    clarification_question: str | None = None
    ask_to_save_to_notion: bool = False


class StructuredGateway(Protocol):
    async def invoke_structured(self, *, prompt: str, response_model: type[Any]) -> Any: ...


class PreparationPlanStore(Protocol):
    def get_current_plan(self, interview_page_id: str) -> PreparationPlanSnapshot | None: ...

    def save_preparation_plan(self, plan: PreparationPlanSnapshot) -> Any: ...


def _canonical_plan_hash(plan: PreparationPlanContent) -> str:
    payload = json.dumps(
        plan.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_plan_decision(
    decision: PreparationPlanDecision,
    *,
    interview_id: str,
    application_row_id: str,
    allowed_source_ids: set[str],
) -> PreparationPlanContent | None:
    """Reject identity changes and facts that escape the supplied evidence boundary."""

    if decision.outcome == "needs_clarification":
        return None
    plan = decision.plan
    if plan is None:
        raise ValueError("Qwen returned no preparation plan")
    if plan.interview_id != interview_id or plan.application_row_id != application_row_id:
        raise ValueError("preparation plan changed a stable source identity")
    if not set(plan.source_ids).issubset(allowed_source_ids):
        raise ValueError("preparation plan referenced an unknown source")
    for requirement in plan.verified_requirements:
        if not set(requirement.source_ids).issubset(allowed_source_ids):
            raise ValueError("verified requirement referenced an unknown source")
        if not set(requirement.source_ids).issubset(set(plan.source_ids)):
            raise ValueError("verified requirement source is absent from plan provenance")
    return plan


def snapshot_from_plan(
    plan: PreparationPlanContent,
    *,
    previous: PreparationPlanSnapshot | None,
    generated_at: datetime,
    material_change_reason: str | None,
) -> tuple[PreparationPlanSnapshot, bool]:
    """Version material changes while preserving one stable current plan identity."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    plan_hash = _canonical_plan_hash(plan)
    changed = previous is None or previous.plan_hash != plan_hash
    revision = 1 if previous is None else previous.revision + int(changed)
    snapshot = PreparationPlanSnapshot(
        interview_page_id=plan.interview_id,
        revision=revision,
        generated_at=generated_at.astimezone(UTC),
        plan_hash=plan_hash,
        summary=plan.role_and_company_summary,
        next_actions=plan.daily_actions[:3],
        evidence=plan.source_ids,
        research_snapshot_id=plan.research_fingerprint,
        material_change_reason=material_change_reason if changed else None,
        plan=plan.model_dump(mode="json"),
    )
    return snapshot, changed


async def maintain_preparation_plan(
    gateway: StructuredGateway,
    *,
    store: PreparationPlanStore,
    interview_id: str,
    interview_title: str,
    days_until: int,
    application_row_id: str,
    application_facts: Mapping[str, object],
    research_excerpts: Sequence[Mapping[str, object]],
    source_ids: Sequence[str],
    research_fingerprint: str | None = None,
    user_clarifications: Sequence[str] = (),
    now: datetime | None = None,
) -> PreparationOutcome:
    """Create or revise the single maintained plan using only bounded evidence."""

    existing = store.get_current_plan(interview_id)
    payload = {
        "interview": {
            "id": interview_id,
            "title": interview_title,
            "days_until": days_until,
        },
        "application": {"row_id": application_row_id, "facts": dict(application_facts)},
        "research_excerpts": list(research_excerpts)[:12],
        "source_ids": list(source_ids)[:50],
        "research_fingerprint": research_fingerprint,
        "user_clarifications": list(user_clarifications)[:10],
        "existing_plan": existing.plan if existing is not None else None,
    }
    prompt = (
        "Maintain one evolving interview preparation plan. Use only supplied source facts and "
        "clarifications. Preserve useful existing work. Verified requirements need source IDs; "
        "preparation suggestions must not be stated as company facts. Do not invent interview "
        "format, technology, or evaluation criteria. If the evidence is insufficient for "
        "tailored advice, ask one focused question instead of returning generic advice. "
        "daily_actions must be ordered with today's grounded next action first.\n"
        + json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    )
    result = await gateway.invoke_structured(prompt=prompt, response_model=PreparationPlanDecision)
    decision = getattr(result, "output", None)
    if not isinstance(decision, PreparationPlanDecision):
        raise ValueError("Qwen returned no valid structured preparation result")
    validated = validate_plan_decision(
        decision,
        interview_id=interview_id,
        application_row_id=application_row_id,
        allowed_source_ids=set(source_ids),
    )
    if validated is None:
        return PreparationOutcome(
            status="needs_clarification",
            clarification_question=decision.clarification_question,
        )
    timestamp = now or datetime.now(UTC)
    snapshot, changed = snapshot_from_plan(
        validated,
        previous=existing,
        generated_at=timestamp,
        material_change_reason=decision.material_change_reason,
    )
    if not changed:
        return PreparationOutcome(status="unchanged", plan=existing)
    store.save_preparation_plan(snapshot)
    return PreparationOutcome(
        status="created" if existing is None else "updated",
        plan=snapshot,
        ask_to_save_to_notion=True,
    )


__all__ = [
    "PreparationOutcome",
    "PreparationPlanContent",
    "PreparationPlanDecision",
    "VerifiedRequirement",
    "maintain_preparation_plan",
    "snapshot_from_plan",
    "validate_plan_decision",
]
