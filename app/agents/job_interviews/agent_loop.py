"""Career tools exposed only inside the authorized planner-channel harness."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.agents.harness import NativeTool, ToolExecutionError, ToolSideEffectClass
from app.agents.job_interviews.contracts import (
    ApplicationInterpretation,
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
from app.connectors.job_research import (
    CompanyResearchClient,
    UnconfiguredCompanyResearchSearchProvider,
)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _SearchInterviewsArgs(_Args):
    query: str = Field(default="", max_length=300)


class _SearchJobsContextArgs(_Args):
    query: str = Field(default="", max_length=300)


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

    def restore_checkpoint(self, value: Mapping[str, object] | None) -> None:
        """Restore verified interview capabilities from host-only state."""

        if value is None:
            return
        if value.get("version") != "career-native-tools.v1":
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

    def export_checkpoint(self) -> dict[str, object]:
        """Return the bounded host-only state needed to validate resumed tools."""

        return {
            "version": "career-native-tools.v1",
            "known_interviews": [
                item.model_dump(mode="json")
                for item in sorted(
                    self._known_interviews.values(),
                    key=lambda item: item.interview_page_id,
                )
            ],
        }

    def tools(self) -> tuple[NativeTool, ...]:
        return (
            self._tool(
                "search_jobs_context",
                "Search the owner's Jobs application table rows and upcoming interview calendar "
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
        cached_rows = self._load_application_table_context()
        await self._ensure_synced(cached_available=bool(cached_interviews or cached_rows))
        interviews = self._load_interview_context()
        rows = self._load_application_table_context()
        self._known_interviews.update((item.interview_page_id, item) for item in interviews)
        return {
            "query": args.query,
            "sync": self._sync_warning
            or {
                "status": "fresh",
                "sync_status": "succeeded",
                "message": "Jobs sync succeeded before this lookup.",
            },
            "interviews": [self._interview_payload(item) for item in interviews],
            "application_tables": _application_table_payloads(rows),
        }

    async def _search_interviews(self, arguments: Mapping[str, object]) -> object:
        args = _SearchInterviewsArgs.model_validate(arguments)
        cached = tuple(self._store.search_interviews(args.query, now=self._now))[:50]
        await self._ensure_synced(cached_available=bool(cached))
        results = tuple(self._store.search_interviews(args.query, now=self._now))[:50]
        self._known_interviews.update((item.interview_page_id, item) for item in results)
        return {
            "sync": self._sync_warning
            or {
                "status": "fresh",
                "sync_status": "succeeded",
                "message": "Jobs sync succeeded before this lookup.",
            },
            "interviews": [self._interview_payload(item) for item in results],
        }

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
        stored_rows: dict[str, Mapping[str, Any]] = {
            str(item["row_block_id"]): item for item in self._store.list_active_application_rows()
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
        return {"review": "required", **proposal}

    def _selected(self, interview_page_id: str) -> InterviewEventSnapshot:
        interview = self._known_interviews.get(interview_page_id)
        if interview is None:
            raise ToolExecutionError(
                "interview_page_id must come from search_jobs_context or search_job_interviews "
                "in this turn"
            )
        return interview

    def _interview_payload(self, item: InterviewEventSnapshot) -> Mapping[str, object]:
        return {
            "interview_page_id": item.interview_page_id,
            "title": item.title,
            "date": item.local_date.isoformat(),
            "time": (
                item.date_start.isoformat(timespec="minutes")
                if item.date_start is not None
                else None
            ),
            "timezone": item.timezone,
            "is_all_day": item.is_all_day,
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
                return tuple(cast(Sequence[InterviewEventSnapshot], loaded))[:50]
            return ()
        return tuple(self._store.search_interviews("", now=self._now))[:50]

    def _load_application_table_context(self) -> tuple[Any, ...]:
        loader = getattr(self._store, "application_table_snapshots", None)
        if callable(loader):
            loaded = loader()
            if isinstance(loaded, Sequence):
                return tuple(cast(Sequence[Any], loaded))[:75]
            return ()
        return tuple(self._store.application_row_snapshots())[:75]

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


def _confidence(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def _application_table_payloads(rows: tuple[Any, ...]) -> list[dict[str, object]]:
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row.table_block_id), []).append(row)
    payloads: list[dict[str, object]] = []
    for table_block_id, table_rows in grouped.items():
        ordered = sorted(table_rows, key=lambda row: int(getattr(row, "row_order", 0)))
        header = next((row for row in ordered if bool(getattr(row, "is_header", False))), None)
        headers = tuple(str(cell) for cell in getattr(header, "cells", ()) if str(cell).strip())
        data_rows = [row for row in ordered if not bool(getattr(row, "is_header", False))]
        max_columns = max((len(tuple(getattr(row, "cells", ()))) for row in ordered), default=0)
        columns = [
            {
                "index": index,
                "header": headers[index] if index < len(headers) else f"column_{index + 1}",
            }
            for index in range(max_columns)
        ]
        payloads.append(
            {
                "table_block_id": table_block_id,
                "columns": columns,
                "rows": [
                    {
                        "row_block_id": row.row_block_id,
                        "row_order": row.row_order,
                        "cells": list(row.cells),
                        "column_values": {
                            str(columns[index]["header"]): cell
                            for index, cell in enumerate(row.cells[: len(columns)])
                        },
                    }
                    for row in data_rows[:50]
                ],
            }
        )
    return payloads


__all__ = ["CareerAgentToolState"]
