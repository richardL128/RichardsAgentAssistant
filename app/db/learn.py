"""Durable, raw-content-free persistence for Waterloo LEARN metadata."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import Select, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    AcademicProposedChange,
    LearnAnnouncementSemanticResult,
    LearnAnnouncementSource,
    LearnCourse,
    LearnDatedImplication,
    LearnNotificationDelivery,
    LearnNotionProposalLink,
    LearnScheduledItem,
)

LearnProposalCreateStatus = Literal["created", "existing", "requires_explicit_request"]

_SEMANTIC_ACTIVE_STATUSES = frozenset(("valid", "summary_unavailable"))
_PROPOSAL_BLOCKING_STATES = frozenset(
    ("pending", "confirmed", "rejected", "expired", "applied", "skipped")
)


@dataclass(frozen=True, slots=True)
class LearnCourseInput:
    org_unit_id: str
    code: str
    name: str
    term: str | None
    active: bool
    url: str | None
    seen_at: datetime


@dataclass(frozen=True, slots=True)
class LearnScheduledItemInput:
    source_id: str
    course_id: uuid.UUID
    title: str
    start_date: date
    fingerprint: str
    seen_at: datetime
    start_at: datetime | None = None
    due_at: datetime | None = None
    end_at: datetime | None = None
    date_precision: Literal["date", "datetime"] = "datetime"
    completion_state: Literal["unknown", "incomplete", "complete", "cancelled"] = "unknown"
    url: str | None = None


@dataclass(frozen=True, slots=True)
class LearnAnnouncementInput:
    source_id: str
    course_id: uuid.UUID
    effective_at: datetime
    fingerprint: str
    seen_at: datetime
    published_at: datetime | None = None
    updated_at: datetime | None = None
    url: str | None = None
    has_attachments: bool = False


@dataclass(frozen=True, slots=True)
class LearnDatedImplicationInput:
    implication_key: str
    activity_type: str
    academic_date: date
    date_precision: Literal["date", "datetime"]
    evidence_fragments: Sequence[Mapping[str, Any]]
    start_at: datetime | None = None
    end_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LearnSemanticResultInput:
    announcement_id: uuid.UUID
    course_id: uuid.UUID
    source_fingerprint: str
    summary: str
    why_it_matters: str
    action_items: Sequence[Mapping[str, Any]]
    evidence_fragments: Sequence[Mapping[str, Any]]
    source_url: str | None
    model_identity: str
    prompt_version: str
    interpreted_at: datetime
    dated_implications: Sequence[LearnDatedImplicationInput] = ()
    critic_model_identity: str | None = None
    critic_prompt_version: str | None = None
    repair_attempted: bool = False
    anti_copy_passed: bool = True
    status: Literal["valid", "summary_unavailable", "invalid"] = "valid"
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class LearnDeliveryInput:
    message_key: str
    delivery_kind: Literal["announcement_window", "day_before", "reconnect_alert"]
    occurrence_date: date
    channel: str = "discord"
    target: str | None = None
    scheduled_for: datetime | None = None
    announcement_id: uuid.UUID | None = None
    dated_implication_id: uuid.UUID | None = None
    source_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class LearnProposalLinkInput:
    source_kind: Literal["scheduled_item", "announcement_implication"]
    source_id: str
    source_fingerprint: str
    operation: Literal["create_learn_calendar_event", "enrich_learn_calendar_event"]
    idempotency_key: str
    reserved_calendar_notion_id: str
    learn_context_property_id: str
    learn_context_property_name: str
    scheduled_item_id: uuid.UUID | None = None
    dated_implication_id: uuid.UUID | None = None
    proposal_id: uuid.UUID | None = None
    target_notion_page_id: str | None = None
    target_expected_title: str | None = None
    target_expected_date: date | None = None
    target_expected_start_at: datetime | None = None
    target_expected_last_edited_at: datetime | None = None
    explicit_request_key: str | None = None
    conflict_code: str | None = None
    state: Literal["pending", "skipped"] = "pending"


class LearnRepository:
    """Low-level LEARN repository. Callers own transactions."""

    @staticmethod
    def upsert_course(session: Session, item: LearnCourseInput) -> LearnCourse:
        seen_at = _utc(item.seen_at, "seen_at")
        values = {
            "org_unit_id": _bounded(item.org_unit_id, "org_unit_id", 128),
            "code": _bounded(item.code, "code", 64),
            "name": _bounded(item.name, "name", 255),
            "term": _optional_bounded(item.term, "term", 128),
            "active": item.active,
            "url": _optional_bounded(item.url, "url", 2048),
            "last_seen_at": seen_at,
            "disappeared_at": None,
        }
        existing = session.scalar(
            select(LearnCourse).where(LearnCourse.org_unit_id == values["org_unit_id"])
        )
        if existing is not None:
            for key, value in values.items():
                setattr(existing, key, value)
            session.flush()
            return existing
        course = LearnCourse(first_seen_at=seen_at, **values)
        session.add(course)
        session.flush()
        return course

    @staticmethod
    def upsert_scheduled_item(
        session: Session, item: LearnScheduledItemInput
    ) -> LearnScheduledItem:
        seen_at = _utc(item.seen_at, "seen_at")
        fingerprint = _bounded(item.fingerprint, "fingerprint", 128)
        existing = session.scalar(
            select(LearnScheduledItem).where(LearnScheduledItem.source_id == item.source_id)
        )
        if existing is not None and existing.fingerprint != fingerprint:
            LearnRepository._supersede_proposal_links(
                session,
                source_kind="scheduled_item",
                source_id=existing.source_id,
                source_fingerprint=existing.fingerprint,
            )
        values = {
            "source_id": _bounded(item.source_id, "source_id", 255),
            "course_id": item.course_id,
            "title": _bounded(item.title, "title", 500),
            "start_date": item.start_date,
            "start_at": _optional_utc(item.start_at, "start_at"),
            "due_at": _optional_utc(item.due_at, "due_at"),
            "end_at": _optional_utc(item.end_at, "end_at"),
            "date_precision": _choice(item.date_precision, {"date", "datetime"}, "date_precision"),
            "completion_state": _choice(
                item.completion_state,
                {"unknown", "incomplete", "complete", "cancelled"},
                "completion_state",
            ),
            "url": _optional_bounded(item.url, "url", 2048),
            "fingerprint": fingerprint,
            "active": True,
            "last_seen_at": seen_at,
            "disappeared_at": None,
        }
        if existing is not None:
            for key, value in values.items():
                setattr(existing, key, value)
            session.flush()
            return existing
        scheduled = LearnScheduledItem(first_seen_at=seen_at, **values)
        session.add(scheduled)
        session.flush()
        return scheduled

    @staticmethod
    def upsert_announcement(
        session: Session, item: LearnAnnouncementInput
    ) -> LearnAnnouncementSource:
        seen_at = _utc(item.seen_at, "seen_at")
        source_id = _bounded(item.source_id, "source_id", 255)
        fingerprint = _bounded(item.fingerprint, "fingerprint", 128)
        existing = session.scalar(
            select(LearnAnnouncementSource).where(LearnAnnouncementSource.source_id == source_id)
        )
        if existing is not None and existing.fingerprint != fingerprint:
            LearnRepository._supersede_announcement_facts(
                session,
                announcement_id=existing.id,
                source_id=existing.source_id,
                source_fingerprint=existing.fingerprint,
                result_status="superseded",
                implication_status="superseded",
                delivery_status="superseded",
            )
        values = {
            "source_id": source_id,
            "course_id": item.course_id,
            "published_at": _optional_utc(item.published_at, "published_at"),
            "content_updated_at": _optional_utc(item.updated_at, "updated_at"),
            "effective_at": _utc(item.effective_at, "effective_at"),
            "url": _optional_bounded(item.url, "url", 2048),
            "fingerprint": fingerprint,
            "visible": True,
            "has_attachments": item.has_attachments,
            "last_seen_at": seen_at,
            "disappeared_at": None,
        }
        if existing is not None:
            for key, value in values.items():
                setattr(existing, key, value)
            session.flush()
            return existing
        announcement = LearnAnnouncementSource(first_seen_at=seen_at, **values)
        session.add(announcement)
        session.flush()
        return announcement

    @staticmethod
    def mark_announcements_missing(
        session: Session,
        *,
        seen_source_ids: set[str],
        now: datetime,
        course_ids: set[uuid.UUID] | None = None,
    ) -> int:
        current = _utc(now, "now")
        statement = select(LearnAnnouncementSource).where(LearnAnnouncementSource.visible.is_(True))
        if course_ids is not None:
            statement = statement.where(LearnAnnouncementSource.course_id.in_(course_ids))
        count = 0
        for row in session.scalars(statement):
            if row.source_id in seen_source_ids:
                continue
            row.visible = False
            row.disappeared_at = current
            LearnRepository._supersede_announcement_facts(
                session,
                announcement_id=row.id,
                source_id=row.source_id,
                source_fingerprint=row.fingerprint,
                result_status="source_removed",
                implication_status="source_removed",
                delivery_status="superseded",
            )
            count += 1
        session.flush()
        return count

    @staticmethod
    def save_announcement_semantics(
        session: Session, item: LearnSemanticResultInput
    ) -> LearnAnnouncementSemanticResult:
        announcement = session.get(LearnAnnouncementSource, item.announcement_id)
        if announcement is None:
            raise ValueError("announcement_id does not exist")
        if announcement.fingerprint != item.source_fingerprint:
            raise ValueError("semantic result fingerprint must match current announcement")
        existing = session.scalar(
            select(LearnAnnouncementSemanticResult).where(
                LearnAnnouncementSemanticResult.announcement_id == item.announcement_id,
                LearnAnnouncementSemanticResult.source_fingerprint == item.source_fingerprint,
                LearnAnnouncementSemanticResult.prompt_version == item.prompt_version,
            )
        )
        if existing is not None:
            return existing
        result = LearnAnnouncementSemanticResult(
            announcement_id=item.announcement_id,
            course_id=item.course_id,
            source_fingerprint=_bounded(item.source_fingerprint, "source_fingerprint", 128),
            summary=_bounded(item.summary, "summary", 1600),
            why_it_matters=_bounded(item.why_it_matters, "why_it_matters", 1600),
            action_items=_json_list(item.action_items),
            evidence_fragments=_citation_ids(item.evidence_fragments),
            source_url=_optional_bounded(item.source_url, "source_url", 2048),
            model_identity=_bounded(item.model_identity, "model_identity", 255),
            prompt_version=_bounded(item.prompt_version, "prompt_version", 128),
            critic_model_identity=_optional_bounded(
                item.critic_model_identity, "critic_model_identity", 255
            ),
            critic_prompt_version=_optional_bounded(
                item.critic_prompt_version, "critic_prompt_version", 128
            ),
            repair_attempted=item.repair_attempted,
            anti_copy_passed=item.anti_copy_passed,
            status=_choice(
                item.status,
                {"valid", "summary_unavailable", "invalid"},
                "semantic status",
            ),
            error_code=_optional_bounded(item.error_code, "error_code", 128),
            interpreted_at=_utc(item.interpreted_at, "interpreted_at"),
        )
        session.add(result)
        session.flush()
        for implication in item.dated_implications:
            session.add(
                LearnDatedImplication(
                    semantic_result_id=result.id,
                    announcement_id=item.announcement_id,
                    course_id=item.course_id,
                    source_fingerprint=result.source_fingerprint,
                    implication_key=_bounded(implication.implication_key, "implication_key", 128),
                    activity_type=_bounded(implication.activity_type, "activity_type", 128),
                    academic_date=implication.academic_date,
                    start_at=_optional_utc(implication.start_at, "start_at"),
                    end_at=_optional_utc(implication.end_at, "end_at"),
                    date_precision=_choice(
                        implication.date_precision,
                        {"date", "datetime"},
                        "date_precision",
                    ),
                    reminder_date=implication.academic_date - timedelta(days=1),
                    evidence_fragments=_citation_ids(implication.evidence_fragments),
                )
            )
        session.flush()
        return result

    @staticmethod
    def announcement_summaries_for_window(
        session: Session,
        *,
        since: datetime,
        until: datetime,
        course_ids: set[uuid.UUID] | None = None,
    ) -> Sequence[LearnAnnouncementSemanticResult]:
        statement = LearnRepository._active_semantic_results().where(
            LearnAnnouncementSource.visible.is_(True),
            LearnAnnouncementSource.effective_at >= _utc(since, "since"),
            LearnAnnouncementSource.effective_at <= _utc(until, "until"),
        )
        if course_ids is not None:
            statement = statement.where(LearnAnnouncementSource.course_id.in_(course_ids))
        return tuple(session.scalars(statement.order_by(LearnAnnouncementSource.effective_at)))

    @staticmethod
    def day_before_implications(
        session: Session,
        *,
        reminder_date: date,
        course_ids: set[uuid.UUID] | None = None,
    ) -> Sequence[LearnDatedImplication]:
        statement = (
            select(LearnDatedImplication)
            .join(
                LearnAnnouncementSemanticResult,
                LearnAnnouncementSemanticResult.id == LearnDatedImplication.semantic_result_id,
            )
            .join(
                LearnAnnouncementSource,
                LearnAnnouncementSource.id == LearnDatedImplication.announcement_id,
            )
            .where(
                LearnDatedImplication.status == "active",
                LearnDatedImplication.reminder_date == reminder_date,
                LearnAnnouncementSource.visible.is_(True),
                LearnAnnouncementSemanticResult.status.in_(_SEMANTIC_ACTIVE_STATUSES),
            )
        )
        if course_ids is not None:
            statement = statement.where(LearnDatedImplication.course_id.in_(course_ids))
        return tuple(session.scalars(statement.order_by(LearnDatedImplication.academic_date)))

    @staticmethod
    def record_delivery(
        session: Session, item: LearnDeliveryInput
    ) -> tuple[LearnNotificationDelivery, bool]:
        values = {
            "message_key": _bounded(item.message_key, "message_key", 512),
            "delivery_kind": _choice(
                item.delivery_kind,
                {"announcement_window", "day_before", "reconnect_alert"},
                "delivery_kind",
            ),
            "occurrence_date": item.occurrence_date,
            "scheduled_for": _optional_utc(item.scheduled_for, "scheduled_for"),
            "channel": _bounded(item.channel, "channel", 64),
            "target": _optional_bounded(item.target, "target", 255),
            "announcement_id": item.announcement_id,
            "dated_implication_id": item.dated_implication_id,
            "source_fingerprint": _optional_bounded(
                item.source_fingerprint, "source_fingerprint", 128
            ),
        }
        existing = session.scalar(
            select(LearnNotificationDelivery).where(
                LearnNotificationDelivery.message_key == values["message_key"]
            )
        )
        if existing is not None:
            return existing, False
        row = LearnNotificationDelivery(**values)
        try:
            with session.begin_nested():
                session.add(row)
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(LearnNotificationDelivery).where(
                    LearnNotificationDelivery.message_key == values["message_key"]
                )
            )
            if existing is None:
                raise
            return existing, False
        return row, True

    @staticmethod
    def mark_delivery_sent(
        session: Session,
        *,
        delivery_id: uuid.UUID,
        sent_at: datetime,
        external_message_id: str | None = None,
    ) -> LearnNotificationDelivery:
        row = session.get(LearnNotificationDelivery, delivery_id)
        if row is None:
            raise ValueError("delivery_id does not exist")
        row.status = "sent"
        row.sent_at = _utc(sent_at, "sent_at")
        row.external_message_id = _optional_bounded(external_message_id, "external_message_id", 255)
        session.flush()
        return row

    @staticmethod
    def create_proposal_link(
        session: Session,
        item: LearnProposalLinkInput,
        *,
        explicit_request: bool = False,
    ) -> tuple[LearnProposalCreateStatus, LearnNotionProposalLink]:
        existing_same_key = session.scalar(
            select(LearnNotionProposalLink).where(
                LearnNotionProposalLink.idempotency_key == item.idempotency_key
            )
        )
        if existing_same_key is not None:
            return "existing", existing_same_key
        blocking = LearnRepository._blocking_proposal_link(
            session,
            source_kind=item.source_kind,
            source_id=item.source_id,
            source_fingerprint=item.source_fingerprint,
            operation=item.operation,
        )
        if blocking is not None:
            if (
                explicit_request
                and blocking.state in {"rejected", "expired"}
                and item.explicit_request_key is not None
            ):
                pass
            elif blocking.state in {"rejected", "expired"}:
                return "requires_explicit_request", blocking
            else:
                return "existing", blocking
        row = LearnNotionProposalLink(
            source_kind=item.source_kind,
            source_id=_bounded(item.source_id, "source_id", 255),
            source_fingerprint=_bounded(item.source_fingerprint, "source_fingerprint", 128),
            scheduled_item_id=item.scheduled_item_id,
            dated_implication_id=item.dated_implication_id,
            operation=item.operation,
            state=item.state,
            idempotency_key=_bounded(item.idempotency_key, "idempotency_key", 512),
            proposal_id=item.proposal_id,
            reserved_calendar_notion_id=_bounded(
                item.reserved_calendar_notion_id,
                "reserved_calendar_notion_id",
                255,
            ),
            target_notion_page_id=_optional_bounded(
                item.target_notion_page_id,
                "target_notion_page_id",
                255,
            ),
            target_expected_title=_optional_bounded(
                item.target_expected_title,
                "target_expected_title",
                500,
            ),
            target_expected_date=item.target_expected_date,
            target_expected_start_at=_optional_utc(
                item.target_expected_start_at,
                "target_expected_start_at",
            ),
            target_expected_last_edited_at=_optional_utc(
                item.target_expected_last_edited_at,
                "target_expected_last_edited_at",
            ),
            learn_context_property_id=_bounded(
                item.learn_context_property_id, "learn_context_property_id", 255
            ),
            learn_context_property_name=_bounded(
                item.learn_context_property_name, "learn_context_property_name", 255
            ),
            explicit_request_key=_optional_bounded(
                item.explicit_request_key,
                "explicit_request_key",
                255,
            ),
            conflict_code=_optional_bounded(item.conflict_code, "conflict_code", 128),
        )
        session.add(row)
        session.flush()
        return "created", row

    @staticmethod
    def set_proposal_link_state(
        session: Session,
        *,
        link_id: uuid.UUID,
        state: Literal["pending", "confirmed", "rejected", "expired", "applied", "superseded"],
    ) -> LearnNotionProposalLink:
        row = session.get(LearnNotionProposalLink, link_id)
        if row is None:
            raise ValueError("proposal link does not exist")
        row.state = _choice(
            state,
            {"pending", "confirmed", "rejected", "expired", "applied", "superseded"},
            "proposal link state",
        )
        session.flush()
        return row

    @staticmethod
    def _active_semantic_results() -> Select[tuple[LearnAnnouncementSemanticResult]]:
        return (
            select(LearnAnnouncementSemanticResult)
            .join(
                LearnAnnouncementSource,
                LearnAnnouncementSource.id == LearnAnnouncementSemanticResult.announcement_id,
            )
            .where(
                LearnAnnouncementSemanticResult.status.in_(_SEMANTIC_ACTIVE_STATUSES),
                LearnAnnouncementSemanticResult.source_fingerprint
                == LearnAnnouncementSource.fingerprint,
            )
        )

    @staticmethod
    def _supersede_announcement_facts(
        session: Session,
        *,
        announcement_id: uuid.UUID,
        source_id: str,
        source_fingerprint: str,
        result_status: Literal["superseded", "source_removed"],
        implication_status: Literal["superseded", "source_removed"],
        delivery_status: Literal["superseded"],
    ) -> None:
        for result in session.scalars(
            select(LearnAnnouncementSemanticResult).where(
                LearnAnnouncementSemanticResult.announcement_id == announcement_id,
                LearnAnnouncementSemanticResult.source_fingerprint == source_fingerprint,
                LearnAnnouncementSemanticResult.status.in_(
                    ("valid", "summary_unavailable", "invalid")
                ),
            )
        ):
            result.status = result_status
        for implication in session.scalars(
            select(LearnDatedImplication).where(
                LearnDatedImplication.announcement_id == announcement_id,
                LearnDatedImplication.source_fingerprint == source_fingerprint,
                LearnDatedImplication.status == "active",
            )
        ):
            implication.status = implication_status
        for delivery in session.scalars(
            select(LearnNotificationDelivery).where(
                LearnNotificationDelivery.announcement_id == announcement_id,
                LearnNotificationDelivery.source_fingerprint == source_fingerprint,
                LearnNotificationDelivery.status.in_(("pending", "failed")),
            )
        ):
            delivery.status = delivery_status
        LearnRepository._supersede_proposal_links(
            session,
            source_kind="announcement_implication",
            source_id=source_id,
            source_fingerprint=source_fingerprint,
        )

    @staticmethod
    def _supersede_proposal_links(
        session: Session,
        *,
        source_kind: Literal["scheduled_item", "announcement_implication"],
        source_id: str,
        source_fingerprint: str,
    ) -> None:
        source_identity = (
            or_(
                LearnNotionProposalLink.source_id == source_id,
                LearnNotionProposalLink.source_id.startswith(f"{source_id}:"),
            )
            if source_kind == "announcement_implication"
            else LearnNotionProposalLink.source_id == source_id
        )
        for link in session.scalars(
            select(LearnNotionProposalLink).where(
                LearnNotionProposalLink.source_kind == source_kind,
                source_identity,
                LearnNotionProposalLink.source_fingerprint == source_fingerprint,
                LearnNotionProposalLink.state.in_(
                    ("pending", "confirmed", "rejected", "expired", "skipped")
                ),
            )
        ):
            link.state = "superseded"

    @staticmethod
    def blocking_proposal_link(
        session: Session,
        *,
        source_kind: str,
        source_id: str,
        source_fingerprint: str,
        operation: str,
    ) -> LearnNotionProposalLink | None:
        link = LearnRepository._blocking_proposal_link(
            session,
            source_kind=source_kind,
            source_id=source_id,
            source_fingerprint=source_fingerprint,
            operation=operation,
        )
        if link is None or link.proposal_id is None:
            return link
        proposal = session.get(AcademicProposedChange, link.proposal_id)
        if proposal is None:
            return link
        state = {
            "pending": "pending",
            "confirmed": "confirmed",
            "applying": "confirmed",
            "rejected": "rejected",
            "applied": "applied",
            "expired": "expired",
            "superseded": "superseded",
        }.get(proposal.state)
        if state is not None and link.state != state:
            link.state = state
            session.flush()
        return None if link.state == "superseded" else link

    @staticmethod
    def _blocking_proposal_link(
        session: Session,
        *,
        source_kind: str,
        source_id: str,
        source_fingerprint: str,
        operation: str,
    ) -> LearnNotionProposalLink | None:
        return session.scalar(
            select(LearnNotionProposalLink)
            .where(
                LearnNotionProposalLink.source_kind == source_kind,
                LearnNotionProposalLink.source_id == source_id,
                LearnNotionProposalLink.source_fingerprint == source_fingerprint,
                LearnNotionProposalLink.operation == operation,
                LearnNotionProposalLink.state.in_(_PROPOSAL_BLOCKING_STATES),
            )
            .order_by(LearnNotionProposalLink.created_at.desc())
        )


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_utc(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    return _utc(value, field)


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


def _choice(value: str, allowed: set[str], field: str) -> str:
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"{field} must be one of: {allowed_values}")
    return value


def _json_list(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in value]


def _citation_ids(value: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Persist citation identities only; source announcement text is runtime-only."""

    citations: list[dict[str, str]] = []
    for item in value:
        raw_id = item.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip() or len(raw_id) > 255:
            raise ValueError("LEARN evidence citations require bounded fragment ids")
        citations.append({"id": raw_id.strip()})
    return citations


def proposal_link_input_from_mapping(value: Mapping[str, Any]) -> LearnProposalLinkInput:
    """Typed helper for integration code that builds links from proposal payloads."""

    return LearnProposalLinkInput(
        source_kind=cast(
            Literal["scheduled_item", "announcement_implication"],
            value["source_kind"],
        ),
        source_id=str(value["source_id"]),
        source_fingerprint=str(value["source_fingerprint"]),
        operation=cast(
            Literal["create_learn_calendar_event", "enrich_learn_calendar_event"],
            value["operation"],
        ),
        idempotency_key=str(value["idempotency_key"]),
        reserved_calendar_notion_id=str(value["reserved_calendar_notion_id"]),
        learn_context_property_id=str(value["learn_context_property_id"]),
        learn_context_property_name=str(value["learn_context_property_name"]),
    )
