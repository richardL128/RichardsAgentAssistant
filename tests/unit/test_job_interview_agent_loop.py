from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.agents.harness import TerminalGrounding, ToolExecutionError
from app.agents.job_interviews.agent_loop import CareerAgentToolState
from app.agents.job_interviews.contracts import (
    ApplicationRowSnapshot,
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
    def __init__(self, plan=None, rows=(), table_rows=None, interviews=None):
        self.plan = plan
        self.rows = rows
        self.table_rows = rows if table_rows is None else table_rows
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
        return self.rows

    def application_table_snapshots(self):
        return self.table_rows

    def save_clarification(self, request):
        self.clarifications.append(request)


def _event():
    return _interview("interview-1", "Shopify Technical Interview", date(2026, 9, 20))


def _interview(interview_id, title, local_date):
    return InterviewEventSnapshot(
        interview_page_id=interview_id,
        title=title,
        local_date=local_date,
        is_all_day=True,
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


def _state(store, syncer=None):
    return CareerAgentToolState(
        store=store,
        syncer=syncer or _Syncer(),
        gateway=object(),
        now=NOW,
    )


async def test_search_jobs_context_returns_applications_and_interview_dates() -> None:
    store = _Store(
        table_rows=(
            _row("header-row", 0, ("Company", "Role", "Status"), header=True),
            _row("app-row", 1, ("Shopify", "Backend Developer", "Applied")),
        )
    )
    state = _state(store)
    tools = {tool.name: tool for tool in state.tools()}

    result = await tools["search_jobs_context"].handler({"query": "what jobs dates are coming up?"})

    _assert_envelope(result, query="what jobs dates are coming up?")
    assert _freshness_state(result) == "fresh_complete"
    assert _context_items(result, "interview")[0]["stable_id"] == "interview-1"
    assert _context_items(result, "interview")[0]["date"] == "2026-09-20"
    assert _context_items(result, "application_row") == [
        {
            "kind": "application_row",
            "stable_id": "app-row",
            "table_block_id": "applications-table",
            "row_block_id": "app-row",
            "row_order": 1,
            "cells": ["Shopify", "Backend Developer", "Applied"],
            "column_values": {
                "Company": "Shopify",
                "Role": "Backend Developer",
                "Status": "Applied",
            },
        }
    ]


async def test_search_jobs_context_uses_cached_data_when_sync_setup_needs_attention() -> None:
    store = _Store(table_rows=(_row("app-row", 1, ("Shopify", "Backend Developer", "Applied")),))
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
    assert _context_items(result, "application_row")[0]["cells"] == [
        "Shopify",
        "Backend Developer",
        "Applied",
    ]

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
    store = _Store(table_rows=(_row("app-row", 1, ("Shopify", "Backend Developer", "Applied")),))
    state = _state(store, syncer=_Syncer(fail=True))
    tools = {tool.name: tool for tool in state.tools()}

    result = await tools["search_jobs_context"].handler({"query": "Shopify"})

    _assert_envelope(result, query="Shopify", completeness="cached_stale")
    assert _freshness_state(result) == "cached_stale"
    assert result["freshness"][0]["diagnostic_codes"] == ["failed"]
    assert _context_items(result, "interview")[0]["date"] == "2026-09-20"


async def test_search_jobs_context_filters_many_unrelated_rows_before_pagination() -> None:
    unrelated = tuple(
        _row(f"other-{index}", index, (f"Company {index}", "Designer", "Rejected"))
        for index in range(1, 30)
    )
    store = _Store(
        table_rows=(
            _row("header-row", 0, ("Company", "Role", "Status"), header=True),
            *unrelated,
            _row("shopify-row", 30, ("Shopify", "Backend Developer", "Interviewing")),
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
    assert [item["row_block_id"] for item in _context_items(result, "application_row")] == [
        "shopify-row"
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
    store = _Store(rows=(), interviews=())
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
