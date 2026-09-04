"""Database projections used by the read-only API and server-rendered UI."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.redaction import redact_text
from app.db.finance import FinanceRepository
from app.db.models import (
    AgentRun,
    AuditEvent,
    Delivery,
    EvidenceRef,
    HealthCheck,
    HealthState,
    RunStatus,
    RunStep,
    UIAcknowledgement,
)
from app.db.repositories import UIAcknowledgementRepository
from app.operations.contracts import (
    AcknowledgementResult,
    ActivityDetail,
    ActivityItem,
    ActivityPage,
    ApprovedSource,
    ConsoleState,
    DeliveryReceipt,
    EvidenceLink,
    ExternalLink,
    HealthCard,
    SourceSettings,
    TimelineStep,
)

_CARD_LABELS: Mapping[str, str] = {
    "finance": "Finance briefing",
    "code_review": "Code review",
    "academic_planner": "Academic planner",
    "shared_services": "Shared services",
}
_AGENT_CHECK_NAMES: Mapping[str, tuple[str, ...]] = {
    "finance": ("finance", "finance_briefing"),
    "code_review": ("code_review", "code-review"),
    "academic_planner": ("academic_planner", "academic-planner"),
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def _safe_text(value: str | None, *, fallback: str = "No summary recorded") -> str:
    return redact_text(value or fallback)[:4000]


def _safe_link(label: str, value: str | None) -> ExternalLink | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    try:
        return ExternalLink.model_validate({"label": label, "url": value})
    except ValueError:
        return None


def _console_state(status: str) -> ConsoleState:
    if status == RunStatus.SUCCEEDED.value:
        return "healthy"
    if status in {RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
        return "failed"
    return "attention"


def _health_state(state: HealthState) -> ConsoleState:
    if str(state) == HealthState.HEALTHY.value:
        return "healthy"
    if str(state) == HealthState.FAILED.value:
        return "failed"
    return "attention"


def _evidence_link(reference: EvidenceRef) -> EvidenceLink | None:
    safe_link = _safe_link("evidence", reference.url)
    if safe_link is None:
        return None
    return EvidenceLink(
        claim_id=reference.claim_id,
        title=_safe_text(reference.title),
        url=safe_link.url,
        published_at=_aware(reference.published_at),
        classification=str(reference.classification),
    )


class OperationsRepository:
    """Query-only operational projections plus the isolated UI acknowledgement write."""

    @staticmethod
    def health_cards(session: Session) -> tuple[HealthCard, ...]:
        checks = list(session.scalars(select(HealthCheck).order_by(HealthCheck.checked_at.desc())))
        latest: dict[str, HealthCheck] = {}
        for check in checks:
            latest.setdefault(check.check_name, check)
        cards: list[HealthCard] = []
        for component, names in _AGENT_CHECK_NAMES.items():
            check = next((latest[name] for name in names if name in latest), None)
            cards.append(_card(component, check))
        cards.append(_card("shared_services", latest.get("shared_services")))
        return tuple(cards)

    @staticmethod
    def source_settings(
        session: Session,
        *,
        allowlist_version: str,
        schedule_configured: bool = True,
    ) -> SourceSettings:
        """Project the audited finance allowlist without calling another HTTP route."""

        records = FinanceRepository.list_source_records(
            session,
            allowlist_version=allowlist_version,
        )
        approvals = FinanceRepository.load_approved_sources(
            session,
            allowlist_version=allowlist_version,
        )
        approval_complete = FinanceRepository.source_approval_gate(
            session,
            allowlist_version=allowlist_version,
        )
        enabled_count = sum(
            source.enabled
            and source.approved_at is not None
            and source.approval_audit_id is not None
            for source in approvals
        )
        if not records:
            diagnostic = "No finance sources are recorded for this allowlist version"
        elif len(records) != 8:
            diagnostic = f"Finance allowlist has {len(records)} of 8 required sources"
        elif not approval_complete:
            diagnostic = f"Finance schedule is gated; {enabled_count} of 8 sources are approved"
        elif not schedule_configured:
            diagnostic = "Finance schedule is disabled by configuration"
        else:
            diagnostic = "Finance source approval is complete; the market-open schedule is enabled"
        return SourceSettings(
            allowlist_version=allowlist_version,
            schedule_enabled=schedule_configured and approval_complete,
            approval_complete=approval_complete,
            sources=tuple(
                ApprovedSource(
                    slot=slot,
                    name=record.name,
                    hostname=urlsplit(record.base_url).hostname or "invalid-source-url",
                    entitlement=record.entitlement,
                    enabled=record.enabled,
                )
                for slot, record in enumerate(records, start=1)
            ),
            diagnostic=diagnostic,
        )

    @staticmethod
    def activity(
        session: Session,
        *,
        user_id: str,
        agent: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        attention_only: bool = False,
        repository: str | None = None,
        ticker_theme: str | None = None,
        course: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> ActivityPage:
        filters: list[ColumnElement[bool]] = []
        if agent:
            filters.append(AgentRun.agent_name == agent)
        if date_from:
            filters.append(AgentRun.started_at >= date_from)
        if date_to:
            filters.append(AgentRun.started_at <= date_to)
        if attention_only:
            filters.append(AgentRun.status.in_((RunStatus.ATTENTION, RunStatus.FAILED)))
        for target_type, term in (
            ("repository", repository),
            ("ticker_theme", ticker_theme),
            ("course", course),
        ):
            if term:
                escaped = term.replace("%", "\\%").replace("_", "\\_")
                pattern = f"%{escaped}%"
                filters.append(
                    or_(
                        AgentRun.summary.ilike(pattern, escape="\\"),
                        exists(
                            select(AuditEvent.id).where(
                                AuditEvent.run_id == AgentRun.id,
                                AuditEvent.target_type == target_type,
                                AuditEvent.target_id.ilike(pattern, escape="\\"),
                            )
                        ),
                    )
                )
        count = session.scalar(select(func.count()).select_from(AgentRun).where(*filters)) or 0
        rows = list(
            session.scalars(
                select(AgentRun)
                .where(*filters)
                .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        items = tuple(_activity_item(session, run, user_id=user_id) for run in rows)
        return ActivityPage(
            items=items,
            page=page,
            page_size=page_size,
            total=count,
            pages=math.ceil(count / page_size) if count else 0,
        )

    @staticmethod
    def detail(session: Session, *, run_id: UUID, user_id: str) -> ActivityDetail | None:
        run = session.get(AgentRun, run_id)
        if run is None:
            return None
        item = _activity_item(session, run, user_id=user_id)
        steps = list(
            session.scalars(
                select(RunStep)
                .where(RunStep.run_id == run_id)
                .order_by(RunStep.created_at, RunStep.attempt)
            )
        )
        deliveries = list(
            session.scalars(
                select(Delivery)
                .where(Delivery.run_id == run_id)
                .order_by(Delivery.created_at, Delivery.id)
            )
        )
        evidence = list(
            session.scalars(
                select(EvidenceRef)
                .where(EvidenceRef.run_id == run_id)
                .order_by(EvidenceRef.created_at, EvidenceRef.id)
            )
        )
        warnings = tuple(
            value
            for value in (
                _safe_text(run.error_code, fallback="") or None,
                *(
                    _safe_text(step.diagnostic, fallback="") or None
                    for step in steps
                    if str(step.status) in {"attention", "failed"}
                ),
                *(
                    _safe_text(delivery.error_code, fallback="") or None
                    for delivery in deliveries
                    if str(delivery.status) in {"uncertain", "failed"}
                ),
            )
            if value
        )
        return ActivityDetail(
            item=item,
            timeline=tuple(
                TimelineStep(
                    name=step.node_name,
                    attempt=step.attempt,
                    status=str(step.status),
                    started_at=_aware(step.started_at),
                    ended_at=_aware(step.ended_at),
                    diagnostic=_safe_text(step.diagnostic, fallback="") or None,
                )
                for step in steps
            ),
            deliveries=tuple(
                DeliveryReceipt(
                    channel=delivery.channel,
                    status=str(delivery.status),
                    sent_at=_aware(delivery.last_attempt_at),
                    link=_safe_link(delivery.channel, delivery.external_url),
                    error_code=_safe_text(delivery.error_code, fallback="") or None,
                )
                for delivery in deliveries
            ),
            evidence=tuple(
                link for reference in evidence if (link := _evidence_link(reference)) is not None
            ),
            warnings=warnings,
            raw_record={
                "run_id": str(run.id),
                "agent": run.agent_name,
                "trigger": run.trigger,
                "schedule": run.schedule,
                "status": str(run.status),
                "started_at": _aware(run.started_at),
                "finished_at": _aware(run.finished_at),
                "model_version": run.model_version,
                "config_version": run.config_version,
                "input_version": run.input_version,
                "summary": _safe_text(run.summary),
                "error_code": _safe_text(run.error_code, fallback="") or None,
                "artifact_reference": run.artifact_key,
            },
        )

    @staticmethod
    def acknowledge(
        session: Session,
        *,
        user_id: str,
        run_id: UUID,
        alert_key: str,
    ) -> AcknowledgementResult | None:
        if session.get(AgentRun, run_id) is None:
            return None
        row = UIAcknowledgementRepository.acknowledge(
            session, user_id=user_id, run_id=run_id, alert_key=alert_key
        )
        session.commit()
        return AcknowledgementResult(
            acknowledgement_id=row.id,
            run_id=run_id,
            alert_key=row.alert_key,
            acknowledged_at=_aware(row.acknowledged_at) or datetime.now(UTC),
        )


def _card(component: str, check: HealthCheck | None) -> HealthCard:
    if check is None:
        return HealthCard(
            component=component,
            label=_CARD_LABELS[component],
            state="attention",
            last_success_at=None,
            next_expected_at=None,
            diagnostic="No deterministic health record has been evaluated yet",
            activity_url=f"/activity?agent={component}"
            if component != "shared_services"
            else "/activity",
        )
    return HealthCard(
        component=component,
        label=_CARD_LABELS[component],
        state=_health_state(check.state),
        last_success_at=_aware(check.last_success_at),
        next_expected_at=_aware(check.next_due_at),
        diagnostic=_safe_text(check.diagnostic),
        activity_url=f"/activity?agent={component}"
        if component != "shared_services"
        else "/activity",
    )


def _activity_item(session: Session, run: AgentRun, *, user_id: str) -> ActivityItem:
    deliveries = list(session.scalars(select(Delivery).where(Delivery.run_id == run.id)))
    evidence = list(session.scalars(select(EvidenceRef).where(EvidenceRef.run_id == run.id)))
    acknowledged = session.scalar(
        select(func.count())
        .select_from(UIAcknowledgement)
        .where(UIAcknowledgement.run_id == run.id, UIAcknowledgement.user_id == user_id)
    )
    return ActivityItem(
        run_id=run.id,
        timestamp=_aware(run.finished_at or run.started_at) or datetime.now(UTC),
        agent=run.agent_name,
        status=str(run.status),
        severity=_console_state(str(run.status)),
        summary=_safe_text(run.summary),
        delivery_links=tuple(
            link
            for delivery in deliveries
            if (link := _safe_link(delivery.channel, delivery.external_url)) is not None
        ),
        evidence_links=tuple(
            link
            for reference in evidence
            if (link := _safe_link(_safe_text(reference.title), reference.url)) is not None
        ),
        unresolved=_safe_text(run.error_code, fallback="") or None,
        acknowledged=bool(acknowledged),
    )


__all__ = ["OperationsRepository"]
