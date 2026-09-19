from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.agents.harness import ToolExecutionError
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
        assert interview_page_id == "interview-1"
        return self.plan

    def application_row_snapshots(self):
        return self.rows

    def application_table_snapshots(self):
        return self.table_rows

    def save_clarification(self, request):
        self.clarifications.append(request)


def _event():
    return InterviewEventSnapshot(
        interview_page_id="interview-1",
        title="Shopify Technical Interview",
        local_date=date(2026, 9, 20),
        is_all_day=True,
        last_edited_at=NOW,
        content_fingerprint="source-fingerprint",
    )


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

    assert result["query"] == "what jobs dates are coming up?"
    assert result["sync"]["status"] == "fresh"
    assert result["interviews"][0]["date"] == "2026-09-20"
    assert result["application_tables"][0]["columns"] == [
        {"index": 0, "header": "Company"},
        {"index": 1, "header": "Role"},
        {"index": 2, "header": "Status"},
    ]
    assert result["application_tables"][0]["rows"] == [
        {
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

    assert result["sync"] == {
        "status": "cached_fallback",
        "sync_status": "setup_required",
        "diagnostic_codes": ["jobs_page_missing"],
        "message": "Jobs sync needs attention; returned cached career data.",
    }
    assert result["interviews"][0]["title"] == "Shopify Technical Interview"
    assert result["application_tables"][0]["rows"][0]["cells"] == [
        "Shopify",
        "Backend Developer",
        "Applied",
    ]


async def test_search_jobs_context_uses_cached_data_when_sync_raises() -> None:
    store = _Store(table_rows=(_row("app-row", 1, ("Shopify", "Backend Developer", "Applied")),))
    state = _state(store, syncer=_Syncer(fail=True))
    tools = {tool.name: tool for tool in state.tools()}

    result = await tools["search_jobs_context"].handler({"query": "Shopify"})

    assert result["sync"]["status"] == "cached_fallback"
    assert result["sync"]["sync_status"] == "failed"
    assert result["interviews"][0]["date"] == "2026-09-20"


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

    assert interview_result["interviews"][0]["date"] == "2026-09-20"
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
