"""Read-only LEARN lookups plus confirmation-gated proposal preparation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.agents.academic_planner.contracts import ProposedChange
from app.agents.harness import NativeTool, ToolExecutionError
from app.agents.learn.contracts import (
    LearnAnnouncementSemanticOutcome,
    LearnCourse,
    LearnDatedImplication,
    LearnScheduledItem,
)
from app.agents.learn.morning import scheduled_item_input, semantic_result_input
from app.agents.learn.notion_proposals import (
    LearnNotionProposalBuilder,
    LearnProposalSource,
    learn_proposal_idempotency_key,
)
from app.agents.learn.semantic_interpreter import LearnAnnouncementSemanticInterpreter
from app.connectors.learn_bridge import (
    LearnBridgeConnector,
    LearnBridgeError,
    LearnBridgeHealthStatus,
    LearnBridgeSnapshot,
)
from app.db.academic import AcademicCourseMutationTarget
from app.db.learn import (
    LearnAnnouncementInput,
    LearnCourseInput,
    LearnProposalLinkInput,
    LearnRepository,
)

MAX_LEARN_TOOL_WINDOW_DAYS = 31


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _SearchLearnCoursesArgs(_Args):
    query: str = Field(default="", max_length=300)


class _GetLearnScheduledItemsArgs(_Args):
    course_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    start_date: date
    end_date: date


class _GetLearnAnnouncementsArgs(_Args):
    course_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    since: datetime
    until: datetime

    @field_validator("since", "until")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("LEARN announcement windows require timezone-aware datetimes")
        return value.astimezone(UTC)


class _ProposeLearnCalendarChangeArgs(_Args):
    proposal_source_id: str = Field(min_length=1, max_length=300)
    explicit_retry: bool = False


class LearnToolState:
    """Per-turn LEARN state enforcing search-first access and proposal-only writes."""

    def __init__(
        self,
        *,
        connector: LearnBridgeConnector,
        semantic_interpreter: LearnAnnouncementSemanticInterpreter,
        now: datetime,
        proposal_builder: LearnNotionProposalBuilder | None = None,
        engine: Engine | None = None,
        explicit_request_key: str | None = None,
    ) -> None:
        self._connector = connector
        self._semantic_interpreter = semantic_interpreter
        self._now = now
        self._proposal_builder = proposal_builder
        self._engine = engine
        self._explicit_request_key = explicit_request_key
        self._known_courses: dict[str, LearnCourse] = {}
        self._proposal_sources: dict[str, LearnProposalSource] = {}
        self._proposed_changes: list[ProposedChange] = []
        self._proposed_source_ids: set[str] = set()
        self._pending_links: list[
            tuple[LearnProposalSource, ProposedChange, AcademicCourseMutationTarget, bool]
        ] = []

    def restore_checkpoint(self, value: Mapping[str, object] | None) -> None:
        """Restore only host-validated course capabilities for a resumed turn."""

        if value is None:
            return
        if value.get("version") != "learn-native-tools.v1":
            raise ValueError("LEARN tool checkpoint version is unsupported")
        raw_courses = value.get("known_courses", ())
        if not isinstance(raw_courses, Sequence) or isinstance(raw_courses, str | bytes):
            raise ValueError("LEARN checkpoint courses are invalid")
        course_values = cast(Sequence[object], raw_courses)
        if len(course_values) > 100:
            raise ValueError("LEARN checkpoint courses are invalid")
        self._known_courses = {
            course.org_unit_id: course
            for raw in course_values
            for course in (LearnCourse.model_validate(raw),)
        }

    def export_checkpoint(self) -> dict[str, object]:
        return {
            "version": "learn-native-tools.v1",
            "known_courses": [
                course.model_dump(mode="json")
                for course in sorted(
                    self._known_courses.values(),
                    key=lambda item: (item.code, item.org_unit_id),
                )
            ],
        }

    def tools(self) -> tuple[NativeTool, ...]:
        tools = (
            self._tool(
                "search_learn_courses",
                "Search active LEARN courses by code, name, or term. Call this before using "
                "LEARN course IDs in later LEARN tools.",
                _SearchLearnCoursesArgs,
                self._search_courses,
            ),
            self._tool(
                "get_learn_scheduled_items",
                "Return read-only LEARN scheduled items for course IDs returned by this turn's "
                "search_learn_courses call. The date window is capped at 31 days.",
                _GetLearnScheduledItemsArgs,
                self._get_scheduled_items,
            ),
            self._tool(
                "get_learn_announcements",
                "Return validated LEARN announcement summaries and dated implications only, "
                "never raw title or body text, for searched course IDs in a capped window.",
                _GetLearnAnnouncementsArgs,
                self._get_announcements,
            ),
        )
        if self._proposal_builder is None:
            return tools
        return (
            *tools,
            self._tool(
                "propose_learn_calendar_change",
                "Prepare a confirmation-gated create or LEARN Context enrichment in the exact "
                "Classes + Tutorials + Labs calendar. Use only a proposal_source_id returned "
                "by a LEARN lookup in this turn. This never performs the write.",
                _ProposeLearnCalendarChangeArgs,
                self._propose_calendar_change,
                side_effect_class="proposal_only",
                activity="proposal_drafting",
            ),
        )

    @staticmethod
    def _tool(
        name: str,
        description: str,
        model: type[BaseModel],
        handler: Any,
        *,
        side_effect_class: str = "read_only",
        activity: str = "learn_data",
    ) -> NativeTool:
        return NativeTool(
            schema={
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            },
            handler=handler,
            name=name,
            side_effect_class=side_effect_class,  # type: ignore[arg-type]
            activity=activity,
        )

    async def _search_courses(self, arguments: Mapping[str, object]) -> object:
        args = _SearchLearnCoursesArgs.model_validate(arguments)
        snapshot = await self._snapshot(
            start_at=_start_of_day(self._now.date()),
            end_at=_start_of_day(self._now.date() + timedelta(days=1)),
            include_announcements=False,
            include_scheduled_items=False,
        )
        self._persist_snapshot(snapshot)
        query = args.query.casefold()
        courses = tuple(
            course
            for course in snapshot.courses
            if course.active and _course_matches(course, query)
        )[:20]
        self._known_courses.update((course.org_unit_id, course) for course in courses)
        return {
            "courses": [
                {
                    "course_id": course.org_unit_id,
                    "code": course.code,
                    "name": course.name,
                    "term": course.term,
                    "url": course.url,
                }
                for course in courses
            ],
        }

    async def _get_scheduled_items(self, arguments: Mapping[str, object]) -> object:
        args = _GetLearnScheduledItemsArgs.model_validate(arguments)
        course_ids = self._authorize_course_ids(args.course_ids)
        _validate_date_window(args.start_date, args.end_date)
        snapshot = await self._snapshot(
            start_at=_start_of_day(args.start_date),
            end_at=_end_of_day(args.end_date),
            course_ids=tuple(course_ids),
            include_announcements=False,
            include_scheduled_items=True,
        )
        self._persist_snapshot(snapshot)
        for item in snapshot.scheduled_items:
            if item.course_org_unit_id in course_ids:
                course = self._known_courses.get(item.course_org_unit_id)
                source = _scheduled_proposal_source(
                    item,
                    fallback_url=course.url if course is not None else None,
                )
                if source is not None:
                    self._proposal_sources[source.source_id] = source
        return {
            "scheduled_items": [
                {
                    **_scheduled_item_payload(item),
                    "proposal_source_id": (
                        item.source_id if item.source_id in self._proposal_sources else None
                    ),
                }
                for item in snapshot.scheduled_items
                if item.course_org_unit_id in course_ids
            ],
        }

    async def _get_announcements(self, arguments: Mapping[str, object]) -> object:
        args = _GetLearnAnnouncementsArgs.model_validate(arguments)
        course_ids = self._authorize_course_ids(args.course_ids)
        _validate_datetime_window(args.since, args.until)
        snapshot = await self._snapshot(
            start_at=args.since,
            end_at=args.until,
            course_ids=tuple(course_ids),
            include_announcements=True,
            include_scheduled_items=False,
        )
        outcomes: list[LearnAnnouncementSemanticOutcome] = [
            await self._semantic_interpreter.analyze(announcement)
            for announcement in snapshot.announcements
            if announcement.course_org_unit_id in course_ids
        ]
        self._persist_snapshot(snapshot, outcomes=outcomes)
        payloads: list[dict[str, object]] = []
        for outcome in outcomes:
            payload = outcome.tool_payload()
            result = outcome.result
            if result is not None:
                dated_payloads = cast(list[dict[str, object]], payload["dated_implications"])
                for implication, implication_payload in zip(
                    result.dated_implications,
                    dated_payloads,
                    strict=True,
                ):
                    source = _announcement_proposal_source(outcome, implication)
                    self._proposal_sources[source.source_id] = source
                    implication_payload["proposal_source_id"] = source.source_id
            payloads.append(payload)
        return {"announcements": payloads}

    async def _propose_calendar_change(self, arguments: Mapping[str, object]) -> object:
        args = _ProposeLearnCalendarChangeArgs.model_validate(arguments)
        source = self._proposal_sources.get(args.proposal_source_id)
        if source is None:
            raise ToolExecutionError(
                "Use only a proposal_source_id returned by a LEARN lookup in this turn."
            )
        if args.proposal_source_id in self._proposed_source_ids:
            raise ToolExecutionError("That LEARN source is already included in this proposal.")
        if len(self._proposed_changes) >= 20:
            raise ToolExecutionError("A proposal may contain at most 20 LEARN changes.")
        builder = self._proposal_builder
        if builder is None:
            raise ToolExecutionError("LEARN calendar proposals are not configured.")
        decision = await builder.build(source)
        if decision.change is None:
            raise ToolExecutionError(
                decision.explanation
                or "The LEARN calendar target could not be matched safely."
            )
        target = builder.resolve_target()
        if target is None or target.learn_context_property_id is None:
            raise ToolExecutionError(
                "Exactly one Classes + Tutorials + Labs calendar with LEARN Context is required."
            )
        if self._engine is not None:
            with Session(self._engine) as session, session.begin():
                existing = LearnRepository.blocking_proposal_link(
                    session,
                    source_kind=source.source_kind,
                    source_id=source.source_id,
                    source_fingerprint=source.fingerprint,
                    operation=decision.change.field,
                )
            if existing is not None and not (
                args.explicit_retry
                and existing.state in {"rejected", "expired"}
                and self._explicit_request_key is not None
            ):
                raise ToolExecutionError(
                    "That LEARN source fingerprint already has a proposal decision. "
                    "A rejected or expired proposal may be retried only after the owner "
                    "explicitly asks."
                )
        self._proposed_changes.append(decision.change)
        self._proposed_source_ids.add(args.proposal_source_id)
        self._pending_links.append((source, decision.change, target, args.explicit_retry))
        return {
            "review": "required",
            "routing": decision.status,
            "proposed_change": decision.change.model_dump(mode="json", exclude_none=True),
        }

    def proposed_changes(self) -> tuple[ProposedChange, ...]:
        return tuple(self._proposed_changes)

    def persist_proposal_links(self, proposal_id: UUID) -> None:
        if self._engine is None or not self._pending_links:
            return
        with Session(self._engine) as session, session.begin():
            for source, change, target, explicit_retry in self._pending_links:
                operation = cast(
                    Literal[
                        "create_learn_calendar_event",
                        "enrich_learn_calendar_event",
                    ],
                    change.field,
                )
                expected = change.expected_due_value
                expected_date = (
                    expected.date() if isinstance(expected, datetime) else expected
                )
                idempotency_key = learn_proposal_idempotency_key(
                    source,
                    change.target_page_id,
                )
                if explicit_retry and self._explicit_request_key is not None:
                    request_digest = hashlib.sha256(
                        self._explicit_request_key.encode("utf-8")
                    ).hexdigest()[:20]
                    idempotency_key = f"{idempotency_key}:explicit:{request_digest}"
                LearnRepository.create_proposal_link(
                    session,
                    LearnProposalLinkInput(
                        source_kind=source.source_kind,
                        source_id=source.source_id,
                        source_fingerprint=source.fingerprint,
                        operation=operation,
                        idempotency_key=idempotency_key,
                        proposal_id=proposal_id,
                        reserved_calendar_notion_id=target.data_source_id,
                        target_notion_page_id=change.target_page_id,
                        target_expected_title=change.expected_title,
                        target_expected_date=expected_date,
                        target_expected_start_at=(
                            expected if isinstance(expected, datetime) else None
                        ),
                        target_expected_last_edited_at=change.expected_last_edited_at,
                        learn_context_property_id=cast(
                            str,
                            target.learn_context_property_id,
                        ),
                        learn_context_property_name="LEARN Context",
                        explicit_request_key=(
                            self._explicit_request_key if explicit_retry else None
                        ),
                    ),
                    explicit_request=explicit_retry,
                )

    def _persist_snapshot(
        self,
        snapshot: LearnBridgeSnapshot,
        *,
        outcomes: Sequence[LearnAnnouncementSemanticOutcome] = (),
    ) -> None:
        if self._engine is None:
            return
        current = self._now.astimezone(UTC)
        outcomes_by_source = {outcome.source_id: outcome for outcome in outcomes}
        with Session(self._engine) as session, session.begin():
            course_rows = {
                course.org_unit_id: LearnRepository.upsert_course(
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
                for course in snapshot.courses
            }
            for item in snapshot.scheduled_items:
                course = course_rows.get(item.course_org_unit_id)
                if course is not None:
                    LearnRepository.upsert_scheduled_item(
                        session,
                        scheduled_item_input(item, course.id, current),
                    )
            for announcement in snapshot.announcements:
                course = course_rows.get(announcement.course_org_unit_id)
                outcome = outcomes_by_source.get(announcement.source_id)
                if course is None or outcome is None:
                    continue
                source = LearnRepository.upsert_announcement(
                    session,
                    LearnAnnouncementInput(
                        source_id=announcement.source_id,
                        course_id=course.id,
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
                    semantic_result_input(outcome, source.id, course.id, current),
                )

    async def _snapshot(self, **kwargs: Any) -> LearnBridgeSnapshot:
        try:
            snapshot = await self._connector.snapshot(**kwargs)
        except LearnBridgeError:
            raise ToolExecutionError(
                "LEARN is temporarily unavailable. No Notion data was changed."
            ) from None
        if snapshot.status is LearnBridgeHealthStatus.LOGIN_REQUIRED:
            raise ToolExecutionError(
                "LEARN needs reauthentication. Run scripts/lifeagent_learn_bridge.sh login; "
                "no Notion data was changed."
            )
        if snapshot.status is not LearnBridgeHealthStatus.READY:
            raise ToolExecutionError(
                "The LEARN browser is unavailable. No Notion data was changed."
            )
        return snapshot

    def _authorize_course_ids(self, course_ids: tuple[str, ...]) -> frozenset[str]:
        unknown = sorted(set(course_ids) - set(self._known_courses))
        if unknown:
            raise ToolExecutionError(
                "Search LEARN courses first and use only course IDs returned in this turn."
            )
        return frozenset(course_ids)


def _course_matches(course: LearnCourse, query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(part for part in (course.code, course.name, course.term or "") if part)
    return query in haystack.casefold()


def _validate_date_window(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise ToolExecutionError("LEARN end date must be on or after the start date.")
    if (end_date - start_date).days > MAX_LEARN_TOOL_WINDOW_DAYS:
        raise ToolExecutionError("LEARN lookups are capped at a 31-day window.")


def _validate_datetime_window(since: datetime, until: datetime) -> None:
    if until < since:
        raise ToolExecutionError("LEARN end time must be on or after the start time.")
    if until - since > timedelta(days=MAX_LEARN_TOOL_WINDOW_DAYS):
        raise ToolExecutionError("LEARN lookups are capped at a 31-day window.")


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _end_of_day(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=UTC)


def _scheduled_item_payload(item: LearnScheduledItem) -> dict[str, object]:
    return {
        "source_id": item.source_id,
        "course_id": item.course_org_unit_id,
        "course_code": item.course_code,
        "title": item.title,
        "start_at": item.start_at.isoformat() if item.start_at is not None else None,
        "due_at": item.due_at.isoformat() if item.due_at is not None else None,
        "end_at": item.end_at.isoformat() if item.end_at is not None else None,
        "date_precision": item.date_precision.value,
        "completed": item.completed,
        "url": item.url,
    }


def _scheduled_proposal_source(
    item: LearnScheduledItem,
    *,
    fallback_url: str | None,
) -> LearnProposalSource | None:
    value = item.due_at or item.start_at or item.end_at
    source_url = item.url or fallback_url
    if value is None or source_url is None:
        return None
    end_value = item.end_at if item.due_at is None and item.start_at is not None else None
    return LearnProposalSource(
        source_kind="scheduled_item",
        source_id=item.source_id,
        fingerprint=item.fingerprint,
        course_code=item.course_code,
        summary=item.title,
        proposed_title=item.title,
        source_url=source_url,
        date_value=value,
        end_value=end_value,
        date_precision=item.date_precision.value,
    )


def _announcement_proposal_source(
    outcome: LearnAnnouncementSemanticOutcome,
    implication: LearnDatedImplication,
) -> LearnProposalSource:
    identity = "|".join(
        (
            outcome.source_id,
            implication.activity_type,
            implication.date_value.isoformat(),
            implication.end_value.isoformat() if implication.end_value is not None else "",
        )
    )
    source_id = (
        f"{outcome.source_id[:230]}:{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
    )
    return LearnProposalSource(
        source_kind="announcement_implication",
        source_id=source_id,
        fingerprint=outcome.fingerprint,
        course_code=implication.course_code,
        summary=outcome.safe_summary,
        proposed_title=f"{implication.course_code} {implication.activity_type}",
        source_url=outcome.source_url,
        date_value=implication.date_value,
        end_value=implication.end_value,
        date_precision=implication.date_precision.value,
    )


__all__ = ["LearnToolState"]
