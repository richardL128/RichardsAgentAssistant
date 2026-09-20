"""Fail-open LEARN refresh and safe morning-announcement selection."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.agents.learn.contracts import (
    LearnAnnouncementSemanticOutcome,
    LearnDatedImplication,
    LearnScheduledItem,
)
from app.agents.learn.semantic_interpreter import LearnAnnouncementSemanticInterpreter
from app.connectors.learn_bridge import (
    LearnBridgeConnector,
    LearnBridgeError,
    LearnBridgeHealthStatus,
)
from app.db.learn import (
    LearnAnnouncementInput,
    LearnCourseInput,
    LearnDatedImplicationInput,
    LearnDeliveryInput,
    LearnRepository,
    LearnScheduledItemInput,
    LearnSemanticResultInput,
)
from app.db.models import (
    LearnAnnouncementSemanticResult,
    LearnAnnouncementSource,
    LearnCourse,
)
from app.db.models import (
    LearnDatedImplication as LearnDatedImplicationRow,
)


@dataclass(frozen=True, slots=True)
class LearnMorningAnnouncement:
    announcement_id: str
    source_fingerprint: str
    course_code: str
    summary: str
    why_it_matters: str
    source_url: str
    exact_dates: tuple[str, ...]
    tomorrow: bool
    attachments_present: bool


@dataclass(frozen=True, slots=True)
class LearnMorningDigest:
    announcements: tuple[LearnMorningAnnouncement, ...] = ()
    condition: str | None = None
    reconnect_alert: str | None = None
    refresh_status: str = "disabled"


class LearnMorningService:
    """Refresh LEARN without making it a prerequisite for the Notion briefing."""

    def __init__(
        self,
        *,
        engine: Engine,
        connector: LearnBridgeConnector,
        interpreter: LearnAnnouncementSemanticInterpreter,
        timezone: str = "America/Toronto",
        lookback_hours: int = 72,
    ) -> None:
        self._engine = engine
        self._connector = connector
        self._interpreter = interpreter
        self._zone = ZoneInfo(timezone)
        self._lookback = timedelta(hours=lookback_hours)

    async def refresh_and_select(self, *, occurrence: datetime) -> LearnMorningDigest:
        current = _aware(occurrence).astimezone(UTC)
        local_date = current.astimezone(self._zone).date()
        try:
            health = await self._connector.health()
            if health.status is not LearnBridgeHealthStatus.READY:
                return self._unavailable(health.status, local_date)
            announcement_snapshot = await self._connector.snapshot(
                start_at=current - timedelta(days=31),
                end_at=current,
                include_announcements=True,
                include_scheduled_items=False,
            )
            if announcement_snapshot.status is not LearnBridgeHealthStatus.READY:
                return self._unavailable(announcement_snapshot.status, local_date)
            schedule_snapshot = await self._connector.snapshot(
                start_at=current,
                end_at=current + timedelta(days=31),
                include_announcements=False,
                include_scheduled_items=True,
            )
            if schedule_snapshot.status is not LearnBridgeHealthStatus.READY:
                return self._unavailable(schedule_snapshot.status, local_date)
        except (LearnBridgeError, OSError, TimeoutError, ValueError):
            return LearnMorningDigest(
                condition="I couldn't refresh LEARN; the Notion calendar is unaffected.",
                refresh_status="unavailable",
            )

        courses = {
            course.org_unit_id: course
            for course in (*announcement_snapshot.courses, *schedule_snapshot.courses)
        }
        with Session(self._engine) as session, session.begin():
            course_rows = {
                org_unit_id: LearnRepository.upsert_course(
                    session,
                    LearnCourseInput(
                        org_unit_id=course.org_unit_id,
                        code=course.code,
                        name=course.name,
                        term=course.term,
                        active=course.active,
                        url=course.url,
                        seen_at=current,
                    ),
                )
                for org_unit_id, course in courses.items()
            }
            for item in schedule_snapshot.scheduled_items:
                course = course_rows.get(item.course_org_unit_id)
                if course is not None:
                    LearnRepository.upsert_scheduled_item(
                        session,
                        scheduled_item_input(item, course.id, current),
                    )

        for announcement in announcement_snapshot.announcements:
            course_row = course_rows.get(announcement.course_org_unit_id)
            if course_row is None:
                continue
            outcome = await self._interpreter.analyze(announcement)
            with Session(self._engine) as session, session.begin():
                source = LearnRepository.upsert_announcement(
                    session,
                    LearnAnnouncementInput(
                        source_id=announcement.source_id,
                        course_id=course_row.id,
                        effective_at=announcement.effective_at,
                        fingerprint=announcement.fingerprint,
                        seen_at=current,
                        published_at=announcement.published_at,
                        updated_at=announcement.updated_at,
                        url=announcement.url,
                        has_attachments=announcement.attachments_present,
                    ),
                )
                LearnRepository.save_announcement_semantics(
                    session,
                    semantic_result_input(outcome, source.id, course_row.id, current),
                )

        return self._select(current=current, local_date=local_date)

    def _unavailable(
        self,
        status: LearnBridgeHealthStatus,
        local_date: date,
    ) -> LearnMorningDigest:
        if status is LearnBridgeHealthStatus.LOGIN_REQUIRED:
            message_key = "learn-reconnect:login-required:v1"
            with Session(self._engine) as session, session.begin():
                _row, created = LearnRepository.record_delivery(
                    session,
                    LearnDeliveryInput(
                        message_key=message_key,
                        delivery_kind="reconnect_alert",
                        occurrence_date=local_date,
                    ),
                )
            return LearnMorningDigest(
                condition="LEARN needs reauthentication; the Notion calendar is unaffected.",
                reconnect_alert=(
                    "LEARN needs reauthentication. Run "
                    "`scripts/lifeagent_learn_bridge.sh login`."
                    if created
                    else None
                ),
                refresh_status="login_required",
            )
        return LearnMorningDigest(
            condition="The LEARN browser is unavailable; the Notion calendar is unaffected.",
            refresh_status="browser_unavailable",
        )

    def _select(self, *, current: datetime, local_date: date) -> LearnMorningDigest:
        since = current - self._lookback
        selected: dict[str, LearnMorningAnnouncement] = {}
        with Session(self._engine) as session, session.begin():
            window_results = LearnRepository.announcement_summaries_for_window(
                session,
                since=since,
                until=current,
            )
            reminder_rows = LearnRepository.day_before_implications(
                session,
                reminder_date=local_date,
            )
            reminder_ids = {row.announcement_id for row in reminder_rows}
            announcement_ids = {
                row.announcement_id for row in window_results
            } | reminder_ids
            if not announcement_ids:
                return LearnMorningDigest(refresh_status="ready")
            rows = session.execute(
                select(
                    LearnAnnouncementSemanticResult,
                    LearnAnnouncementSource,
                    LearnCourse,
                )
                .join(
                    LearnAnnouncementSource,
                    LearnAnnouncementSource.id
                    == LearnAnnouncementSemanticResult.announcement_id,
                )
                .join(LearnCourse, LearnCourse.id == LearnAnnouncementSource.course_id)
                .where(
                    LearnAnnouncementSemanticResult.announcement_id.in_(announcement_ids),
                    LearnAnnouncementSemanticResult.source_fingerprint
                    == LearnAnnouncementSource.fingerprint,
                    LearnAnnouncementSemanticResult.status.in_(
                        ("valid", "summary_unavailable")
                    ),
                    LearnAnnouncementSource.visible.is_(True),
                )
            )
            for semantic, announcement, course in rows:
                implication_rows = tuple(
                    session.scalars(
                        select(LearnDatedImplicationRow).where(
                            LearnDatedImplicationRow.semantic_result_id == semantic.id,
                            LearnDatedImplicationRow.status == "active",
                        )
                    )
                )
                exact_dates = tuple(
                    _implication_label(item, self._zone) for item in implication_rows
                )
                selected[str(announcement.id)] = LearnMorningAnnouncement(
                    announcement_id=str(announcement.id),
                    source_fingerprint=announcement.fingerprint,
                    course_code=course.code,
                    summary=semantic.summary,
                    why_it_matters=semantic.why_it_matters,
                    source_url=semantic.source_url or announcement.url or "",
                    exact_dates=exact_dates,
                    tomorrow=announcement.id in reminder_ids,
                    attachments_present=announcement.has_attachments,
                )
                kind = "day_before" if announcement.id in reminder_ids else "announcement_window"
                LearnRepository.record_delivery(
                    session,
                    LearnDeliveryInput(
                        message_key=(
                            f"learn-morning:{local_date.isoformat()}:{announcement.id}:"
                            f"{announcement.fingerprint}"
                        ),
                        delivery_kind=kind,
                        occurrence_date=local_date,
                        scheduled_for=current,
                        announcement_id=announcement.id,
                        source_fingerprint=announcement.fingerprint,
                    ),
                )
        return LearnMorningDigest(
            announcements=tuple(
                sorted(selected.values(), key=lambda item: (item.course_code, item.announcement_id))
            ),
            refresh_status="ready",
        )


def scheduled_item_input(
    item: LearnScheduledItem,
    course_id: object,
    seen_at: datetime,
) -> LearnScheduledItemInput:
    value = item.due_at or item.start_at or item.end_at
    if value is None:
        raise ValueError("LEARN scheduled item has no date")
    academic_date = value.date() if isinstance(value, datetime) else value
    return LearnScheduledItemInput(
        source_id=item.source_id,
        course_id=course_id,  # type: ignore[arg-type]
        title=item.title,
        start_date=academic_date,
        fingerprint=item.fingerprint,
        seen_at=seen_at,
        start_at=item.start_at if isinstance(item.start_at, datetime) else None,
        due_at=item.due_at if isinstance(item.due_at, datetime) else None,
        end_at=item.end_at if isinstance(item.end_at, datetime) else None,
        date_precision=item.date_precision.value,
        completion_state="complete" if item.completed else "incomplete",
        url=item.url,
    )


def semantic_result_input(
    outcome: LearnAnnouncementSemanticOutcome,
    announcement_id: object,
    course_id: object,
    interpreted_at: datetime,
) -> LearnSemanticResultInput:
    result = outcome.result
    implications = () if result is None else result.dated_implications
    return LearnSemanticResultInput(
        announcement_id=announcement_id,  # type: ignore[arg-type]
        course_id=course_id,  # type: ignore[arg-type]
        source_fingerprint=outcome.fingerprint,
        summary=result.summary if result is not None else "summary unavailable",
        why_it_matters=(
            result.why_it_matters
            if result is not None
            else "Open the source link for the original announcement."
        ),
        action_items=(
            [
                {"text": item.text, "evidence_fragment_ids": list(item.evidence_fragment_ids)}
                for item in result.action_items
            ]
            if result is not None
            else []
        ),
        evidence_fragments=(
            [{"id": value} for value in result.evidence_fragment_ids]
            if result is not None
            else []
        ),
        source_url=outcome.source_url,
        model_identity=outcome.model_identity or "unknown-local-model",
        prompt_version=outcome.prompt_version,
        critic_model_identity=outcome.model_identity,
        critic_prompt_version=outcome.critic_version,
        repair_attempted=outcome.status.value == "invalid",
        anti_copy_passed=result is not None,
        status="valid" if result is not None else "summary_unavailable",
        error_code=outcome.error_code,
        interpreted_at=interpreted_at,
        dated_implications=tuple(dated_implication_input(value) for value in implications),
    )


def dated_implication_input(value: LearnDatedImplication) -> LearnDatedImplicationInput:
    academic_date = (
        value.date_value.date()
        if isinstance(value.date_value, datetime)
        else value.date_value
    )
    digest = hashlib.sha256(
        repr(
            (
                value.activity_type,
                value.date_value.isoformat(),
                value.end_value.isoformat() if value.end_value is not None else None,
                value.evidence_fragment_ids,
            )
        ).encode("utf-8")
    ).hexdigest()
    return LearnDatedImplicationInput(
        implication_key=digest,
        activity_type=value.activity_type,
        academic_date=academic_date,
        date_precision=value.date_precision.value,
        start_at=value.date_value if isinstance(value.date_value, datetime) else None,
        end_at=value.end_value if isinstance(value.end_value, datetime) else None,
        evidence_fragments=[{"id": item} for item in value.evidence_fragment_ids],
    )


def _implication_label(item: LearnDatedImplicationRow, zone: ZoneInfo) -> str:
    if item.start_at is not None:
        return _aware(item.start_at).astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")
    return item.academic_date.isoformat()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("LEARN morning timestamps must be timezone-aware")
    return value


__all__ = [
    "LearnMorningAnnouncement",
    "LearnMorningDigest",
    "LearnMorningService",
    "dated_implication_input",
    "scheduled_item_input",
    "semantic_result_input",
]
