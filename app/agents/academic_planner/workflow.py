"""Academic planning orchestration with deterministic state-changing boundaries."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from app.agents.academic_planner.allocator import allocate_plan_with_deferred
from app.agents.academic_planner.contracts import (
    AmbiguousFact,
    CheckinProposal,
    DailyPlan,
    PlanCritique,
    PlannerFacts,
    ProposedChange,
    WorkBreakdown,
)

TORONTO = ZoneInfo("America/Toronto")


class AcademicPlannerStore(Protocol):
    """Persistence seam for facts, plans, and confirmation proposals."""

    def load_planner_facts(self, *, now: datetime, horizon_days: int) -> PlannerFacts: ...

    def save_daily_plan(self, plan: DailyPlan) -> None: ...

    def get_latest_daily_plan(self) -> DailyPlan | None: ...

    def save_checkin_proposal(self, proposal: CheckinProposal) -> None: ...

    def get_checkin_proposal(self, proposal_id: uuid.UUID) -> CheckinProposal | None: ...

    def prepare_checkin_application(
        self, proposal_id: uuid.UUID, confirmation_event: str
    ) -> tuple[str, CheckinProposal | None]: ...

    def mark_checkin_applied(
        self, proposal_id: uuid.UUID, confirmation_event: str | None = None
    ) -> None: ...

    def reject_checkin_proposal(
        self, proposal_id: uuid.UUID, *, actor: str = "academic_planner"
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

    async def extract_checkin(self, reply: str) -> Sequence[ProposedChange]: ...


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

    async def extract_checkin(self, reply: str) -> Sequence[ProposedChange]:
        # Check-in replies may originate in a private Discord channel. Keep
        # their content out of model prompts and accept only the conservative,
        # explicit local grammar below.
        return _fallback_extract(reply)


class PlannerDelivery(Protocol):
    async def send_morning_plan(self, plan: DailyPlan, *, idempotency_key: str) -> object: ...

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


async def run_morning_plan(
    *,
    store: AcademicPlannerStore,
    syncer: AcademicSynchronizer | None = None,
    delivery: PlannerDelivery | None = None,
    model: PlannerModelGateway | None = None,
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
    if model is not None:
        # Breakdowns are advisory context only; allocation remains entirely
        # deterministic and cannot be changed by model output.
        for assessment in facts.assessments:
            if not assessment.completed and not assessment.ambiguous:
                await model.breakdown(assessment)
        critique = await model.critique(plan)
    else:
        critique = None
    if critique is not None:
        plan = plan.model_copy(update={"critique": critique})
    store.save_daily_plan(plan)
    delivery_count = 0
    if delivery is not None:
        await delivery.send_morning_plan(plan, idempotency_key=f"academic-plan:{plan.plan_id}:v1")
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
) -> dict[str, object]:
    """Apply changes only when the confirmation event matches byte-for-byte."""

    if confirmation_event != f"confirm {proposal_id}":
        return {"status": "confirmation_required", "proposal_id": str(proposal_id)}

    status, proposal = store.prepare_checkin_application(proposal_id, confirmation_event)
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
) -> dict[str, object]:
    """Terminally reject exactly one pending proposal without an external write."""

    if rejection_event != f"reject {proposal_id}":
        return {"status": "rejection_required", "proposal_id": str(proposal_id)}
    status, proposal = store.reject_checkin_proposal(
        proposal_id,
        actor="discord_authorized_user",
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
    from app.llm.gateway import LLMGateway

    model = LLMPlannerModel(LLMGateway(settings))
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
        default_practice_minutes=settings.academic_memory_default_practice_minutes,
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
    syncer = AcademicNotionSync(
        connector=connector,
        store=store,
        discord=adapter,
        discord_channel_id=channel_id,
        timezone=settings.app_timezone,
        clarification_ttl_hours=settings.academic_confirmation_ttl_hours,
        setup_condition_code=notion_setup_condition,
    )
    return _Runtime(
        store,
        delivery,
        model,
        syncer,
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
    "configure_academic_runtime",
    "confirm_checkin_proposal",
    "create_checkin_proposal",
    "extract_checkin_changes",
    "reject_checkin_proposal",
    "run_academic_planner",
    "run_end_of_day_checkin",
    "run_morning_plan",
]
