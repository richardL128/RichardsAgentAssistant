from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.agents.action_items import DateOnlyValue
from app.agents.harness import TerminalGrounding, ToolExecutionError
from app.agents.job_interviews.agent_loop import CareerAgentToolState
from app.agents.job_interviews.contracts import (
    ApplicationRowSnapshot,
    CareerApplicationSnapshot,
    InterviewEventSnapshot,
    PreparationPlanSnapshot,
)

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


class _Syncer:
    def __init__(self, status="succeeded", diagnostic_codes=(), *, fail=False):
        self.status = status
        self.diagnostic_codes = diagnostic_codes
        self.fail = fail

    async def sync(self, *, now):
        assert now == NOW
        if self.fail:
            raise RuntimeError("private Notion outage detail")
        return type(
            "Result",
            (),
            {"status": self.status, "diagnostic_codes": self.diagnostic_codes},
        )()


class _Store:
    def __init__(self, plan=None, applications=(), interviews=None):
        self.plan = plan
        self.applications = applications
        self.interviews = (_event(),) if interviews is None else interviews
        self.clarifications = []

    def load_upcoming_interviews(self, *, now):
        assert now == NOW
        return self.interviews

    def search_interviews(self, query, *, now):
        assert now == NOW
        terms = [term for term in query.casefold().split() if term]
        if not terms:
            return self.interviews
        return tuple(
            item for item in self.interviews if all(term in item.title.casefold() for term in terms)
        )

    def get_current_plan(self, interview_page_id):
        assert interview_page_id
        return self.plan

    def application_row_snapshots(self):
        return tuple(_application_row(item) for item in self.applications)

    def typed_application_snapshots(self):
        return self.applications

    def typed_application_rows(self):
        return [
            {
                "row_block_id": item.application_id,
                "interpretation": {
                    "company_name": item.company_name,
                    "role_title": item.role_title,
                    "status": item.pipeline_status,
                    "confidence": 1.0,
                    "model_version": "typed-career-application.v1",
                },
            }
            for item in self.applications
        ]

    def save_clarification(self, request):
        self.clarifications.append(request)


def _event():
    return _interview("interview-1", "Shopify Technical Interview", date(2026, 9, 20))


def _interview(interview_id, title, local_date, *, date_start=None, is_all_day=True):
    return InterviewEventSnapshot(
        interview_page_id=interview_id,
        title=title,
        date_start=date_start,
        local_date=local_date,
        is_all_day=is_all_day,
        last_edited_at=NOW,
        content_fingerprint=f"{interview_id}-fingerprint",
    )


def _context_items(result, kind):
    return [item for item in result["items"] if item["kind"] == kind]


def _freshness_state(result):
    return result["freshness"][0]["state"]


def _assert_envelope(result, *, query, completeness="complete"):
    assert result["query_id"]
    assert result["as_of"] == "2026-09-10T12:00:00Z"
    assert result["timezone"] == "America/Toronto"
    assert result["applied_filters"]["text"] == query
    assert result["applied_filters"]["limit"] <= 20
    assert result["result_count"] == len(result["items"])
    assert result["completeness"] == completeness


def _row(row_id, order, cells, *, header=False):
    return ApplicationRowSnapshot(
        table_block_id="applications-table",
        row_block_id=row_id,
        row_order=order,
        is_header=header,
        cells=cells,
        normalized_cells=tuple(cell.casefold() for cell in cells),
        content_fingerprint=f"{row_id}-fingerprint",
        last_seen_at=NOW,
    )


def _application(
    application_id="application-1",
    company="Shopify",
    role="Backend Developer",
    status="Applied",
    *,
    next_action="Send follow-up note",
    next_action_date=date(2026, 9, 22),
):
    return CareerApplicationSnapshot(
        application_id=application_id,
        company_name=company,
        role_title=role,
        pipeline_status=status,
        next_action=next_action,
        next_action_temporal=(
            DateOnlyValue(start_date=next_action_date) if next_action_date is not None else None
        ),
        posting_url="https://jobs.example.com/posting",
        source_url=f"https://www.notion.so/{application_id}",
        content_fingerprint=f"{application_id}-fingerprint",
        last_edited_at=NOW,
    )


def _application_row(application: CareerApplicationSnapshot) -> ApplicationRowSnapshot:
    cells = (
        application.company_name or "",
        application.role_title or "",
        application.pipeline_status or "",
        application.next_action or "",
        application.posting_url or "",
    )
    return ApplicationRowSnapshot(
        table_block_id="typed-applications",
        row_block_id=application.application_id,
        row_order=0,
        cells=cells,
        normalized_cells=tuple(cell.casefold() for cell in cells),
        content_fingerprint=application.content_fingerprint,
        last_seen_at=NOW,
    )


def _state(store, syncer=None):
    return CareerAgentToolState(
        store=store,
        syncer=syncer or _Syncer(),
        gateway=object(),
        now=NOW,
    )


async def test_search_jobs_context_returns_applications_and_interview_dates() -> None:
    store = _Store(applications=(_application(),))
    state = _state(store)
    tools = {tool.name: tool for tool in state.tools()}

    result = await tools["search_jobs_context"].handler({"query": "what jobs dates are coming up?"})

    _assert_envelope(result, query="what jobs dates are coming up?")
    assert _freshness_state(result) == "fresh_complete"
    assert _context_items(result, "interview")[0]["stable_id"] == "interview-1"
    assert _context_items(result, "interview")[0]["date"] == "2026-09-20"
    assert _context_items(result, "application") == [
        {
            "kind": "application",
            "stable_id": "application-1",
            "application_id": "application-1",
            "company_name": "Shopify",
            "role_title": "Backend Developer",
            "pipeline_status": "Applied",
            "next_action": "Send follow-up note",
            "next_action_date": "2026-09-22",
            "next_action_temporal": {
                "precision": "date",
                "start_date": "2026-09-22",
            },
            "posting_url": "https://jobs.example.com/posting",
            "source_url": "https://www.notion.so/application-1",
        }
    ]
    assert "cells" not in _context_items(result, "application")[0]
    assert "column_values" not in _context_items(result, "application")[0]


async def test_search_jobs_context_renders_timed_interview_in_local_time() -> None:
    store = _Store(
        interviews=(
            _interview(
                "timed-interview",
                "Shopify Timed Interview",
                date(2026, 9, 22),
                date_start=datetime(2026, 9, 22, 21, 30, tzinfo=UTC),
                is_all_day=False,
            ),
        )
    )
    state = _state(store)
    tools = {tool.name: tool for tool in state.tools()}

    result = await tools["search_jobs_context"].handler({"query": "Shopify"})

    interview = _context_items(result, "interview")[0]
    assert interview["date"] == "2026-09-22"
    assert interview["time"] == "17:30 EDT"
    assert interview["temporal"]["precision"] == "datetime"


async def test_search_jobs_context_uses_cached_data_when_sync_setup_needs_attention() -> None:
    store = _Store(applications=(_application(status="Applied"),))
    state = _state(
        store,
        syncer=_Syncer(status="setup_required", diagnostic_codes=("jobs_page_missing",)),
    )
    tools = {tool.name: tool for tool in state.tools()}

    result = await tools["search_jobs_context"].handler({"query": "Shopify"})

    _assert_envelope(result, query="Shopify", completeness="cached_stale")
    assert result["freshness"] == [
        {
            "source_id": "career_jobs_context",
            "state": "cached_stale",
            "as_of": None,
            "diagnostic_codes": ["setup_required", "jobs_page_missing"],
        }
    ]
    assert _context_items(result, "interview")[0]["title"] == "Shopify Technical Interview"
    assert _context_items(result, "application")[0]["pipeline_status"] == "Applied"

    grounding = TerminalGrounding(
        query_id=result["query_id"],
        item_ids=("interview-1",),
        acknowledge_stale=False,
    )
    assert state.validate_grounding(grounding) == (
        "grounding must acknowledge stale cached career data"
    )
    disclosed = grounding.__class__(
        query_id=grounding.query_id,
        item_ids=grounding.item_ids,
        acknowledge_stale=True,
    )
    assert state.validate_grounding(disclosed) is None
    assert "cached and stale" in state.render_grounding(disclosed)


async def test_search_jobs_context_uses_cached_data_when_sync_raises() -> None:
    store = _Store(applications=(_application(status="Applied"),))
    state = _state(store, syncer=_Syncer(fail=True))
    tools = {tool.name: tool for tool in state.tools()}

    result = await tools["search_jobs_context"].handler({"query": "Shopify"})

    _assert_envelope(result, query="Shopify", completeness="cached_stale")
    assert _freshness_state(result) == "cached_stale"
    assert result["freshness"][0]["diagnostic_codes"] == ["failed"]
    assert _context_items(result, "interview")[0]["date"] == "2026-09-20"


async def test_search_jobs_context_filters_many_unrelated_rows_before_pagination() -> None:
    unrelated = tuple(
        _application(f"other-{index}", f"Company {index}", "Designer", "Rejected")
        for index in range(1, 30)
    )
    store = _Store(
        applications=(
            *unrelated,
            _application("shopify-app", "Shopify", "Backend Developer", "Interviewing"),
        ),
        interviews=(
            _interview("interview-1", "Shopify Technical Interview", date(2026, 9, 20)),
            _interview("interview-2", "Stripe Recruiter Call", date(2026, 9, 21)),
        ),
    )
    state = _state(store)
    tools = {tool.name: tool for tool in state.tools()}

    result = await tools["search_jobs_context"].handler({"query": "Shopify", "limit": 20})

    _assert_envelope(result, query="Shopify")
    assert [item["title"] for item in _context_items(result, "interview")] == [
        "Shopify Technical Interview"
    ]
    assert [item["application_id"] for item in _context_items(result, "application")] == [
        "shopify-app"
    ]
    assert not result["has_more"]


async def test_search_job_interviews_paginates_with_cursor_and_preserves_capabilities() -> None:
    interviews = tuple(
        _interview(
            f"interview-{index}",
            f"Company {index:02d} Technical Interview",
            date(2026, 9, 20),
        )
        for index in range(1, 5)
    )
    state = _state(_Store(interviews=interviews))
    tools = {tool.name: tool for tool in state.tools()}

    first = await tools["search_job_interviews"].handler({"query": "Company", "limit": 2})
    second = await tools["search_job_interviews"].handler(
        {"query": "Company", "limit": 2, "cursor": first["next_cursor"]}
    )

    _assert_envelope(first, query="Company", completeness="more_available")
    assert first["has_more"] is True
    assert [item["interview_page_id"] for item in first["items"]] == [
        "interview-1",
        "interview-2",
    ]
    assert [item["interview_page_id"] for item in second["items"]] == [
        "interview-3",
        "interview-4",
    ]
    with pytest.raises(ToolExecutionError, match="interview_page_id must come"):
        await tools["prepare_job_interview"].handler({"interview_page_id": "missing"})


async def test_search_jobs_context_preserves_error_when_sync_fails_without_cache() -> None:
    store = _Store(applications=(), interviews=())
    state = _state(store, syncer=_Syncer(fail=True))
    tools = {tool.name: tool for tool in state.tools()}

    with pytest.raises(ToolExecutionError) as exc:
        await tools["search_jobs_context"].handler({"query": "Shopify"})

    assert str(exc.value) == "Jobs sync failed and no cached Jobs/interview data is available."


async def test_search_then_cached_preparation_returns_one_maintained_plan() -> None:
    plan = PreparationPlanSnapshot(
        interview_page_id="interview-1",
        revision=3,
        generated_at=NOW,
        plan_hash="plan-hash",
        summary="Current tailored plan",
        next_actions=("Practice the verified systems-design topic.",),
    )
    state = _state(_Store(plan=plan))
    tools = {tool.name: tool for tool in state.tools()}

    interview_result = await tools["search_job_interviews"].handler({"query": "Shopify"})
    prepared = await tools["prepare_job_interview"].handler(
        {"interview_page_id": "interview-1", "refresh_research": False}
    )

    assert interview_result["items"][0]["date"] == "2026-09-20"
    assert prepared["status"] == "current"
    assert prepared["plan_revision"] == 3
    assert prepared["notion_save_question"] is None


async def test_missing_application_is_a_focused_persisted_clarification() -> None:
    store = _Store()
    state = _state(store)
    tools = {tool.name: tool for tool in state.tools()}
    await tools["search_job_interviews"].handler({"query": ""})

    result = await tools["prepare_job_interview"].handler({"interview_page_id": "interview-1"})

    assert result["status"] == "needs_clarification"
    assert "Which application" in result["question"]
    assert store.clarifications[0].subject_id == "interview-1"
