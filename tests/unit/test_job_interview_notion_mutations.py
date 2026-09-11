from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.job_interviews.notion_mutations import (
    confirm_career_write,
    propose_interview_date_write,
    propose_preparation_plan_write,
)
from app.connectors.notion import NotionConnector, NotionWriteConflict, NotionWriteReceipt
from app.db.job_interviews import (
    InterviewEventInput,
    JobInterviewRepository,
    JobsWorkspaceInput,
    PreparationPlanInput,
)
from app.db.models import Base, CareerWriteProposal, CareerWriteReceipt

EDITED_AT = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'career-writes.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _seed_interview(engine: Any, *, with_plan: bool = False) -> None:
    with Session(engine) as session, session.begin():
        workspace = JobInterviewRepository.upsert_jobs_workspace(
            session,
            snapshot=JobsWorkspaceInput(
                jobs_page_id="jobs-page",
                jobs_page_title="Jobs",
                discovery_status="valid",
                interviews_database_id="interviews-db",
                interviews_data_source_id="interviews-source",
                title_property_id="title-prop",
                date_property_id="date-prop",
                discovered_at=EDITED_AT,
                synced_at=EDITED_AT,
            ),
        )
        JobInterviewRepository.upsert_interview_event(
            session,
            workspace_id=workspace.id,
            event=InterviewEventInput(
                interview_page_id="interview-1",
                title="Shopify Backend Technical Round",
                local_date=date(2026, 9, 20),
                notion_last_edited_at=EDITED_AT,
                content_fingerprint="interview-hash",
            ),
        )
        if with_plan:
            JobInterviewRepository.save_current_plan(
                session,
                plan=PreparationPlanInput(
                    interview_page_id="interview-1",
                    generated_at=EDITED_AT,
                    plan_hash="plan-hash",
                    summary="Prepare the verified API-design topics.",
                    next_actions=("Practice one API-design walkthrough.",),
                    evidence=("posting:requirements",),
                    plan_payload={
                        "daily_actions": ["Practice one API-design walkthrough."],
                        "source_ids": ["posting:requirements"],
                    },
                ),
            )


class _RecordingWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    async def apply(self, proposal: dict[str, Any]) -> NotionWriteReceipt:
        self.calls.append(proposal)
        if self.fail:
            raise RuntimeError("write result could not be verified")
        return NotionWriteReceipt(
            proposal_id=str(proposal["id"]),
            page_id=str(proposal["target_page_id"]),
        )


@pytest.mark.asyncio
async def test_date_proposal_requires_exact_confirmation_and_replays_without_second_write(
    engine: Any,
) -> None:
    _seed_interview(engine)
    proposal = propose_interview_date_write(
        engine=engine,
        interview_page_id="interview-1",
        proposed_date="2026-09-25T14:00:00-04:00",
        requester="discord-user-1",
        idempotency_key="discord-message-1:date",
        now=EDITED_AT,
    )
    writer = _RecordingWriter()

    wrong = await confirm_career_write(
        engine=engine,
        writer=writer,
        proposal_id=uuid.UUID(proposal["proposal_id"]),
        confirmation_event="confirm the interview please",
        now=EDITED_AT,
    )
    assert wrong["status"] == "token_mismatch"
    assert writer.calls == []

    applied = await confirm_career_write(
        engine=engine,
        writer=writer,
        proposal_id=uuid.UUID(proposal["proposal_id"]),
        confirmation_event=proposal["confirmation_token"],
        now=EDITED_AT,
    )
    replay = await confirm_career_write(
        engine=engine,
        writer=writer,
        proposal_id=uuid.UUID(proposal["proposal_id"]),
        confirmation_event=proposal["confirmation_token"],
        now=EDITED_AT,
    )
    assert applied["status"] == replay["status"] == "applied"
    assert len(writer.calls) == 1
    assert writer.calls[0]["payload"] == {"date_start": "2026-09-25T14:00:00-04:00"}


@pytest.mark.asyncio
async def test_failed_external_write_is_audited_uncertain_and_never_retried(engine: Any) -> None:
    _seed_interview(engine)
    proposal = propose_interview_date_write(
        engine=engine,
        interview_page_id="interview-1",
        proposed_date="2026-09-25",
        requester="discord-user-1",
        idempotency_key="discord-message-2:date",
        now=EDITED_AT,
    )
    writer = _RecordingWriter(fail=True)
    proposal_id = uuid.UUID(proposal["proposal_id"])
    with pytest.raises(RuntimeError, match="could not be verified"):
        await confirm_career_write(
            engine=engine,
            writer=writer,
            proposal_id=proposal_id,
            confirmation_event=proposal["confirmation_token"],
            now=EDITED_AT,
        )
    replay = await confirm_career_write(
        engine=engine,
        writer=writer,
        proposal_id=proposal_id,
        confirmation_event=proposal["confirmation_token"],
        now=EDITED_AT,
    )
    assert replay["status"] == "uncertain"
    assert len(writer.calls) == 1
    with Session(engine) as session:
        receipt = session.scalar(
            select(CareerWriteReceipt).where(CareerWriteReceipt.proposal_id == proposal_id)
        )
        assert receipt is not None
        assert receipt.state == "uncertain"
        assert receipt.error_code == "career_notion_write_unverified"


def test_plan_proposal_captures_exact_current_revision_without_writing(engine: Any) -> None:
    _seed_interview(engine, with_plan=True)
    proposal = propose_preparation_plan_write(
        engine=engine,
        interview_page_id="interview-1",
        requester="discord-user-1",
        idempotency_key="discord-message-3:plan",
        now=EDITED_AT,
    )
    assert proposal["status"] == "pending"
    assert "Plan revision: 1" in proposal["preview"]
    assert "Practice one API-design walkthrough." in proposal["preview"]
    with Session(engine) as session:
        row = session.get(CareerWriteProposal, uuid.UUID(proposal["proposal_id"]))
        assert row is not None
        assert row.payload["plan_revision"] == 1
        assert row.state == "pending"


def _interview_page(*, edited: str = "2026-09-11T14:00:00.000Z") -> dict[str, Any]:
    return {
        "id": "interview-1",
        "last_edited_time": edited,
        "url": "https://www.notion.so/interview-1",
        "archived": False,
        "in_trash": False,
        "properties": {
            "Date": {
                "id": "date-prop",
                "type": "date",
                "date": {"start": "2026-09-20", "end": None},
            }
        },
    }


@pytest.mark.asyncio
async def test_guarded_interview_date_write_patches_only_discovered_date_property() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_interview_page())
        return httpx.Response(
            200,
            json={"id": "interview-1", "url": "https://www.notion.so/interview-1"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        await connector.guarded_update_interview_date(
            proposal_id=str(uuid.uuid4()),
            page_id="interview-1",
            date_property_id="date-prop",
            expected_last_edited_at=EDITED_AT,
            due="2026-09-25T14:00:00-04:00",
        )

    assert [request.method for request in requests] == ["GET", "PATCH"]
    assert json.loads(requests[1].content) == {
        "properties": {"date-prop": {"date": {"start": "2026-09-25T18:00:00Z"}}}
    }


@pytest.mark.asyncio
async def test_guarded_interview_date_write_rejects_stale_page_without_patch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_interview_page(edited="2026-09-11T14:01:00.000Z"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        with pytest.raises(NotionWriteConflict):
            await connector.guarded_update_interview_date(
                proposal_id=str(uuid.uuid4()),
                page_id="interview-1",
                date_property_id="date-prop",
                expected_last_edited_at=EDITED_AT,
                due="2026-09-25",
            )
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_plan_write_appends_only_to_unique_lifeagent_owned_child_page() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/pages/interview-1":
            return httpx.Response(200, json=_interview_page())
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "owned-plan-page",
                            "type": "child_page",
                            "child_page": {"title": "Interview Preparation — LifeAgent"},
                        },
                        {
                            "id": "user-notes",
                            "type": "paragraph",
                            "paragraph": {"rich_text": []},
                        },
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        await connector.append_interview_preparation_plan(
            proposal_id=str(uuid.uuid4()),
            page_id="interview-1",
            date_property_id="date-prop",
            expected_last_edited_at=EDITED_AT,
            plan_revision=2,
            plan_text="Practice the verified API-design requirements.",
        )

    assert [request.method for request in requests] == ["GET", "GET", "PATCH"]
    assert requests[2].url.path == "/v1/blocks/owned-plan-page/children"
    payload = json.loads(requests[2].content)
    assert payload["children"][0]["heading_2"]["rich_text"][0]["text"]["content"] == (
        "LifeAgent plan revision 2"
    )
    assert all("user-notes" not in request.url.path for request in requests)
