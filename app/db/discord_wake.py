"""Durable Discord wake handoff persistence.

This boundary stores only verified Discord IDs, allowlisted routing metadata,
and an artifact key for redacted inbound content. It deliberately has no queue
imports so gateway and worker code can compose it inside their own transactions.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from app.db.models import DiscordAbortRequest, DiscordWakeInbound

DiscordWakeEventKind = Literal["message", "interaction"]
DiscordWakeAction = Literal[
    "academic_checkin",
    "academic_continuation",
    "agent_clarification",
    "proposal_confirmation",
    "proposal_rejection",
]
DiscordWakeState = Literal["queued", "running", "abort_requested", "aborted", "completed", "failed"]
DiscordWakeAcceptStatus = Literal["created", "replayed"]
DiscordAbortReceiptStatus = Literal["processing", "accepted", "no_active", "unconfirmed"]
DiscordAbortSafeToolStatus = Literal[
    "none",
    "cancelled",
    "cancellation_requested",
    "unknown",
    "completed_before_cancel",
]
DiscordWakeActivityPhase = Literal[
    "accepted",
    "queued",
    "runtime_check",
    "model_waiting",
    "model_turn_started",
    "model_turn_pending",
    "tool_started",
    "tool_succeeded",
    "tool_failed",
    "proposal_persistence",
    "confirmation_write",
    "external_write",
    "reply_delivery",
    "abort_requested",
    "terminal",
]
DiscordWakeToolStatus = Literal[
    "not_started",
    "running",
    "succeeded",
    "failed",
    "unknown",
    "completed_before_cancel",
    "cancellation_requested",
    "cancelled",
]
DiscordWakeSideEffectClass = Literal[
    "read_only",
    "proposal_only",
    "durable_local_write",
    "external_write",
    "unknown",
]

DISCORD_WAKE_ACTIONS: frozenset[str] = frozenset(
    (
        "academic_checkin",
        "academic_continuation",
        "agent_clarification",
        "proposal_confirmation",
        "proposal_rejection",
    )
)
DISCORD_WAKE_EVENT_KINDS: frozenset[str] = frozenset(("message", "interaction"))
DISCORD_WAKE_STATES: frozenset[str] = frozenset(
    ("queued", "running", "abort_requested", "aborted", "completed", "failed")
)
DISCORD_WAKE_ACTIVE_STATES: frozenset[str] = frozenset(("queued", "running", "abort_requested"))
DISCORD_WAKE_TERMINAL_STATES: frozenset[str] = frozenset(("completed", "failed", "aborted"))
DISCORD_WAKE_ACTIVITY_PHASES: frozenset[str] = frozenset(
    (
        "accepted",
        "queued",
        "runtime_check",
        "model_waiting",
        "model_turn_started",
        "model_turn_pending",
        "tool_started",
        "tool_succeeded",
        "tool_failed",
        "proposal_persistence",
        "confirmation_write",
        "external_write",
        "reply_delivery",
        "abort_requested",
        "terminal",
    )
)
DISCORD_WAKE_TOOL_STATUSES: frozenset[str] = frozenset(
    (
        "not_started",
        "running",
        "succeeded",
        "failed",
        "unknown",
        "completed_before_cancel",
        "cancellation_requested",
        "cancelled",
    )
)
DISCORD_WAKE_SIDE_EFFECT_CLASSES: frozenset[str] = frozenset(
    ("read_only", "proposal_only", "durable_local_write", "external_write", "unknown")
)
DISCORD_WAKE_TOOL_NAMES: frozenset[str] = frozenset(
    (
        "archive_assessment",
        "attach_material_to_assessment",
        "create_assessment",
        "create_course_event",
        "create_misc_task",
        "find_course_event_slots",
        "inspect_inbound_pdf",
        "manage_academic_memory",
        "prepare_job_interview",
        "propose_interview_date",
        "propose_interview_plan_save",
        "search_assessment_materials",
        "search_calendar_items",
        "search_courses",
        "search_job_interviews",
        "search_jobs_context",
        "search_pending_assessment_creates",
        "update_assessment",
    )
)
DISCORD_WAKE_NONCE_MAX_LENGTH = 64


class DiscordWakeNonceReplayError(ValueError):
    """Raised when a handoff nonce is reused by a different Discord event."""


@dataclass(frozen=True, slots=True)
class DiscordWakeInboundInput:
    """Verified inbound Discord wake handoff metadata.

    Raw Discord message or interaction content is not accepted here. Callers
    must write redacted content to the artifact store first and pass only the
    resulting key.
    """

    discord_event_id: str
    handoff_nonce: str
    event_kind: DiscordWakeEventKind
    action: DiscordWakeAction
    content_artifact_key: str
    received_at: datetime
    action_id: uuid.UUID | None = None
    clarification_id: uuid.UUID | None = None
    interaction_action: str | None = None
    discord_channel_id: str | None = None
    discord_user_id: str | None = None
    discord_message_id: str | None = None
    discord_interaction_id: str | None = None
    ack_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class DiscordWakeAcceptResult:
    """Result returned after creating or replaying an inbound handoff."""

    status: DiscordWakeAcceptStatus
    wake_id: uuid.UUID
    state: str
    enqueued: bool


@dataclass(frozen=True, slots=True)
class DiscordWakeEnqueueDecision:
    """Whether an accepted wake should still be deferred to Procrastinate."""

    wake_id: uuid.UUID
    state: str
    queue_job_id: int | None
    should_enqueue: bool
    abort_requested: bool


@dataclass(frozen=True, slots=True)
class DiscordWakeQueueBindingResult:
    """Result of binding a Procrastinate job ID to a wake row."""

    wake_id: uuid.UUID
    state: str
    queue_job_id: int | None
    should_cancel_queue_job: bool
    abort_requested: bool


@dataclass(frozen=True, slots=True)
class DiscordWakeAbortTarget:
    """Bounded abort target metadata safe to return through handoff APIs."""

    wake_id: uuid.UUID
    prior_state: str
    state: str
    queue_job_id: int | None


@dataclass(frozen=True, slots=True)
class DiscordWakeAbortRequestResult:
    """Idempotent result for one ABORT Discord event."""

    abort_event_id: str
    selected_count: int
    newly_requested_count: int
    queued_count: int
    running_count: int
    targets: tuple[DiscordWakeAbortTarget, ...]


@dataclass(frozen=True, slots=True)
class DiscordWakeActivitySnapshot:
    """Allowlisted execution status for user-visible abort receipts."""

    wake_id: uuid.UUID
    state: str
    activity_phase: str | None
    activity_model_turn: int | None
    activity_tool_name: str | None
    activity_tool_status: str | None
    activity_side_effect_class: str | None
    activity_updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class DiscordWakeAbortStatusSnapshot:
    """Bounded aggregate status for one abort receipt."""

    total_count: int
    abort_requested_count: int
    aborted_count: int
    completed_count: int
    failed_count: int
    queued_count: int
    running_count: int
    activity: DiscordWakeActivitySnapshot | None
    targets: tuple[DiscordWakeAbortTarget, ...]


@dataclass(frozen=True, slots=True)
class DiscordAbortRequestRecord:
    """Content-free idempotency and receipt snapshot for one ABORT event."""

    created: bool
    abort_event_id: str
    status: DiscordAbortReceiptStatus
    target_count: int
    running_count: int
    queued_count: int
    safe_activity_label: str | None
    safe_tool_status: DiscordAbortSafeToolStatus


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded(value: str, field: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return cleaned


def _optional_bounded(value: str | None, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    return _bounded(value, field, max_length)


def _queue_job_id(value: int | str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("queue_job_id must be an integer") from None
    if parsed <= 0:
        raise ValueError("queue_job_id must be positive")
    return parsed


def _activity_phase(value: str) -> str:
    cleaned = _bounded(value, "activity_phase", 64)
    if cleaned not in DISCORD_WAKE_ACTIVITY_PHASES:
        raise ValueError("Discord wake activity phase is invalid")
    return cleaned


def _optional_tool_status(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _bounded(value, "activity_tool_status", 64)
    if cleaned not in DISCORD_WAKE_TOOL_STATUSES:
        raise ValueError("Discord wake activity tool status is invalid")
    return cleaned


def _optional_side_effect_class(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _bounded(value, "activity_side_effect_class", 64)
    if cleaned not in DISCORD_WAKE_SIDE_EFFECT_CLASSES:
        raise ValueError("Discord wake activity side-effect class is invalid")
    return cleaned


def _optional_tool_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _bounded(value, "activity_tool_name", 128)
    if cleaned not in DISCORD_WAKE_TOOL_NAMES:
        raise ValueError("Discord wake activity tool name is invalid")
    return cleaned


def _validate_event(event: DiscordWakeInboundInput) -> dict[str, object]:
    if event.event_kind not in DISCORD_WAKE_EVENT_KINDS:
        raise ValueError("Discord wake event kind is invalid")
    if event.action not in DISCORD_WAKE_ACTIONS:
        raise ValueError("Discord wake action is invalid")
    interaction_action = event.interaction_action
    if event.event_kind == "interaction":
        if interaction_action not in {
            "quiz",
            "assignment",
            "tutorial",
            "lab",
            "event",
            "ignore",
        }:
            raise ValueError("Discord wake interaction action is invalid")
    elif interaction_action is not None:
        raise ValueError("Discord message wake cannot contain an interaction action")
    return {
        "discord_event_id": _bounded(event.discord_event_id, "discord_event_id", 255),
        "handoff_nonce": _bounded(
            event.handoff_nonce, "handoff_nonce", DISCORD_WAKE_NONCE_MAX_LENGTH
        ),
        "event_kind": event.event_kind,
        "action": event.action,
        "action_id": event.action_id,
        "clarification_id": event.clarification_id,
        "interaction_action": interaction_action,
        "discord_channel_id": _optional_bounded(event.discord_channel_id, "discord_channel_id", 32),
        "discord_user_id": _optional_bounded(event.discord_user_id, "discord_user_id", 32),
        "discord_message_id": _optional_bounded(event.discord_message_id, "discord_message_id", 32),
        "discord_interaction_id": _optional_bounded(
            event.discord_interaction_id, "discord_interaction_id", 32
        ),
        "ack_message_id": _optional_bounded(event.ack_message_id, "ack_message_id", 32),
        "content_artifact_key": _bounded(event.content_artifact_key, "content_artifact_key", 512),
        "received_at": _utc(event.received_at, "received_at"),
    }


def _wake_or_raise(session: Session, wake_id: uuid.UUID) -> DiscordWakeInbound:
    wake = session.get(DiscordWakeInbound, wake_id, with_for_update=True)
    if wake is None:
        raise NoResultFound(f"Discord wake inbound handoff {wake_id} was not found")
    return wake


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DiscordWakeRepository:
    """Accept and advance verified Discord wake handoffs without committing."""

    @staticmethod
    def record_abort_request(
        session: Session,
        *,
        abort_event_id: str,
        handoff_nonce: str,
        channel_id: str,
        user_id: str,
        ack_message_id: str,
        received_at: datetime,
    ) -> DiscordAbortRequestRecord:
        """Create or replay one content-free ABORT request."""

        values = {
            "abort_event_id": _bounded(abort_event_id, "abort_event_id", 255),
            "handoff_nonce": _bounded(
                handoff_nonce,
                "handoff_nonce",
                DISCORD_WAKE_NONCE_MAX_LENGTH,
            ),
            "discord_channel_id": _bounded(channel_id, "channel_id", 32),
            "discord_user_id": _bounded(user_id, "user_id", 32),
            "ack_message_id": _bounded(ack_message_id, "ack_message_id", 32),
            "received_at": _utc(received_at, "received_at"),
        }
        existing = session.get(
            DiscordAbortRequest,
            values["abort_event_id"],
            with_for_update=True,
        )
        if existing is not None:
            _verify_abort_replay(existing, values)
            return _abort_request_record(existing, created=False)
        nonce_owner = session.scalar(
            select(DiscordAbortRequest)
            .where(DiscordAbortRequest.handoff_nonce == values["handoff_nonce"])
            .with_for_update()
        )
        if nonce_owner is not None:
            raise DiscordWakeNonceReplayError(
                "Discord abort handoff nonce was already used by another event"
            )
        row = DiscordAbortRequest(**values)
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            existing = session.get(
                DiscordAbortRequest,
                values["abort_event_id"],
                with_for_update=True,
            )
            if existing is not None:
                _verify_abort_replay(existing, values)
                return _abort_request_record(existing, created=False)
            raise
        return _abort_request_record(row, created=True)

    @staticmethod
    def finalize_abort_request(
        session: Session,
        *,
        abort_event_id: str,
        status: Literal["accepted", "no_active", "unconfirmed"],
        target_count: int,
        running_count: int,
        queued_count: int,
        safe_activity_label: str | None,
        safe_tool_status: DiscordAbortSafeToolStatus,
    ) -> DiscordAbortRequestRecord:
        row = session.get(
            DiscordAbortRequest,
            _bounded(abort_event_id, "abort_event_id", 255),
            with_for_update=True,
        )
        if row is None:
            raise NoResultFound("Discord abort request was not found")
        for value, field in (
            (target_count, "target_count"),
            (running_count, "running_count"),
            (queued_count, "queued_count"),
        ):
            if value < 0 or value > 100:
                raise ValueError(f"{field} must be between 0 and 100")
        if safe_tool_status not in {
            "none",
            "cancelled",
            "cancellation_requested",
            "unknown",
            "completed_before_cancel",
        }:
            raise ValueError("safe_tool_status is invalid")
        row.status = status
        row.target_count = target_count
        row.running_count = running_count
        row.queued_count = queued_count
        row.safe_activity_label = _optional_bounded(
            safe_activity_label,
            "safe_activity_label",
            80,
        )
        row.safe_tool_status = safe_tool_status
        row.completed_at = _utc_now()
        session.flush()
        return _abort_request_record(row, created=False)

    @staticmethod
    def accept_verified_event(
        session: Session,
        event: DiscordWakeInboundInput,
    ) -> DiscordWakeAcceptResult:
        """Create one inbound handoff, idempotently replaying the same event ID."""

        values = _validate_event(event)
        existing_event = session.scalar(
            select(DiscordWakeInbound)
            .where(DiscordWakeInbound.discord_event_id == values["discord_event_id"])
            .with_for_update()
        )
        if existing_event is not None:
            if existing_event.handoff_nonce != values["handoff_nonce"]:
                raise ValueError("Discord event ID replay used a different handoff nonce")
            return DiscordWakeAcceptResult(
                status="replayed",
                wake_id=existing_event.id,
                state=existing_event.state,
                enqueued=existing_event.enqueued_at is not None,
            )

        existing_nonce = session.scalar(
            select(DiscordWakeInbound)
            .where(DiscordWakeInbound.handoff_nonce == values["handoff_nonce"])
            .with_for_update()
        )
        if existing_nonce is not None:
            raise DiscordWakeNonceReplayError(
                "Discord wake handoff nonce was already used by another event"
            )

        wake = DiscordWakeInbound(
            **values,
            activity_phase="accepted",
            activity_updated_at=_utc_now(),
        )
        try:
            with session.begin_nested():
                session.add(wake)
                session.flush()
                _apply_covering_abort(session, wake)
        except IntegrityError:
            existing_event = session.scalar(
                select(DiscordWakeInbound)
                .where(DiscordWakeInbound.discord_event_id == values["discord_event_id"])
                .with_for_update()
            )
            if existing_event is not None:
                if existing_event.handoff_nonce != values["handoff_nonce"]:
                    raise ValueError(
                        "Discord event ID replay used a different handoff nonce"
                    ) from None
                return DiscordWakeAcceptResult(
                    status="replayed",
                    wake_id=existing_event.id,
                    state=existing_event.state,
                    enqueued=existing_event.enqueued_at is not None,
                )
            existing_nonce = session.scalar(
                select(DiscordWakeInbound)
                .where(DiscordWakeInbound.handoff_nonce == values["handoff_nonce"])
                .with_for_update()
            )
            if existing_nonce is not None:
                raise DiscordWakeNonceReplayError(
                    "Discord wake handoff nonce was already used by another event"
                ) from None
            raise

        return DiscordWakeAcceptResult(
            status="created",
            wake_id=wake.id,
            state=wake.state,
            enqueued=False,
        )

    @staticmethod
    def enqueue_decision(session: Session, wake_id: uuid.UUID) -> DiscordWakeEnqueueDecision:
        """Return whether the caller should still enqueue this wake.

        Call this after accepting a wake and before deferring a queue job. If an
        ABORT won the race first, callers can skip deferral entirely.
        """

        wake = _wake_or_raise(session, wake_id)
        _apply_covering_abort(session, wake)
        abort_requested = wake.state in {"abort_requested", "aborted"}
        return DiscordWakeEnqueueDecision(
            wake_id=wake.id,
            state=wake.state,
            queue_job_id=wake.queue_job_id,
            should_enqueue=not abort_requested and wake.enqueued_at is None,
            abort_requested=abort_requested,
        )

    @staticmethod
    def bind_queue_job_id(
        session: Session,
        wake_id: uuid.UUID,
        queue_job_id: int | str,
    ) -> DiscordWakeQueueBindingResult:
        """Attach the queue job ID and report whether it must be cancelled.

        This is the enqueue race primitive used when an ABORT can arrive between
        queue deferral and durable queue binding.
        """

        wake = _wake_or_raise(session, wake_id)
        parsed_job_id = _queue_job_id(queue_job_id)
        if wake.queue_job_id is not None and wake.queue_job_id != parsed_job_id:
            raise ValueError("Discord wake queue job ID is already bound")
        if wake.queue_job_id is None:
            wake.queue_job_id = parsed_job_id
        if wake.enqueued_at is None:
            wake.enqueued_at = _utc_now()
        if wake.state not in {"abort_requested", "aborted"}:
            wake.activity_phase = "queued"
            wake.activity_updated_at = _utc_now()
        session.flush()
        abort_requested = wake.state in {"abort_requested", "aborted"}
        return DiscordWakeQueueBindingResult(
            wake_id=wake.id,
            state=wake.state,
            queue_job_id=wake.queue_job_id,
            should_cancel_queue_job=abort_requested,
            abort_requested=abort_requested,
        )

    @staticmethod
    def mark_enqueued(
        session: Session,
        wake_id: uuid.UUID,
        *,
        queue_job_id: int | str | None = None,
    ) -> DiscordWakeInbound:
        """Record successful queue acceptance so later handoffs cannot add another job."""

        if queue_job_id is not None:
            DiscordWakeRepository.bind_queue_job_id(session, wake_id, queue_job_id)
            return _wake_or_raise(session, wake_id)
        wake = _wake_or_raise(session, wake_id)
        if wake.enqueued_at is None:
            wake.enqueued_at = _utc_now()
        if wake.state not in {"abort_requested", "aborted"}:
            wake.activity_phase = "queued"
            wake.activity_updated_at = _utc_now()
        session.flush()
        return wake

    @staticmethod
    def get_by_id(session: Session, wake_id: uuid.UUID) -> DiscordWakeInbound | None:
        return session.get(DiscordWakeInbound, wake_id)

    @staticmethod
    def get_by_event_id(session: Session, discord_event_id: str) -> DiscordWakeInbound | None:
        event_id = _bounded(discord_event_id, "discord_event_id", 255)
        return session.scalar(
            select(DiscordWakeInbound).where(DiscordWakeInbound.discord_event_id == event_id)
        )

    @staticmethod
    def mark_running(session: Session, wake_id: uuid.UUID) -> DiscordWakeInbound:
        wake = _wake_or_raise(session, wake_id)
        if wake.state == "completed":
            raise ValueError("completed Discord wake handoff cannot return to running")
        if wake.state in {"abort_requested", "aborted"}:
            raise ValueError("aborted Discord wake handoff cannot start running")
        wake.state = "running"
        wake.retry_count += 1
        wake.started_at = _utc_now()
        wake.last_error_code = None
        wake.failed_at = None
        wake.activity_phase = "runtime_check"
        wake.activity_tool_status = None
        wake.activity_updated_at = _utc_now()
        session.flush()
        return wake

    @staticmethod
    def mark_completed(session: Session, wake_id: uuid.UUID) -> DiscordWakeInbound:
        wake = _wake_or_raise(session, wake_id)
        if wake.state == "failed":
            raise ValueError("failed Discord wake handoff must be marked running before completion")
        if wake.state == "aborted":
            raise ValueError("aborted Discord wake handoff cannot be marked completed")
        wake.state = "completed"
        wake.completed_at = _utc_now()
        wake.last_error_code = None
        wake.activity_phase = "terminal"
        wake.activity_updated_at = _utc_now()
        session.flush()
        return wake

    @staticmethod
    def mark_failed(
        session: Session,
        wake_id: uuid.UUID,
        *,
        error_code: str,
    ) -> DiscordWakeInbound:
        wake = _wake_or_raise(session, wake_id)
        if wake.state == "completed":
            raise ValueError("completed Discord wake handoff cannot be marked failed")
        if wake.state == "aborted":
            raise ValueError("aborted Discord wake handoff cannot be marked failed")
        wake.state = "failed"
        wake.last_error_code = _bounded(error_code, "error_code", 128)
        wake.failed_at = _utc_now()
        wake.activity_phase = "terminal"
        wake.activity_updated_at = _utc_now()
        session.flush()
        return wake

    @staticmethod
    def request_abort_for_scope(
        session: Session,
        *,
        channel_id: str,
        user_id: str,
        abort_event_id: str,
        abort_received_at: datetime,
    ) -> DiscordWakeAbortRequestResult:
        """Idempotently mark earlier active message wakes for owner/channel ABORT."""

        bounded_channel = _bounded(channel_id, "channel_id", 32)
        bounded_user = _bounded(user_id, "user_id", 32)
        bounded_abort_event = _bounded(abort_event_id, "abort_event_id", 255)
        cutoff = _utc(abort_received_at, "abort_received_at")
        rows = session.scalars(
            select(DiscordWakeInbound)
            .where(
                DiscordWakeInbound.event_kind == "message",
                DiscordWakeInbound.discord_channel_id == bounded_channel,
                DiscordWakeInbound.discord_user_id == bounded_user,
                DiscordWakeInbound.received_at < cutoff,
                (
                    DiscordWakeInbound.state.in_(DISCORD_WAKE_ACTIVE_STATES)
                    | (DiscordWakeInbound.abort_requested_by_event_id == bounded_abort_event)
                ),
            )
            .order_by(DiscordWakeInbound.received_at.asc(), DiscordWakeInbound.created_at.asc())
            .with_for_update()
        ).all()

        now = _utc_now()
        newly_requested = 0
        prior_states = {wake.id: wake.abort_requested_prior_state or wake.state for wake in rows}
        for wake in rows:
            if wake.abort_requested_prior_state is None:
                wake.abort_requested_prior_state = wake.state
            if wake.abort_requested_by_event_id is None:
                wake.abort_requested_by_event_id = bounded_abort_event
            if wake.abort_requested_at is None:
                wake.abort_requested_at = now
            if wake.abort_reason_code is None:
                wake.abort_reason_code = "user_abort"
            if wake.state in {"queued", "running"}:
                wake.state = "abort_requested"
                newly_requested += 1
            if wake.activity_phase is None:
                wake.activity_phase = "abort_requested"
                wake.activity_updated_at = now
            if wake.activity_tool_status is None:
                wake.activity_tool_status = "cancellation_requested"
        session.flush()
        return DiscordWakeAbortRequestResult(
            abort_event_id=bounded_abort_event,
            selected_count=len(rows),
            newly_requested_count=newly_requested,
            queued_count=sum(1 for state in prior_states.values() if state == "queued"),
            running_count=sum(1 for state in prior_states.values() if state == "running"),
            targets=tuple(
                DiscordWakeAbortTarget(
                    wake_id=wake.id,
                    prior_state=prior_states[wake.id],
                    state=wake.state,
                    queue_job_id=wake.queue_job_id,
                )
                for wake in rows
            ),
        )

    @staticmethod
    def record_activity(
        session: Session,
        wake_id: uuid.UUID,
        *,
        phase: DiscordWakeActivityPhase,
        model_turn: int | None = None,
        tool_name: str | None = None,
        tool_status: DiscordWakeToolStatus | None = None,
        side_effect_class: DiscordWakeSideEffectClass | None = None,
        occurred_at: datetime | None = None,
    ) -> DiscordWakeInbound:
        """Persist a bounded, allowlisted activity snapshot for abort receipts."""

        wake = _wake_or_raise(session, wake_id)
        if model_turn is not None and (model_turn < 1 or model_turn > 50):
            raise ValueError("model_turn must be between 1 and 50")
        wake.activity_phase = _activity_phase(phase)
        wake.activity_model_turn = model_turn
        wake.activity_tool_name = _optional_tool_name(tool_name)
        wake.activity_tool_status = _optional_tool_status(tool_status)
        wake.activity_side_effect_class = _optional_side_effect_class(side_effect_class)
        wake.activity_updated_at = _utc(occurred_at, "occurred_at") if occurred_at else _utc_now()
        session.flush()
        return wake

    @staticmethod
    def mark_aborted(
        session: Session,
        wake_id: uuid.UUID,
        *,
        reason_code: str = "user_abort",
        abort_event_id: str | None = None,
    ) -> DiscordWakeInbound:
        """Mark a requested wake terminally aborted without overwriting completed work."""

        wake = _wake_or_raise(session, wake_id)
        if wake.state in {"completed", "failed"}:
            return wake
        now = _utc_now()
        if abort_event_id is not None and wake.abort_requested_by_event_id is None:
            wake.abort_requested_by_event_id = _bounded(abort_event_id, "abort_event_id", 255)
        if wake.abort_requested_at is None:
            wake.abort_requested_at = now
        wake.state = "aborted"
        wake.abort_reason_code = _bounded(reason_code, "reason_code", 128)
        wake.abort_terminal_at = now
        wake.activity_phase = "terminal"
        wake.activity_tool_status = wake.activity_tool_status or "cancelled"
        wake.activity_updated_at = now
        session.flush()
        return wake

    @staticmethod
    def mark_queue_cancelled_aborted(
        session: Session,
        wake_id: uuid.UUID,
        *,
        abort_event_id: str | None = None,
    ) -> DiscordWakeInbound:
        """Record successful Procrastinate cancellation of a queued wake."""

        return DiscordWakeRepository.mark_aborted(
            session,
            wake_id,
            reason_code="queue_cancelled",
            abort_event_id=abort_event_id,
        )

    @staticmethod
    def abort_status_snapshot(
        session: Session,
        wake_ids: Sequence[uuid.UUID],
    ) -> DiscordWakeAbortStatusSnapshot:
        """Return bounded target counts and the latest safe activity snapshot."""

        unique_ids = tuple(dict.fromkeys(wake_ids))
        if not unique_ids:
            return DiscordWakeAbortStatusSnapshot(
                total_count=0,
                abort_requested_count=0,
                aborted_count=0,
                completed_count=0,
                failed_count=0,
                queued_count=0,
                running_count=0,
                activity=None,
                targets=(),
            )
        rows = session.scalars(
            select(DiscordWakeInbound).where(DiscordWakeInbound.id.in_(unique_ids))
        ).all()
        targets = tuple(
            DiscordWakeAbortTarget(
                wake_id=wake.id,
                prior_state=wake.state,
                state=wake.state,
                queue_job_id=wake.queue_job_id,
            )
            for wake in rows
        )
        selected_activity = (
            max(
                rows,
                key=lambda wake: (
                    wake.abort_requested_prior_state == "running",
                    wake.state in {"running", "abort_requested"},
                    wake.activity_updated_at or wake.started_at or wake.received_at,
                ),
            )
            if rows
            else None
        )
        activity = (
            DiscordWakeActivitySnapshot(
                wake_id=selected_activity.id,
                state=selected_activity.state,
                activity_phase=selected_activity.activity_phase,
                activity_model_turn=selected_activity.activity_model_turn,
                activity_tool_name=selected_activity.activity_tool_name,
                activity_tool_status=selected_activity.activity_tool_status,
                activity_side_effect_class=selected_activity.activity_side_effect_class,
                activity_updated_at=selected_activity.activity_updated_at,
            )
            if selected_activity is not None
            else None
        )
        return DiscordWakeAbortStatusSnapshot(
            total_count=len(rows),
            abort_requested_count=sum(1 for wake in rows if wake.state == "abort_requested"),
            aborted_count=sum(1 for wake in rows if wake.state == "aborted"),
            completed_count=sum(1 for wake in rows if wake.state == "completed"),
            failed_count=sum(1 for wake in rows if wake.state == "failed"),
            queued_count=sum(1 for wake in rows if wake.state == "queued"),
            running_count=sum(1 for wake in rows if wake.state == "running"),
            activity=activity,
            targets=targets,
        )


def _apply_covering_abort(session: Session, wake: DiscordWakeInbound) -> None:
    if (
        wake.event_kind != "message"
        or wake.state != "queued"
        or wake.queue_job_id is not None
        or wake.discord_channel_id is None
        or wake.discord_user_id is None
    ):
        return
    abort = session.scalar(
        select(DiscordAbortRequest)
        .where(
            DiscordAbortRequest.discord_channel_id == wake.discord_channel_id,
            DiscordAbortRequest.discord_user_id == wake.discord_user_id,
            DiscordAbortRequest.received_at > wake.received_at,
        )
        .order_by(DiscordAbortRequest.received_at.asc())
        .limit(1)
        .with_for_update()
    )
    if abort is None:
        return
    now = _utc_now()
    wake.state = "aborted"
    wake.abort_requested_at = now
    wake.abort_requested_by_event_id = abort.abort_event_id
    wake.abort_requested_prior_state = "queued"
    wake.abort_terminal_at = now
    wake.abort_reason_code = "late_handoff_after_user_abort"
    wake.activity_phase = "terminal"
    wake.activity_tool_status = "cancelled"
    wake.activity_updated_at = now
    session.flush()


def _verify_abort_replay(
    row: DiscordAbortRequest,
    values: Mapping[str, object],
) -> None:
    received_at = row.received_at
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        received_at = received_at.replace(tzinfo=UTC)
    else:
        received_at = received_at.astimezone(UTC)
    if (
        row.handoff_nonce != values["handoff_nonce"]
        or row.discord_channel_id != values["discord_channel_id"]
        or row.discord_user_id != values["discord_user_id"]
        or row.ack_message_id != values["ack_message_id"]
        or received_at != values["received_at"]
    ):
        raise ValueError("Discord abort replay metadata does not match")


def _abort_request_record(
    row: DiscordAbortRequest,
    *,
    created: bool,
) -> DiscordAbortRequestRecord:
    return DiscordAbortRequestRecord(
        created=created,
        abort_event_id=row.abort_event_id,
        status=cast(DiscordAbortReceiptStatus, row.status),
        target_count=row.target_count,
        running_count=row.running_count,
        queued_count=row.queued_count,
        safe_activity_label=row.safe_activity_label,
        safe_tool_status=cast(DiscordAbortSafeToolStatus, row.safe_tool_status),
    )


__all__ = [
    "DISCORD_WAKE_ACTIONS",
    "DISCORD_WAKE_ACTIVE_STATES",
    "DISCORD_WAKE_ACTIVITY_PHASES",
    "DISCORD_WAKE_EVENT_KINDS",
    "DISCORD_WAKE_NONCE_MAX_LENGTH",
    "DISCORD_WAKE_SIDE_EFFECT_CLASSES",
    "DISCORD_WAKE_STATES",
    "DISCORD_WAKE_TERMINAL_STATES",
    "DISCORD_WAKE_TOOL_NAMES",
    "DISCORD_WAKE_TOOL_STATUSES",
    "DiscordAbortReceiptStatus",
    "DiscordAbortRequestRecord",
    "DiscordAbortSafeToolStatus",
    "DiscordWakeAbortRequestResult",
    "DiscordWakeAbortStatusSnapshot",
    "DiscordWakeAbortTarget",
    "DiscordWakeAcceptResult",
    "DiscordWakeAction",
    "DiscordWakeActivityPhase",
    "DiscordWakeActivitySnapshot",
    "DiscordWakeEnqueueDecision",
    "DiscordWakeEventKind",
    "DiscordWakeInboundInput",
    "DiscordWakeNonceReplayError",
    "DiscordWakeQueueBindingResult",
    "DiscordWakeRepository",
    "DiscordWakeSideEffectClass",
    "DiscordWakeState",
    "DiscordWakeToolStatus",
]
