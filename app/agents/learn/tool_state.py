"""Read-only LEARN lookups plus confirmation-gated proposal preparation."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.agents.academic_planner.contracts import ProposedChange
from app.agents.harness import (
    ConversationLifecycle,
    HostLifecycleResolution,
    NativeTool,
    PostToolLifecycleContext,
    TerminalGrounding,
    ToolExecutionError,
    ToolResultOversizeError,
)
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
from app.agents.query_contracts import (
    MAX_QUERY_PAGE_SIZE,
    MODEL_TOOL_RESULT_MAX_CHARS,
    CompletenessState,
    CompletionMode,
    CursorCodec,
    FreshnessState,
    NormalizedQueryFilters,
    QueryEnvelope,
    QueryResultKind,
    ResolvedTemporalWindow,
    SourceFreshness,
    TemporalScope,
    model_json_size,
)
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
_LEARN_CURSOR_CODEC = CursorCodec(b"learn-query-cursor-contract-v2")


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _SearchLearnCoursesArgs(_Args):
    query: str = Field(default="", max_length=300)
    limit: int = Field(default=10, ge=1, le=MAX_QUERY_PAGE_SIZE)
    cursor: str | None = Field(default=None, max_length=2_000)


class _GetLearnScheduledItemsArgs(_Args):
    course_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    start_date: date
    end_date: date
    limit: int = Field(default=10, ge=1, le=MAX_QUERY_PAGE_SIZE)
    cursor: str | None = Field(default=None, max_length=2_000)


class _GetLearnAnnouncementsArgs(_Args):
    course_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    since: datetime
    until: datetime
    limit: int = Field(default=10, ge=1, le=MAX_QUERY_PAGE_SIZE)
    cursor: str | None = Field(default=None, max_length=2_000)

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
        timezone: str = "America/Toronto",
    ) -> None:
        self._connector = connector
        self._semantic_interpreter = semantic_interpreter
        self._now = now
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("LEARN tool state now must be timezone-aware")
        self._timezone = ZoneInfo(timezone)
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
        self._query_envelopes: dict[str, QueryEnvelope[dict[str, object]]] = {}

    def restore_checkpoint(self, value: Mapping[str, object] | None) -> None:
        """Restore only host-validated course capabilities for a resumed turn."""

        if value is None:
            return
        if value.get("version") != "learn-native-tools.v2":
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
        raw_envelopes: object = value.get("query_envelopes", ())
        if not isinstance(raw_envelopes, Sequence) or isinstance(raw_envelopes, str | bytes):
            raise ValueError("LEARN checkpoint query envelopes are invalid")
        envelope_values = cast(Sequence[object], raw_envelopes)
        if len(envelope_values) > 20:
            raise ValueError("LEARN checkpoint query envelopes are invalid")
        self._query_envelopes = {
            envelope.query_id: envelope
            for raw in envelope_values
            for envelope in (QueryEnvelope[dict[str, object]].model_validate(raw),)
        }

    def export_checkpoint(self) -> dict[str, object]:
        return {
            "version": "learn-native-tools.v2",
            "known_courses": [
                course.model_dump(mode="json")
                for course in sorted(
                    self._known_courses.values(),
                    key=lambda item: (item.code, item.org_unit_id),
                )
            ],
            "query_envelopes": [
                envelope.model_dump(mode="json")
                for envelope in sorted(
                    self._query_envelopes.values(), key=lambda item: item.query_id
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
        start_at, end_at = _date_window(self._now.astimezone(self._timezone).date(), self._timezone)
        snapshot = await self._snapshot(
            start_at=start_at,
            end_at=end_at,
            include_announcements=False,
            include_scheduled_items=False,
        )
        self._persist_snapshot(snapshot)
        query = args.query.casefold()
        courses = tuple(
            course
            for course in snapshot.courses
            if course.active and _course_matches(course, query)
        )
        items = [
            {
                "stable_id": course.org_unit_id,
                "course_id": course.org_unit_id,
                "code": course.code,
                "name": course.name,
                "term": course.term,
                "url": course.url,
            }
            for course in courses
        ]
        payload = _envelope_payload(
            kind="learn_courses",
            as_of=snapshot.generated_at,
            timezone=str(self._timezone),
            filters=NormalizedQueryFilters(
                temporal=ResolvedTemporalWindow(scope=TemporalScope.ALL),
                completion=CompletionMode.ALL,
                text=args.query,
                roles=("learn",),
                limit=args.limit,
            ),
            freshness=_freshness(snapshot),
            items=items,
            limit=args.limit,
            cursor=args.cursor,
            owner_scope="learn:courses",
        )
        self._record_envelope(payload)
        returned_ids = {
            str(item.get("course_id"))
            for item in cast(Sequence[Mapping[str, object]], payload["items"])
        }
        self._known_courses.update(
            (course.org_unit_id, course) for course in courses if course.org_unit_id in returned_ids
        )
        return payload

    async def _get_scheduled_items(self, arguments: Mapping[str, object]) -> object:
        args = _GetLearnScheduledItemsArgs.model_validate(arguments)
        course_ids = self._authorize_course_ids(args.course_ids)
        _validate_date_window(args.start_date, args.end_date)
        start_at, end_at = _date_window(args.start_date, self._timezone, end_date=args.end_date)
        snapshot = await self._snapshot(
            start_at=start_at,
            end_at=end_at,
            course_ids=course_ids,
            include_announcements=False,
            include_scheduled_items=True,
        )
        self._persist_snapshot(snapshot)
        scheduled_items = tuple(
            item
            for item in snapshot.scheduled_items
            if item.course_org_unit_id in course_ids
            and not item.completed
            and _scheduled_item_in_window(item, start_at=start_at, end_at=end_at)
        )
        proposal_sources: dict[str, LearnProposalSource] = {}
        items: list[dict[str, object]] = []
        for item in scheduled_items:
            course = self._known_courses.get(item.course_org_unit_id)
            source = _scheduled_proposal_source(
                item,
                fallback_url=course.url if course is not None else None,
            )
            if source is not None:
                proposal_sources[source.source_id] = source
            items.append(
                {
                    **_scheduled_item_payload(item),
                    "proposal_source_id": (source.source_id if source is not None else None),
                }
            )
        payload = _envelope_payload(
            kind="learn_scheduled_items",
            as_of=snapshot.generated_at,
            timezone=str(self._timezone),
            filters=NormalizedQueryFilters(
                temporal=_resolved_date_window(args.start_date, args.end_date, self._timezone),
                completion=CompletionMode.INCOMPLETE,
                roles=("learn",),
                source_ids=course_ids,
                limit=args.limit,
            ),
            freshness=_freshness(snapshot),
            items=items,
            limit=args.limit,
            cursor=args.cursor,
            owner_scope="learn:scheduled:" + ",".join(course_ids),
        )
        self._record_envelope(payload)
        for item in cast(Sequence[Mapping[str, object]], payload["items"]):
            source_id = item.get("proposal_source_id")
            if isinstance(source_id, str) and source_id in proposal_sources:
                self._proposal_sources[source_id] = proposal_sources[source_id]
        return payload

    async def _get_announcements(self, arguments: Mapping[str, object]) -> object:
        args = _GetLearnAnnouncementsArgs.model_validate(arguments)
        course_ids = self._authorize_course_ids(args.course_ids)
        _validate_datetime_window(args.since, args.until, timezone=self._timezone)
        start_at, end_at = _datetime_window(args.since, args.until, self._timezone)
        snapshot = await self._snapshot(
            start_at=start_at,
            end_at=end_at,
            course_ids=course_ids,
            include_announcements=True,
            include_scheduled_items=False,
        )
        announcements = tuple(
            announcement
            for announcement in snapshot.announcements
            if announcement.course_org_unit_id in course_ids
            and start_at <= announcement.effective_at.astimezone(self._timezone) < end_at
        )
        outcomes: list[LearnAnnouncementSemanticOutcome] = [
            await self._semantic_interpreter.analyze(announcement) for announcement in announcements
        ]
        self._persist_snapshot(snapshot, outcomes=outcomes)
        items: list[dict[str, object]] = []
        proposal_sources: dict[str, LearnProposalSource] = {}
        for outcome in outcomes:
            payload = outcome.tool_payload()
            payload["source_id"] = outcome.source_id
            payload["stable_id"] = outcome.source_id
            result = outcome.result
            if result is not None:
                dated_payloads = cast(list[dict[str, object]], payload["dated_implications"])
                for implication, implication_payload in zip(
                    result.dated_implications,
                    dated_payloads,
                    strict=True,
                ):
                    source = _announcement_proposal_source(outcome, implication)
                    proposal_sources[source.source_id] = source
                    implication_payload["proposal_source_id"] = source.source_id
            items.append(payload)
        envelope = _envelope_payload(
            kind="learn_announcements",
            as_of=snapshot.generated_at,
            timezone=str(self._timezone),
            filters=NormalizedQueryFilters(
                temporal=_resolved_datetime_window(args.since, args.until, self._timezone),
                completion=CompletionMode.ALL,
                roles=("learn",),
                source_ids=course_ids,
                limit=args.limit,
            ),
            freshness=_freshness(snapshot),
            items=items,
            limit=args.limit,
            cursor=args.cursor,
            owner_scope="learn:announcements:" + ",".join(course_ids),
        )
        self._record_envelope(envelope)
        for item in cast(Sequence[Mapping[str, object]], envelope["items"]):
            implications = item.get("dated_implications")
            if not isinstance(implications, Sequence) or isinstance(implications, str | bytes):
                continue
            for implication in cast(Sequence[object], implications):
                if not isinstance(implication, Mapping):
                    continue
                implication_mapping = cast(Mapping[str, object], implication)
                source_id = implication_mapping.get("proposal_source_id")
                if isinstance(source_id, str) and source_id in proposal_sources:
                    self._proposal_sources[source_id] = proposal_sources[source_id]
        return envelope

    def _record_envelope(self, payload: Mapping[str, object]) -> None:
        envelope = QueryEnvelope[dict[str, object]].model_validate(payload)
        self._query_envelopes[envelope.query_id] = envelope

    @property
    def has_query_results(self) -> bool:
        return bool(self._query_envelopes)

    @property
    def has_prepared_proposal(self) -> bool:
        return bool(self._proposed_changes)

    def query_envelope(self, query_id: str) -> QueryEnvelope[dict[str, object]] | None:
        return self._query_envelopes.get(query_id)

    def validate_grounding(self, grounding: TerminalGrounding) -> str | None:
        envelope = self._query_envelopes.get(grounding.query_id)
        if envelope is None:
            return "grounding query_id was not returned by a current trusted LEARN query"
        if envelope.result_kind is not QueryResultKind.LEARN_CONTENT:
            return "grounding query does not contain LEARN evidence"
        known_ids = {_learn_item_id(item) for item in envelope.items}
        if not set(grounding.item_ids).issubset(known_ids):
            return "grounding item_ids contain an unknown or out-of-scope LEARN item"
        if envelope.has_more and not grounding.acknowledge_incomplete:
            return "grounding must acknowledge that more LEARN items are available"
        return None

    def render_grounding(self, grounding: TerminalGrounding) -> str:
        envelope = self._query_envelopes[grounding.query_id]
        selected = {_learn_item_id(item): item for item in envelope.items}
        items = [selected[item_id] for item_id in grounding.item_ids]
        lines = (
            ["Here are the matching LEARN items:"]
            if items
            else ["I found no matching LEARN items in the requested scope."]
        )
        for item in items:
            title = str(item.get("title") or item.get("name") or item.get("code") or "LEARN item")
            date_label = item.get("due_at") or item.get("start_at") or item.get("effective_at")
            lines.append(f"- {title}" + (f" — {date_label}" if date_label else ""))
        if envelope.has_more:
            lines.append("More matching LEARN items are available; ask me for the next page.")
        return "\n".join(lines)

    def resolve_post_tool_lifecycle(
        self,
        context: PostToolLifecycleContext,
    ) -> HostLifecycleResolution | None:
        """Expose one trusted terminal LEARN lookup or proposal to the root resolver."""

        if context.trigger == "tool_result":
            return None
        if self.has_prepared_proposal:
            return HostLifecycleResolution(
                disposition="complete",
                content="I prepared the LEARN calendar change for review. Please confirm it below.",
            )
        query_ids = _current_turn_query_ids(
            context.messages,
            tool_names={"get_learn_scheduled_items", "get_learn_announcements"},
        )
        envelopes = [
            envelope
            for query_id in query_ids
            if (envelope := self.query_envelope(query_id)) is not None
            and envelope.result_kind is QueryResultKind.LEARN_CONTENT
        ]
        if len(envelopes) > 1:
            return HostLifecycleResolution(
                disposition="awaiting_user",
                content="Which LEARN result set should I use for the answer?",
            )
        if not envelopes:
            return HostLifecycleResolution(disposition="continue_model")
        envelope = envelopes[0]
        grounding = TerminalGrounding(
            query_id=envelope.query_id,
            item_ids=tuple(_learn_item_id(item) for item in envelope.items),
            acknowledge_incomplete=envelope.has_more,
            acknowledge_stale=False,
        )
        return HostLifecycleResolution(
            disposition="complete",
            lifecycle=ConversationLifecycle(
                disposition="completed",
                content="The host completed this answer from trusted LEARN results.",
                grounding=grounding,
            ),
        )

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
                decision.explanation or "The LEARN calendar target could not be matched safely."
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
                expected_date = expected.date() if isinstance(expected, datetime) else expected
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

    def _authorize_course_ids(self, course_ids: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(course_ids) - set(self._known_courses))
        if unknown:
            raise ToolExecutionError(
                "Search LEARN courses first and use only course IDs returned in this turn."
            )
        return tuple(dict.fromkeys(course_ids))


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


def _validate_datetime_window(since: datetime, until: datetime, *, timezone: ZoneInfo) -> None:
    if until < since:
        raise ToolExecutionError("LEARN end time must be on or after the start time.")
    if until - since > timedelta(days=MAX_LEARN_TOOL_WINDOW_DAYS):
        raise ToolExecutionError("LEARN lookups are capped at a 31-day window.")
    start_at, end_at = _datetime_window(since, until, timezone)
    if end_at - start_at > timedelta(days=MAX_LEARN_TOOL_WINDOW_DAYS + 1):
        raise ToolExecutionError("LEARN lookups are capped at a 31-day window.")


def _date_window(
    start_date: date,
    timezone: ZoneInfo,
    *,
    end_date: date | None = None,
) -> tuple[datetime, datetime]:
    end = end_date or start_date
    return (
        datetime.combine(start_date, time.min, tzinfo=timezone),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone),
    )


def _datetime_window(
    since: datetime,
    until: datetime,
    timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    return _date_window(
        since.astimezone(timezone).date(),
        timezone,
        end_date=until.astimezone(timezone).date(),
    )


def _resolved_date_window(
    start_date: date,
    end_date: date,
    timezone: ZoneInfo,
) -> ResolvedTemporalWindow:
    start_at, end_at = _date_window(start_date, timezone, end_date=end_date)
    return ResolvedTemporalWindow(
        scope=TemporalScope.DATE_RANGE,
        start_at=start_at,
        end_at=end_at,
        start_local_date=start_date,
        end_local_date_exclusive=end_date + timedelta(days=1),
    )


def _resolved_datetime_window(
    since: datetime,
    until: datetime,
    timezone: ZoneInfo,
) -> ResolvedTemporalWindow:
    start_at, end_at = _datetime_window(since, until, timezone)
    return ResolvedTemporalWindow(
        scope=TemporalScope.DATE_RANGE,
        start_at=start_at,
        end_at=end_at,
        start_local_date=start_at.date(),
        end_local_date_exclusive=end_at.date(),
    )


def _scheduled_item_in_window(
    item: LearnScheduledItem,
    *,
    start_at: datetime,
    end_at: datetime,
) -> bool:
    value = item.due_at or item.start_at or item.end_at
    if value is None:
        return False
    local_value = _local_datetime(value, start_at)
    return start_at <= local_value < end_at


def _local_datetime(value: datetime | date, window_start: datetime) -> datetime:
    timezone = window_start.tzinfo
    if timezone is None:
        raise ValueError("window_start must be timezone-aware")
    if isinstance(value, datetime):
        return value.astimezone(timezone)
    return datetime.combine(value, time.min, tzinfo=timezone)


def _freshness(snapshot: LearnBridgeSnapshot) -> tuple[SourceFreshness, ...]:
    return (
        SourceFreshness(
            source_id="learn_bridge",
            state=FreshnessState.FRESH_COMPLETE,
            as_of=snapshot.generated_at,
        ),
    )


def _envelope_payload(
    *,
    kind: str,
    as_of: datetime,
    timezone: str,
    filters: NormalizedQueryFilters,
    freshness: tuple[SourceFreshness, ...],
    items: Sequence[Mapping[str, object]],
    limit: int,
    cursor: str | None,
    owner_scope: str,
) -> dict[str, object]:
    all_items = tuple(sorted((dict(item) for item in items), key=_learn_item_id))
    snapshot = as_of.astimezone(UTC).isoformat()
    try:
        cursor_last = (
            _LEARN_CURSOR_CODEC.decode(
                cursor,
                filters=filters,
                owner_scope=owner_scope,
                snapshot=snapshot,
            )
            if cursor is not None
            else None
        )
    except ValueError as exc:
        raise ToolExecutionError("LEARN query cursor is invalid or no longer current.") from exc
    page_source = (
        tuple(item for item in all_items if (_learn_item_id(item),) > cursor_last)
        if cursor_last is not None
        else all_items
    )
    page_size = min(len(page_source), limit)
    while page_size >= 0:
        if page_source and page_size == 0:
            break
        page = page_source[:page_size]
        has_more = len(page_source) > len(page)
        next_cursor = (
            _LEARN_CURSOR_CODEC.encode(
                filters=filters,
                owner_scope=owner_scope,
                snapshot=snapshot,
                last_key=(_learn_item_id(page[-1]),),
            )
            if has_more and page
            else None
        )
        envelope = QueryEnvelope[dict[str, object]](
            query_id=f"{kind}:{uuid.uuid4()}",
            as_of=as_of,
            timezone=timezone,
            result_kind=QueryResultKind.LEARN_CONTENT,
            applied_filters=filters,
            freshness=freshness,
            items=page,
            result_count=len(page),
            has_more=has_more,
            next_cursor=next_cursor,
            completeness=(
                CompletenessState.MORE_AVAILABLE if has_more else CompletenessState.COMPLETE
            ),
        )
        payload = envelope.model_dump(mode="json")
        if (
            model_json_size({"content": payload, "status": "succeeded"})
            <= MODEL_TOOL_RESULT_MAX_CHARS
        ):
            return payload
        page_size -= 1
    raise ToolResultOversizeError("LEARN query result exceeded the safe payload budget")


def _learn_item_id(item: Mapping[str, object]) -> str:
    value = item.get("stable_id") or item.get("course_id") or item.get("source_id")
    return str(value or "")


def _current_turn_query_ids(
    messages: Sequence[object],
    *,
    tool_names: set[str],
) -> tuple[str, ...]:
    query_ids: list[str] = []
    for message in reversed(messages):
        if getattr(message, "type", None) == "human":
            break
        if (
            getattr(message, "type", None) != "tool"
            or getattr(message, "status", None) != "success"
            or str(getattr(message, "name", "")) not in tool_names
        ):
            continue
        try:
            decoded: object = json.loads(str(getattr(message, "content", "")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(decoded, Mapping):
            continue
        payload = cast(Mapping[str, object], decoded)
        content = payload.get("content")
        query_id = (
            cast(Mapping[str, object], content).get("query_id")
            if isinstance(content, Mapping)
            else None
        )
        if isinstance(query_id, str) and query_id and query_id not in query_ids:
            query_ids.append(query_id)
    return tuple(reversed(query_ids))


def _scheduled_item_payload(item: LearnScheduledItem) -> dict[str, object]:
    return {
        "stable_id": item.source_id,
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
    source_id = f"{outcome.source_id[:230]}:{hashlib.sha256(identity.encode()).hexdigest()[:20]}"
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
