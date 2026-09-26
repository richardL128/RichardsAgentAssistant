"""Career tools exposed only inside the authorized planner-channel harness."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.agents.action_items import DateOnlyValue, DateTimeValue, TemporalValue
from app.agents.harness import (
    ConversationLifecycle,
    HostLifecycleResolution,
    NativeTool,
    PostToolLifecycleContext,
    TerminalGrounding,
    ToolExecutionError,
    ToolSideEffectClass,
)
from app.agents.job_interviews.contracts import (
    ApplicationInterpretation,
    CareerApplicationSnapshot,
    CareerClarificationRequest,
    ClarificationKind,
    InterviewEventSnapshot,
)
from app.agents.job_interviews.matching import (
    identify_posting_url,
    interpret_application_row,
    match_interview_application,
)
from app.agents.job_interviews.notion_mutations import (
    propose_interview_date_write,
    propose_preparation_plan_write,
)
from app.agents.job_interviews.preparation import maintain_preparation_plan
from app.agents.job_interviews.research import JobInterviewResearchInput, research_job_interview
from app.agents.query_contracts import (
    MAX_QUERY_PAGE_SIZE,
    CompletenessState,
    CompletionMode,
    CursorCodec,
    FreshnessState,
    NormalizedQueryFilters,
    QueryEnvelope,
    QueryResultKind,
    SourceFreshness,
    TemporalQuery,
    TemporalScope,
    resolve_temporal_window,
)
from app.connectors.job_research import (
    CompanyResearchClient,
    UnconfiguredCompanyResearchSearchProvider,
)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _SearchInterviewsArgs(_Args):
    query: str = Field(default="", max_length=300)
    limit: int = Field(default=10, ge=1, le=MAX_QUERY_PAGE_SIZE)
    cursor: str | None = Field(default=None, max_length=2_000)


class _SearchJobsContextArgs(_Args):
    query: str = Field(default="", max_length=300)
    limit: int = Field(default=10, ge=1, le=MAX_QUERY_PAGE_SIZE)
    cursor: str | None = Field(default=None, max_length=2_000)


class _PrepareInterviewArgs(_Args):
    interview_page_id: str = Field(min_length=1, max_length=255)
    refresh_research: bool = False


class _ProposeInterviewDateArgs(_Args):
    interview_page_id: str = Field(min_length=1, max_length=255)
    date_start: str = Field(min_length=10, max_length=100)


class _ProposePlanSaveArgs(_Args):
    interview_page_id: str = Field(min_length=1, max_length=255)


_CAREER_TOOL_ACTIVITY = {
    "search_jobs_context": "interview_data",
    "search_job_interviews": "interview_data",
    "prepare_job_interview": "interview_preparation",
    "propose_interview_date": "proposal_drafting",
    "propose_interview_plan_save": "proposal_drafting",
}
_CAREER_TOOL_SIDE_EFFECT_CLASS: dict[str, ToolSideEffectClass] = {
    "search_jobs_context": "read_only",
    "search_job_interviews": "read_only",
    "prepare_job_interview": "durable_local_write",
    "propose_interview_date": "proposal_only",
    "propose_interview_plan_save": "proposal_only",
}
_CAREER_CURSOR_CODEC = CursorCodec(b"career-query-cursor-v1")
_CAREER_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "an",
        "and",
        "any",
        "application",
        "applications",
        "are",
        "coming",
        "date",
        "dates",
        "for",
        "interview",
        "interviews",
        "is",
        "job",
        "jobs",
        "me",
        "my",
        "of",
        "on",
        "please",
        "process",
        "role",
        "roles",
        "status",
        "statuses",
        "the",
        "up",
        "upcoming",
        "what",
        "when",
        "with",
    }
)


class CareerAgentToolState:
    """Per-message career state enforcing search-first and source-scoped tools."""

    def __init__(
        self,
        *,
        store: Any,
        syncer: Any,
        gateway: Any,
        engine: Any | None = None,
        requester: str = "owner",
        external_event_id: str = "interactive",
        now: datetime,
        timezone: str = "America/Toronto",
        sync_timeout_seconds: float = 30.0,
        research_timeout_seconds: float = 10.0,
        research_max_redirects: int = 3,
        research_max_response_bytes: int = 1_048_576,
        research_max_pages: int = 5,
        research_max_search_results: int = 5,
    ) -> None:
        self._store = store
        self._syncer = syncer
        self._gateway = gateway
        self._engine = engine
        self._requester = requester
        self._external_event_id = external_event_id
        self._now = now
        self._timezone = timezone
        self._sync_timeout_seconds = sync_timeout_seconds
        self._research_timeout_seconds = research_timeout_seconds
        self._research_max_redirects = research_max_redirects
        self._research_max_response_bytes = research_max_response_bytes
        self._research_max_pages = research_max_pages
        self._research_max_search_results = research_max_search_results
        self._sync_attempted = False
        self._sync_warning: dict[str, object] | None = None
        self._known_interviews: dict[str, InterviewEventSnapshot] = {}
        self._query_envelopes: dict[str, QueryEnvelope[dict[str, object]]] = {}
        self._proposal_prepared = False

    def restore_checkpoint(self, value: Mapping[str, object] | None) -> None:
        """Restore verified interview capabilities from host-only state."""

        if value is None:
            return
        if value.get("version") != "career-native-tools.v2":
            raise ValueError("career tool checkpoint version is unsupported")
        raw_interviews: object = value.get("known_interviews", ())
        if not isinstance(raw_interviews, Sequence) or isinstance(raw_interviews, str | bytes):
            raise ValueError("career tool checkpoint interviews are invalid")
        interview_values = cast(Sequence[object], raw_interviews)
        if len(interview_values) > 100:
            raise ValueError("career tool checkpoint interviews are invalid")
        self._known_interviews = {
            item.interview_page_id: item
            for raw in interview_values
            for item in (InterviewEventSnapshot.model_validate(raw),)
        }
        raw_envelopes: object = value.get("query_envelopes", ())
        if not isinstance(raw_envelopes, Sequence) or isinstance(raw_envelopes, str | bytes):
            raise ValueError("career checkpoint query envelopes are invalid")
        envelope_values = cast(Sequence[object], raw_envelopes)
        if len(envelope_values) > 20:
            raise ValueError("career checkpoint query envelopes are invalid")
        self._query_envelopes = {
            envelope.query_id: envelope
            for raw in envelope_values
            for envelope in (QueryEnvelope[dict[str, object]].model_validate(raw),)
        }
        raw_proposal_prepared = value.get("proposal_prepared", False)
        if not isinstance(raw_proposal_prepared, bool):
            raise ValueError("career checkpoint proposal state is invalid")
        self._proposal_prepared = raw_proposal_prepared

    def export_checkpoint(self) -> dict[str, object]:
        """Return the bounded host-only state needed to validate resumed tools."""

        return {
            "version": "career-native-tools.v2",
            "known_interviews": [
                item.model_dump(mode="json")
                for item in sorted(
                    self._known_interviews.values(),
                    key=lambda item: item.interview_page_id,
                )
            ],
            "query_envelopes": [
                envelope.model_dump(mode="json")
                for envelope in sorted(
                    getattr(self, "_query_envelopes", {}).values(),
                    key=lambda item: item.query_id,
                )
            ],
            "proposal_prepared": bool(getattr(self, "_proposal_prepared", False)),
        }

    def tools(self) -> tuple[NativeTool, ...]:
        return (
            self._tool(
                "search_jobs_context",
                "Search the owner's typed job applications and upcoming interview calendar "
                "events together. Use first for Jobs/career questions about dates, interviews, "
                "applications, companies, roles, or statuses.",
                _SearchJobsContextArgs,
                self._search_jobs_context,
            ),
            self._tool(
                "search_job_interviews",
                "Search the owner's synchronized upcoming interview rounds. Use before selecting "
                "an interview for preparation.",
                _SearchInterviewsArgs,
                self._search_interviews,
            ),
            self._tool(
                "prepare_job_interview",
                "Build or retrieve one grounded, maintained preparation plan for an interview ID "
                "returned by search_job_interviews. Research is constrained to its posting URL.",
                _PrepareInterviewArgs,
                self._prepare_interview,
            ),
            self._tool(
                "propose_interview_date",
                "Prepare an exact, confirmation-gated Date-only Notion change for an interview "
                "returned by search_job_interviews. This never performs the write.",
                _ProposeInterviewDateArgs,
                self._propose_interview_date,
            ),
            self._tool(
                "propose_interview_plan_save",
                "Prepare an exact, confirmation-gated append of the current maintained plan to "
                "the interview's LifeAgent-owned Notion child page. This never writes directly.",
                _ProposePlanSaveArgs,
                self._propose_plan_save,
            ),
        )

    @staticmethod
    def _tool(name: str, description: str, model: type[BaseModel], handler: Any) -> NativeTool:
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
            side_effect_class=_CAREER_TOOL_SIDE_EFFECT_CLASS[name],
            activity=_CAREER_TOOL_ACTIVITY[name],
        )

    async def _ensure_synced(self, *, cached_available: bool = False) -> None:
        if self._sync_attempted:
            return
        self._sync_attempted = True
        try:
            result = await asyncio.wait_for(
                self._syncer.sync(now=self._now),
                timeout=self._sync_timeout_seconds,
            )
        except TimeoutError:
            self._sync_warning = {
                "status": "cached_fallback",
                "sync_status": "timeout",
                "message": "Jobs sync timed out; returned cached career data.",
            }
            if cached_available:
                return
            raise ToolExecutionError(
                "Jobs sync timed out and no cached Jobs/interview data is available."
            ) from None
        except Exception:
            self._sync_warning = {
                "status": "cached_fallback",
                "sync_status": "failed",
                "message": "Jobs sync failed; returned cached career data.",
            }
            if cached_available:
                return
            raise ToolExecutionError(
                "Jobs sync failed and no cached Jobs/interview data is available."
            ) from None
        status = str(getattr(result, "status", ""))
        if status == "partial":
            codes = tuple(str(item) for item in getattr(result, "diagnostic_codes", ()))[:5]
            self._sync_warning = {
                "status": "fresh_partial",
                "sync_status": status,
                "diagnostic_codes": list(codes),
                "message": "Jobs sync completed partially; returned the available career data.",
            }
        if status not in {"succeeded", "partial"}:
            codes = tuple(str(item) for item in getattr(result, "diagnostic_codes", ()))[:5]
            self._sync_warning = {
                "status": "cached_fallback",
                "sync_status": status or "unknown",
                "diagnostic_codes": list(codes),
                "message": "Jobs sync needs attention; returned cached career data.",
            }
            if cached_available:
                return
            suffix = f" Setup codes: {', '.join(codes)}." if codes else ""
            raise ToolExecutionError("Jobs/Interviews setup needs attention." + suffix)

    async def _search_jobs_context(self, arguments: Mapping[str, object]) -> object:
        args = _SearchJobsContextArgs.model_validate(arguments)
        cached_interviews = self._load_interview_context()
        cached_applications = self._load_application_context()
        await self._ensure_synced(
            cached_available=bool(cached_interviews or cached_applications)
        )
        interviews = self._load_interview_context()
        applications = self._load_application_context()
        terms = _career_query_terms(args.query)
        filtered_interviews = tuple(item for item in interviews if _interview_matches(item, terms))
        filtered_applications = _application_payloads(applications, terms)
        items_with_keys: list[
            tuple[tuple[str, ...], dict[str, object], InterviewEventSnapshot | None]
        ]
        items_with_keys = [
            (
                _interview_sort_key(item),
                {"kind": "interview", **dict(self._interview_payload(item))},
                item,
            )
            for item in filtered_interviews
        ]
        items_with_keys.extend(
            (
                _application_sort_key(item),
                item,
                None,
            )
            for item in filtered_applications
        )
        envelope = self._query_envelope(
            tool_name="search_jobs_context",
            query=args.query,
            limit=args.limit,
            cursor=args.cursor,
            source_id="career_jobs_context",
            items_with_keys=tuple(sorted(items_with_keys, key=lambda item: item[0])),
        )
        self._known_interviews.update(
            (item.interview_page_id, item)
            for _key, _payload, item in items_with_keys
            if item is not None
            and any(
                returned.get("kind") == "interview"
                and returned.get("interview_page_id") == item.interview_page_id
                for returned in envelope.items
            )
        )
        self._query_envelopes[envelope.query_id] = envelope
        return envelope.model_dump(mode="json")

    async def _search_interviews(self, arguments: Mapping[str, object]) -> object:
        args = _SearchInterviewsArgs.model_validate(arguments)
        cached = tuple(self._store.search_interviews(args.query, now=self._now))
        await self._ensure_synced(cached_available=bool(cached))
        results = tuple(self._store.search_interviews(args.query, now=self._now))
        items_with_keys = tuple(
            (
                _interview_sort_key(item),
                {"kind": "interview", **dict(self._interview_payload(item))},
                item,
            )
            for item in sorted(results, key=_interview_sort_key)
        )
        envelope = self._query_envelope(
            tool_name="search_job_interviews",
            query=args.query,
            limit=args.limit,
            cursor=args.cursor,
            source_id="career_interviews",
            items_with_keys=items_with_keys,
        )
        self._known_interviews.update(
            (item.interview_page_id, item)
            for _key, _payload, item in items_with_keys
            if any(
                returned.get("interview_page_id") == item.interview_page_id
                for returned in envelope.items
            )
        )
        self._query_envelopes[envelope.query_id] = envelope
        return envelope.model_dump(mode="json")

    async def _prepare_interview(self, arguments: Mapping[str, object]) -> object:
        args = _PrepareInterviewArgs.model_validate(arguments)
        interview = self._known_interviews.get(args.interview_page_id)
        if interview is None:
            raise ToolExecutionError(
                "interview_page_id must come from search_jobs_context or search_job_interviews "
                "in this turn"
            )
        existing = self._store.get_current_plan(interview.interview_page_id)
        if existing is not None and not args.refresh_research:
            return {
                "status": "current",
                "interview": interview.title,
                "plan_revision": existing.revision,
                "summary": existing.summary,
                "next_actions": list(existing.next_actions),
                "source_ids": list(existing.evidence),
                "notion_save_question": None,
            }

        row_snapshots = tuple(self._store.application_row_snapshots())[:50]
        if not row_snapshots:
            return self._clarify(
                interview,
                ClarificationKind.APPLICATION_MATCH,
                f'Which application in your Jobs table belongs to "{interview.title}"?',
                partial_state={"phase": "matching"},
            )
        row_by_id = {row.row_block_id: row for row in row_snapshots}
        link = self._store.get_interview_link(interview.interview_page_id)
        linked_row_id = (
            str(link["row_block_id"])
            if link and link.get("state") == "matched" and link.get("row_block_id")
            else None
        )
        linked_row = row_by_id.get(linked_row_id or "")
        link_is_compatible = bool(
            link
            and linked_row is not None
            and link.get("interview_content_fingerprint") == interview.content_fingerprint
            and link.get("application_content_fingerprint") == linked_row.content_fingerprint
        )
        row_id = linked_row_id if link_is_compatible else None
        if row_id is None:
            matched = await match_interview_application(
                self._gateway,
                interview,
                row_snapshots,
                resolved_at=self._now,
            )
            self._store.save_interview_link(matched)
            if matched.row_block_id is None:
                return self._clarify(
                    interview,
                    ClarificationKind.APPLICATION_MATCH,
                    f'Which Jobs-table application matches "{interview.title}"?',
                    partial_state={"phase": "matching", "reason": matched.rationale},
                )
            row_id = matched.row_block_id
        row = row_by_id[row_id]
        typed_rows = getattr(self._store, "typed_application_rows", None)
        raw_rows = typed_rows() if callable(typed_rows) else ()
        stored_rows: dict[str, Mapping[str, Any]] = {
            str(item["row_block_id"]): item for item in raw_rows
        }
        stored_row = stored_rows.get(row_id, {})
        stored_interpretation_value = stored_row.get("interpretation")
        if isinstance(stored_interpretation_value, Mapping):
            stored_interpretation = cast(Mapping[str, object], stored_interpretation_value)
            interpretation = ApplicationInterpretation(
                row_block_id=row_id,
                company_name=_optional_text(stored_interpretation.get("company_name")),
                role_title=_optional_text(stored_interpretation.get("role_title")),
                status=_optional_text(stored_interpretation.get("status")),
                confidence=_confidence(stored_interpretation.get("confidence")),
                evidence=(),
                model_version=_optional_text(stored_interpretation.get("model_version")),
                interpreted_at=self._now,
            )
        else:
            interpretation = await interpret_application_row(
                self._gateway,
                row,
                model_version=str(getattr(self._gateway, "model_identity", "qwen")),
                interpreted_at=self._now,
            )
            self._store.save_application_interpretation(interpretation)
        if not interpretation.company_name or not interpretation.role_title:
            return self._clarify(
                interview,
                ClarificationKind.PREPARATION_CONTEXT,
                f'What company and role is "{interview.title}" for?',
                partial_state={"phase": "interpreting_application", "row_id": row_id},
            )
        candidates = tuple({item.url: item for item in interview.url_candidates}.values())
        if not candidates:
            return self._clarify(
                interview,
                ClarificationKind.POSTING_URL,
                f'Please paste the job posting for "{interview.title}".',
                partial_state={"phase": "posting", "candidate_count": 0},
            )
        posting = candidates[0]
        if len(candidates) > 1:
            posting, reason = await identify_posting_url(
                self._gateway,
                interview=interview,
                company=interpretation.company_name,
                role=interpretation.role_title,
                candidates=candidates,
            )
            if posting is None:
                return self._clarify(
                    interview,
                    ClarificationKind.POSTING_URL,
                    f'Which URL in "{interview.title}" is the job posting?',
                    partial_state={
                        "phase": "posting",
                        "candidate_count": len(candidates),
                        "reason": reason,
                    },
                )

        async with httpx.AsyncClient() as client:
            researcher = CompanyResearchClient(
                client=client,
                search_provider=UnconfiguredCompanyResearchSearchProvider(),
                timeout_seconds=self._research_timeout_seconds,
                max_redirects=self._research_max_redirects,
                max_response_bytes=self._research_max_response_bytes,
                max_pages=self._research_max_pages,
                max_search_results=self._research_max_search_results,
            )
            research = await research_job_interview(
                JobInterviewResearchInput(
                    posting_url=posting.url,
                    company=interpretation.company_name,
                    role=interpretation.role_title,
                    interview_source_id=interview.interview_page_id,
                    application_source_id=row_id,
                    intents=("company_overview", "role_requirements"),
                ),
                researcher=researcher,
            )
        if not research.sources:
            failure = (
                research.failures[0].code.value if research.failures else "posting_unavailable"
            )
            return self._clarify(
                interview,
                ClarificationKind.POSTING_URL,
                f'I could not access the posting for "{interview.title}". '
                "Please paste or attach it.",
                partial_state={"phase": "research", "failure_code": failure},
            )
        source_ids = (
            f"notion:interview:{interview.interview_page_id}",
            f"notion:application:{row_id}",
            *(source.source_id for source in research.sources),
        )
        current_local_date = self._now.astimezone(ZoneInfo(self._timezone)).date()
        days_until = max(0, (interview.local_date - current_local_date).days)
        outcome = await maintain_preparation_plan(
            self._gateway,
            store=self._store,
            interview_id=interview.interview_page_id,
            interview_title=interview.title,
            days_until=days_until,
            application_row_id=row_id,
            application_facts={
                "company": interpretation.company_name,
                "role": interpretation.role_title,
                "status": interpretation.status,
            },
            research_excerpts=tuple(
                {
                    "source_id": source.source_id,
                    "url": source.canonical_url,
                    "source_class": source.source_class.value,
                    "retrieved_at": source.retrieved_at.isoformat(),
                    "excerpt": source.excerpt,
                    "truncated": source.excerpt_truncated,
                }
                for source in research.sources
            ),
            source_ids=source_ids,
            research_fingerprint=research.research_fingerprint,
            now=self._now,
        )
        if outcome.status == "needs_clarification":
            return self._clarify(
                interview,
                ClarificationKind.PREPARATION_CONTEXT,
                outcome.clarification_question
                or f'What interview format should I prepare for in "{interview.title}"?',
                partial_state={"phase": "preparation"},
            )
        plan = outcome.plan
        return {
            "status": outcome.status,
            "interview": interview.title,
            "plan_revision": plan.revision if plan else None,
            "summary": plan.summary if plan else None,
            "next_actions": list(plan.next_actions) if plan else [],
            "sources": [source.canonical_url for source in research.sources],
            "research_limitations": [failure.code.value for failure in research.failures],
            "notion_save_question": (
                f'Would you like me to save this plan to the "{interview.title}" Notion page?'
                if outcome.ask_to_save_to_notion
                else None
            ),
        }

    async def _propose_interview_date(self, arguments: Mapping[str, object]) -> object:
        args = _ProposeInterviewDateArgs.model_validate(arguments)
        interview = self._selected(args.interview_page_id)
        if self._engine is None:
            raise ToolExecutionError("Career Notion proposals are not configured")
        try:
            parsed = datetime.fromisoformat(args.date_start.replace("Z", "+00:00"))
        except ValueError:
            raise ToolExecutionError(
                "The proposed interview Date is not a valid ISO date/time"
            ) from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(self._timezone))
        proposal = propose_interview_date_write(
            engine=self._engine,
            interview_page_id=interview.interview_page_id,
            proposed_date=parsed,
            requester=self._requester,
            idempotency_key=f"discord:{self._external_event_id}:interview-date",
            now=self._now,
        )
        self._proposal_prepared = True
        return {"review": "required", **proposal}

    async def _propose_plan_save(self, arguments: Mapping[str, object]) -> object:
        args = _ProposePlanSaveArgs.model_validate(arguments)
        interview = self._selected(args.interview_page_id)
        if self._engine is None:
            raise ToolExecutionError("Career Notion proposals are not configured")
        proposal = propose_preparation_plan_write(
            engine=self._engine,
            interview_page_id=interview.interview_page_id,
            requester=self._requester,
            idempotency_key=f"discord:{self._external_event_id}:preparation-plan",
            now=self._now,
        )
        self._proposal_prepared = True
        return {"review": "required", **proposal}

    @property
    def has_query_results(self) -> bool:
        return bool(self._query_envelopes)

    @property
    def has_prepared_proposal(self) -> bool:
        return self._proposal_prepared

    def query_envelope(self, query_id: str) -> QueryEnvelope[dict[str, object]] | None:
        return self._query_envelopes.get(query_id)

    def validate_grounding(self, grounding: TerminalGrounding) -> str | None:
        envelope = self._query_envelopes.get(grounding.query_id)
        if envelope is None:
            return "grounding query_id was not returned by a current trusted career query"
        if envelope.result_kind is not QueryResultKind.JOBS:
            return "grounding query does not contain career evidence"
        known_ids = {_career_item_id(item) for item in envelope.items}
        if not set(grounding.item_ids).issubset(known_ids):
            return "grounding item_ids contain an unknown or out-of-scope career item"
        if envelope.has_more and not grounding.acknowledge_incomplete:
            return "grounding must acknowledge that more career items are available"
        if (
            envelope.completeness is CompletenessState.CACHED_STALE
            and not grounding.acknowledge_stale
        ):
            return "grounding must acknowledge stale cached career data"
        return None

    def render_grounding(self, grounding: TerminalGrounding) -> str:
        envelope = self._query_envelopes[grounding.query_id]
        selected = {_career_item_id(item): item for item in envelope.items}
        items = [selected[item_id] for item_id in grounding.item_ids]
        lines = (
            ["Here are the matching career items:"]
            if items
            else ["I found no matching career items in the requested scope."]
        )
        for item in items:
            if item.get("kind") == "interview":
                date_label = str(item.get("date") or "date unavailable")
                if item.get("time"):
                    date_label += f" at {item['time']}"
                lines.append(f"- {item.get('title', 'Interview')} — {date_label}")
            else:
                company = str(item.get("company_name") or "Unknown company")
                role = str(item.get("role_title") or "Unknown role")
                status = str(item.get("pipeline_status") or "status unavailable")
                next_action = item.get("next_action")
                summary = f"{company} — {role} [{status}]"
                if next_action:
                    summary += f"; next: {next_action}"
                lines.append(f"- {summary}")
        if envelope.has_more:
            lines.append("More matching career items are available; ask me for the next page.")
        if envelope.completeness is CompletenessState.CACHED_STALE:
            lines.append("Jobs sync was unavailable, so these career results are cached and stale.")
        return "\n".join(lines)

    def resolve_post_tool_lifecycle(
        self,
        context: PostToolLifecycleContext,
    ) -> HostLifecycleResolution | None:
        """Expose one trusted career query or proposal candidate to the root resolver."""

        if context.trigger == "tool_result":
            return None
        if self.has_prepared_proposal:
            return HostLifecycleResolution(
                disposition="complete",
                content="I prepared the interview change for review. Please confirm it below.",
            )
        query_ids = _current_turn_query_ids(
            context.messages,
            tool_names={"search_jobs_context", "search_job_interviews"},
        )
        envelopes = [
            envelope
            for query_id in query_ids
            if (envelope := self.query_envelope(query_id)) is not None
            and envelope.result_kind is QueryResultKind.JOBS
        ]
        if len(envelopes) > 1:
            return HostLifecycleResolution(
                disposition="awaiting_user",
                content="Which career result set should I use for the answer?",
            )
        if not envelopes:
            return HostLifecycleResolution(disposition="continue_model")
        envelope = envelopes[0]
        grounding = TerminalGrounding(
            query_id=envelope.query_id,
            item_ids=tuple(_career_item_id(item) for item in envelope.items),
            acknowledge_incomplete=envelope.has_more,
            acknowledge_stale=envelope.completeness is CompletenessState.CACHED_STALE,
        )
        return HostLifecycleResolution(
            disposition="complete",
            lifecycle=ConversationLifecycle(
                disposition="completed",
                content="The host completed this answer from trusted career results.",
                grounding=grounding,
            ),
        )

    def _selected(self, interview_page_id: str) -> InterviewEventSnapshot:
        interview = self._known_interviews.get(interview_page_id)
        if interview is None:
            raise ToolExecutionError(
                "interview_page_id must come from search_jobs_context or search_job_interviews "
                "in this turn"
            )
        return interview

    def _interview_payload(self, item: InterviewEventSnapshot) -> Mapping[str, object]:
        temporal = _interview_temporal(item)
        return {
            "stable_id": item.interview_page_id,
            "interview_page_id": item.interview_page_id,
            "title": item.title,
            "date": _temporal_date_label(temporal, item.timezone),
            "time": _temporal_time_label(temporal, item.timezone),
            "temporal": temporal.model_dump(mode="json"),
            "timezone": item.timezone,
            "is_all_day": item.is_all_day,
            "application_id": item.application_id,
            "stage": item.stage,
            "interview_status": item.interview_status,
            "preparation_status": item.preparation_status,
            "tags": list(item.tags),
            "calendar_semantic_overview": item.calendar_semantic_overview,
            "calendar_semantic_description": item.calendar_semantic_description,
            "plan_available": self._store.get_current_plan(item.interview_page_id) is not None,
        }

    def _load_interview_context(self) -> tuple[InterviewEventSnapshot, ...]:
        loader = getattr(self._store, "load_upcoming_interviews", None)
        if callable(loader):
            loaded = loader(now=self._now)
            if isinstance(loaded, Sequence):
                return tuple(cast(Sequence[InterviewEventSnapshot], loaded))
            return ()
        return tuple(self._store.search_interviews("", now=self._now))

    def _load_application_context(self) -> tuple[CareerApplicationSnapshot, ...]:
        loader = getattr(self._store, "typed_application_snapshots", None)
        if callable(loader):
            loaded = loader()
            if isinstance(loaded, Sequence):
                return tuple(
                    item
                    if isinstance(item, CareerApplicationSnapshot)
                    else CareerApplicationSnapshot.model_validate(item)
                    for item in cast(Sequence[object], loaded)
                )
            return ()
        return ()

    def _query_envelope(
        self,
        *,
        tool_name: str,
        query: str,
        limit: int,
        cursor: str | None,
        source_id: str,
        items_with_keys: tuple[
            tuple[tuple[str, ...], dict[str, object], InterviewEventSnapshot | None], ...
        ],
    ) -> QueryEnvelope[dict[str, object]]:
        filters = _career_filters(query=query, limit=limit, now=self._now, timezone=self._timezone)
        snapshot = self._snapshot_identity()
        owner_scope = f"career:{self._requester}:{tool_name}"
        try:
            cursor_last = (
                _CAREER_CURSOR_CODEC.decode(
                    cursor,
                    filters=filters,
                    owner_scope=owner_scope,
                    snapshot=snapshot,
                )
                if cursor is not None
                else None
            )
        except ValueError as exc:
            raise ToolExecutionError(
                "Career query cursor is invalid or no longer current."
            ) from exc
        page_source = (
            tuple(item for item in items_with_keys if item[0] > cursor_last)
            if cursor_last is not None
            else items_with_keys
        )
        page = page_source[:limit]
        has_more = len(page_source) > limit
        next_cursor = (
            _CAREER_CURSOR_CODEC.encode(
                filters=filters,
                owner_scope=owner_scope,
                snapshot=snapshot,
                last_key=page[-1][0],
            )
            if has_more and page
            else None
        )
        freshness = self._freshness(source_id)
        freshness_states = {item.state for item in freshness}
        completeness = CompletenessState.MORE_AVAILABLE if has_more else CompletenessState.COMPLETE
        if FreshnessState.UNAVAILABLE in freshness_states:
            completeness = CompletenessState.UNAVAILABLE
        elif FreshnessState.CACHED_STALE in freshness_states:
            completeness = CompletenessState.CACHED_STALE
        return QueryEnvelope[dict[str, object]](
            query_id=_query_id(tool_name, self._external_event_id, query, cursor),
            as_of=self._now,
            timezone=self._timezone,
            result_kind=QueryResultKind.JOBS,
            applied_filters=filters,
            freshness=freshness,
            items=tuple(item[1] for item in page),
            result_count=len(page),
            has_more=has_more,
            next_cursor=next_cursor,
            completeness=completeness,
        )

    def _freshness(self, source_id: str) -> tuple[SourceFreshness, ...]:
        warning = self._sync_warning or {}
        status = str(warning.get("status", "fresh"))
        sync_status = str(warning.get("sync_status", "succeeded"))
        raw_codes = warning.get("diagnostic_codes", ())
        codes = (
            tuple(str(item) for item in cast(Sequence[object], raw_codes))
            if isinstance(raw_codes, Sequence) and not isinstance(raw_codes, str | bytes)
            else ()
        )
        if status == "cached_fallback":
            diagnostics = (sync_status, *codes) if sync_status else codes
            return (
                SourceFreshness(
                    source_id=source_id,
                    state=FreshnessState.CACHED_STALE,
                    diagnostic_codes=diagnostics[:10],
                ),
            )
        if status == "fresh_partial":
            return (
                SourceFreshness(
                    source_id=source_id,
                    state=FreshnessState.FRESH_PARTIAL_FOR_UNREQUESTED_SOURCES,
                    as_of=self._now,
                    diagnostic_codes=codes[:10],
                ),
            )
        return (
            SourceFreshness(
                source_id=source_id,
                state=FreshnessState.FRESH_COMPLETE,
                as_of=self._now,
            ),
        )

    def _snapshot_identity(self) -> str:
        warning = self._sync_warning or {}
        parts = (
            self._now.isoformat(),
            str(warning.get("status", "fresh")),
            str(warning.get("sync_status", "succeeded")),
            ",".join(_diagnostic_codes(warning.get("diagnostic_codes", ()))),
        )
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def _clarify(
        self,
        interview: InterviewEventSnapshot,
        kind: ClarificationKind,
        question: str,
        *,
        partial_state: Mapping[str, object],
    ) -> Mapping[str, object]:
        self._store.save_clarification(
            CareerClarificationRequest(
                kind=kind,
                subject_type="interview",
                subject_id=interview.interview_page_id,
                question=question,
                idempotency_key=(
                    f"career:{kind.value}:{interview.interview_page_id}:"
                    f"{interview.content_fingerprint}"
                ),
                partial_state=dict(partial_state),
            )
        )
        return {"status": "needs_clarification", "question": question}


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def _career_item_id(item: Mapping[str, object]) -> str:
    value = (
        item.get("stable_id")
        or item.get("interview_page_id")
        or item.get("application_id")
        or item.get("row_block_id")
    )
    return str(value or "")


def _confidence(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _career_filters(
    *,
    query: str,
    limit: int,
    now: datetime,
    timezone: str,
) -> NormalizedQueryFilters:
    return NormalizedQueryFilters(
        temporal=resolve_temporal_window(
            TemporalQuery(scope=TemporalScope.UPCOMING),
            request_time=now,
            timezone=timezone,
        ),
        completion=CompletionMode.INCOMPLETE,
        text=query,
        roles=("career",),
        limit=limit,
    )


def _diagnostic_codes(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(str(item) for item in cast(Sequence[object], value))


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


def _query_id(tool_name: str, event_id: str, query: str, cursor: str | None) -> str:
    digest = hashlib.sha256(f"{event_id}|{tool_name}|{query}|{cursor or ''}".encode()).hexdigest()
    return f"{tool_name}:{digest[:24]}"


def _career_query_terms(query: str) -> tuple[str, ...]:
    return tuple(
        term
        for raw in query.casefold().replace("/", " ").replace("-", " ").split()
        for term in ("".join(char for char in raw if char.isalnum()),)
        if len(term) >= 2 and term not in _CAREER_QUERY_STOPWORDS
    )


def _interview_matches(item: InterviewEventSnapshot, terms: tuple[str, ...]) -> bool:
    if not terms:
        return True
    haystack = " ".join(
        (
            item.title,
            " ".join(item.tags),
            item.calendar_semantic_overview or "",
            item.calendar_semantic_description or "",
        )
    ).casefold()
    return all(term in haystack for term in terms)


def _interview_sort_key(item: InterviewEventSnapshot) -> tuple[str, ...]:
    return (
        "0",
        item.local_date.isoformat(),
        item.date_start.isoformat() if item.date_start is not None else "",
        item.title.casefold(),
        item.interview_page_id,
    )


def _application_sort_key(item: Mapping[str, object]) -> tuple[str, ...]:
    return (
        "1",
        str(item.get("company_name") or "").casefold(),
        str(item.get("role_title") or "").casefold(),
        str(item["application_id"]),
    )


def _application_payloads(
    applications: tuple[CareerApplicationSnapshot, ...],
    terms: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    payloads: list[dict[str, object]] = []
    for application in applications:
        haystack = " ".join(
            (
                application.company_name or "",
                application.role_title or "",
                application.pipeline_status or "",
                application.next_action or "",
                _temporal_date_label(application.next_action_temporal, application.timezone)
                if application.next_action_temporal
                else "",
                application.posting_url or "",
            )
        ).casefold()
        if terms and not all(term in haystack for term in terms):
            continue
        payloads.append(
            {
                "kind": "application",
                "stable_id": application.application_id,
                "application_id": application.application_id,
                "company_name": application.company_name,
                "role_title": application.role_title,
                "pipeline_status": application.pipeline_status,
                "next_action": application.next_action,
                "next_action_date": (
                    _temporal_date_label(application.next_action_temporal, application.timezone)
                    if application.next_action_temporal
                    else None
                ),
                "next_action_temporal": (
                    application.next_action_temporal.model_dump(mode="json")
                    if application.next_action_temporal
                    else None
                ),
                "posting_url": application.posting_url,
                "source_url": application.source_url,
            }
        )
    return tuple(sorted(payloads, key=_application_sort_key))


def _interview_temporal(item: InterviewEventSnapshot) -> TemporalValue:
    if item.temporal_value is not None:
        return item.temporal_value
    if item.is_all_day or item.date_start is None:
        return DateOnlyValue(start_date=item.local_date)
    return DateTimeValue(
        start_at=item.date_start,
        timezone=item.timezone,
    )


def _temporal_date_label(value: TemporalValue | None, timezone: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, DateOnlyValue):
        return value.start_date.isoformat()
    return value.start_at.astimezone(ZoneInfo(timezone)).date().isoformat()


def _temporal_time_label(value: TemporalValue | None, timezone: str) -> str | None:
    if value is None or isinstance(value, DateOnlyValue):
        return None
    return value.start_at.astimezone(ZoneInfo(timezone)).strftime("%H:%M %Z")


__all__ = ["CareerAgentToolState"]
