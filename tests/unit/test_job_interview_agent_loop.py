from __future__ import annotations

from datetime import UTC, date, datetime

from app.agents.job_interviews.agent_loop import CareerAgentToolState
from app.agents.job_interviews.contracts import InterviewEventSnapshot, PreparationPlanSnapshot

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


class _Syncer:
    async def sync(self, *, now):
        assert now == NOW
        return type("Result", (), {"status": "succeeded", "diagnostic_codes": ()})()


class _Store:
    def __init__(self, plan=None, rows=()):
        self.plan = plan
        self.rows = rows
        self.clarifications = []

    def search_interviews(self, query, *, now):
        assert now == NOW
        assert query in {"", "Shopify"}
        return (_event(),)

    def get_current_plan(self, interview_page_id):
        assert interview_page_id == "interview-1"
        return self.plan

    def application_row_snapshots(self):
        return self.rows

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


def _state(store):
    return CareerAgentToolState(store=store, syncer=_Syncer(), gateway=object(), now=NOW)


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

    interviews = await tools["search_job_interviews"].handler({"query": "Shopify"})
    prepared = await tools["prepare_job_interview"].handler(
        {"interview_page_id": "interview-1", "refresh_research": False}
    )

    assert interviews[0]["date"] == "2026-09-20"
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
