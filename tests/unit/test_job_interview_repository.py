"""Focused career interview persistence tests using SQLite."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.agents.action_items import DateOnlyValue
from app.agents.job_interviews.contracts import PreparationPlanSnapshot
from app.db.job_interviews import (
    ApplicationRowInput,
    ApplicationTableInput,
    CalendarSemanticResultInput,
    CareerApplicationInput,
    CareerWriteProposalInput,
    InterviewEventInput,
    InterviewLinkInput,
    JobInterviewRepository,
    JobsWorkspaceInput,
    PreparationPlanInput,
    SQLAlchemyJobInterviewStore,
)
from app.db.models import (
    Base,
    CareerApplication,
    CareerApplicationRow,
    CareerPreparationPlanRevision,
    CareerWriteReceipt,
)


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'job-interviews.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _workspace(session: Session):
    return JobInterviewRepository.upsert_jobs_workspace(
        session,
        snapshot=JobsWorkspaceInput(
            jobs_page_id="jobs-page",
            jobs_page_title="Jobs",
            discovery_status="valid",
            interviews_database_id="interviews-db",
            interviews_data_source_id="interviews-source",
            discovered_at=datetime(2026, 9, 10, tzinfo=UTC),
            synced_at=datetime(2026, 9, 10, tzinfo=UTC),
        ),
    )


def _seed_sources(session: Session) -> None:
    workspace = _workspace(session)
    JobInterviewRepository.upsert_application_table(
        session,
        workspace_id=workspace.id,
        snapshot=ApplicationTableInput(
            table_block_id="table-1",
            table_order=0,
            has_column_header=True,
            content_fingerprint="table-hash",
            last_seen_at=datetime(2026, 9, 10, tzinfo=UTC),
            rows=(
                ApplicationRowInput(
                    row_block_id="header",
                    row_order=0,
                    cells=("Company", "Job"),
                    normalized_cells=("company", "job"),
                    content_fingerprint="header-hash",
                    last_seen_at=datetime(2026, 9, 10, tzinfo=UTC),
                    is_header=True,
                ),
                ApplicationRowInput(
                    row_block_id="row-shopify",
                    row_order=1,
                    cells=("Shopify", "Backend Developer"),
                    normalized_cells=("shopify", "backend developer"),
                    content_fingerprint="row-hash",
                    last_seen_at=datetime(2026, 9, 10, tzinfo=UTC),
                ),
            ),
        ),
    )
    JobInterviewRepository.upsert_interview_event(
        session,
        workspace_id=workspace.id,
        event=InterviewEventInput(
            interview_page_id="interview-1",
            title="Shopify Backend Technical Round",
            local_date=date(2026, 9, 20),
            date_start=datetime(2026, 9, 20, 18, tzinfo=UTC),
            notion_last_edited_at=datetime(2026, 9, 10, tzinfo=UTC),
            content_fingerprint="interview-hash",
            source_url="https://notion.test/interview-1",
            url_candidates=({"url": "https://jobs.example/1", "source_kind": "block"},),
        ),
    )


def test_application_rows_soft_deactivate_and_links_are_replay_safe(engine) -> None:
    with Session(engine) as session, session.begin():
        _seed_sources(session)
        active_rows = JobInterviewRepository.list_active_application_rows(session)
        assert active_rows[0]["table_block_id"] == "table-1"
        table = session.scalar(
            select(CareerApplicationRow).where(CareerApplicationRow.row_block_id == "row-shopify")
        )
        assert table is not None
        workspace = session.scalar(
            select(CareerApplicationRow).where(CareerApplicationRow.row_block_id == "header")
        )
        assert workspace is not None

    with Session(engine) as session, session.begin():
        active = JobInterviewRepository.list_active_application_rows(session)
        assert [row["row_block_id"] for row in active] == ["row-shopify"]
        table_rows = JobInterviewRepository.list_application_table_rows(session)
        assert [row["row_block_id"] for row in table_rows] == ["header", "row-shopify"]
        assert table_rows[0]["is_header"] is True
        link = JobInterviewRepository.save_interview_link(
            session,
            link=InterviewLinkInput(
                interview_page_id="interview-1",
                row_block_id="row-shopify",
                state="matched",
                confidence=1,
                rationale="Company and role match.",
                evidence=({"row_block_id": "row-shopify", "column_index": 0, "text": "Shopify"},),
                interview_content_fingerprint="interview-hash",
                application_content_fingerprint="row-hash",
                resolved_at=datetime(2026, 9, 10, tzinfo=UTC),
            ),
        )
        replay = JobInterviewRepository.save_interview_link(
            session,
            link=InterviewLinkInput(
                interview_page_id="interview-1",
                row_block_id="row-shopify",
                state="matched",
                confidence=1,
                rationale="Company and role still match.",
                evidence=(),
                interview_content_fingerprint="interview-hash",
                application_content_fingerprint="row-hash",
                resolved_at=datetime(2026, 9, 11, tzinfo=UTC),
            ),
        )
        assert replay.id == link.id
        assert (
            JobInterviewRepository.get_interview_link(session, interview_page_id="interview-1")[
                "rationale"
            ]
            == "Company and role still match."
        )
        persisted = JobInterviewRepository.get_interview_link(
            session, interview_page_id="interview-1"
        )
        assert persisted is not None
        assert persisted["interview_content_fingerprint"] == "interview-hash"
        assert persisted["application_content_fingerprint"] == "row-hash"


def test_typed_applications_are_active_surface_and_legacy_rows_are_history(engine) -> None:
    with Session(engine) as session, session.begin():
        _seed_sources(session)

    store = SQLAlchemyJobInterviewStore(engine)
    workspace_id = store.upsert_jobs_workspace(
        JobsWorkspaceInput(
            jobs_page_id="jobs-page",
            jobs_page_title="Jobs",
            discovery_status="valid",
            discovered_at=datetime(2026, 9, 10, tzinfo=UTC),
            synced_at=datetime(2026, 9, 10, tzinfo=UTC),
        )
    )
    store.upsert_career_application(
        workspace_id,
        CareerApplicationInput(
            application_id="application-typed",
            applications_database_id="applications-db",
            applications_data_source_id="applications-source",
            company_name="Shopify",
            role_title="Backend Developer",
            status="Interviewing",
            next_action="Send follow-up note",
            next_action_temporal=DateOnlyValue(start_date=date(2026, 9, 22)),
            posting_url="https://jobs.example/posting",
            source_url="https://notion.test/application-typed",
            content_fingerprint="typed-hash",
            last_edited_at=datetime(2026, 9, 10, tzinfo=UTC),
        ),
    )
    store.upsert_interview_event(
        workspace_id,
        InterviewEventInput(
            interview_page_id="typed-interview",
            title="Shopify Backend Technical",
            local_date=date(2026, 9, 24),
            notion_last_edited_at=datetime(2026, 9, 10, tzinfo=UTC),
            content_fingerprint="typed-interview-hash",
            application_id="application-typed",
        ),
    )

    typed = store.typed_application_snapshots()
    model_rows = store.application_row_snapshots()

    assert [item.application_id for item in typed] == ["application-typed"]
    assert typed[0].next_action_temporal is not None
    assert typed[0].next_action_temporal.start_date == date(2026, 9, 22)
    assert [row.row_block_id for row in model_rows] == ["application-typed"]
    assert {row["row_block_id"] for row in store.list_active_application_rows()} == {
        "row-shopify",
    }
    assert store.health_summary()["active_application_row_count"] == 1
    with Session(engine) as session:
        persisted = session.scalar(
            select(CareerApplication).where(
                CareerApplication.application_page_id == "application-typed"
            )
        )
        assert persisted is not None
        assert persisted.status == "Interviewing"
        assert persisted.application_url == "https://jobs.example/posting"
        relation = JobInterviewRepository.get_interview_link(
            session,
            interview_page_id="typed-interview",
        )
        assert relation is not None
        assert relation["application_id"] == "application-typed"
        assert relation["row_block_id"] == "application-typed"


def test_sqlalchemy_store_exposes_application_table_headers_for_read_only_context(engine) -> None:
    with Session(engine) as session, session.begin():
        _seed_sources(session)

    store = SQLAlchemyJobInterviewStore(engine)

    matching_rows = store.application_row_snapshots()
    table_rows = store.application_table_snapshots()

    assert matching_rows == ()
    assert [row.row_block_id for row in table_rows] == ["header", "row-shopify"]
    assert table_rows[0].is_header is True
    assert table_rows[0].cells == ("Company", "Job")


def test_current_plan_versions_only_material_changes(engine) -> None:
    with Session(engine) as session, session.begin():
        _seed_sources(session)
        first = JobInterviewRepository.save_current_plan(
            session,
            plan=PreparationPlanInput(
                interview_page_id="interview-1",
                generated_at=datetime(2026, 9, 10, tzinfo=UTC),
                plan_hash="hash-1",
                summary="Prepare API design stories.",
                next_actions=("Practice API design.",),
                evidence=("posting:requirements",),
                plan_payload={"daily_actions": ["Practice API design."]},
            ),
        )
        unchanged = JobInterviewRepository.save_current_plan(
            session,
            plan=PreparationPlanInput(
                interview_page_id="interview-1",
                generated_at=datetime(2026, 9, 11, tzinfo=UTC),
                plan_hash="hash-1",
                summary="Prepare API design stories.",
                next_actions=("Practice API design.",),
                evidence=("posting:requirements",),
                plan_payload={"daily_actions": ["Practice API design."]},
            ),
        )
        revised = JobInterviewRepository.save_current_plan(
            session,
            plan=PreparationPlanInput(
                interview_page_id="interview-1",
                generated_at=datetime(2026, 9, 12, tzinfo=UTC),
                plan_hash="hash-2",
                summary="Prepare API and behavioral stories.",
                next_actions=("Practice API design.", "Draft stories."),
                evidence=("posting:requirements",),
                plan_payload={"daily_actions": ["Practice API design.", "Draft stories."]},
                material_change_reason="New behavioral round evidence.",
            ),
        )
        assert first.id == unchanged.id == revised.id
        assert revised.revision == 2
        assert session.scalar(select(func.count()).select_from(CareerPreparationPlanRevision)) == 2


def test_confirmed_write_proposals_and_receipts_are_bounded(engine) -> None:
    with Session(engine) as session, session.begin():
        _seed_sources(session)
        proposal = JobInterviewRepository.save_write_proposal(
            session,
            proposal=CareerWriteProposalInput(
                idempotency_key="proposal-1",
                operation="preparation_plan",
                target_page_id="interview-1",
                interview_page_id="interview-1",
                payload={"plan": "safe preview only"},
                redacted_preview="Save this plan to the interview page.",
                confirmation_token="CONFIRM CAREER proposal-1",
                expires_at=datetime(2026, 9, 11, tzinfo=UTC),
            ),
        )
        status, confirmed = JobInterviewRepository.confirm_write_proposal(
            session,
            proposal_id=proposal.id,
            confirmation_token="CONFIRM CAREER proposal-1",
            confirmation_event="discord-message-1",
            now=datetime(2026, 9, 10, tzinfo=UTC),
        )
        receipt_status, _ = JobInterviewRepository.begin_write_receipt(
            session,
            proposal_id=confirmed.id,
            operation_id="notion-plan-write",
            payload_hash="a" * 64,
        )
        receipt = JobInterviewRepository.mark_write_receipt_applied(
            session,
            proposal_id=confirmed.id,
            operation_id="notion-plan-write",
            payload_hash="a" * 64,
            receipt={"page_id": "interview-1", "ignored": "private", "url": "x" * 2_000},
            applied_at=datetime(2026, 9, 10, tzinfo=UTC),
        )
        assert status == "confirmed"
        assert receipt_status == "ready"
        assert set(receipt.receipt or {}) == {"page_id", "url"}
        assert len((receipt.receipt or {})["url"]) == 1_000
        assert session.get(CareerWriteReceipt, receipt.id).state == "applied"


def test_sqlalchemy_store_adapter_satisfies_sync_and_morning_protocols(engine) -> None:
    store = SQLAlchemyJobInterviewStore(engine)
    synced_at = datetime(2026, 9, 10, tzinfo=UTC)
    workspace_id = store.upsert_jobs_workspace(
        SimpleNamespace(
            jobs_page_id="jobs-page",
            jobs_title="Jobs",
            status="valid",
            interviews_database_id="interviews-db",
            interviews_source_id="interviews-source",
            synced_at=synced_at,
        )
    )
    table_id = store.upsert_application_table(
        workspace_id,
        SimpleNamespace(
            table_block_id="table-1",
            table_order=0,
            has_column_header=True,
            row_count=1,
            column_count=2,
            content_fingerprint="table-hash",
            last_seen_at=synced_at,
        ),
    )
    store.upsert_application_row(
        table_id,
        SimpleNamespace(
            row_block_id="row-1",
            row_order=0,
            is_header=False,
            cells=("Shopify", "Backend Developer"),
            content_fingerprint="row-hash",
            last_seen_at=synced_at,
            active=True,
        ),
    )
    store.upsert_interview_event(
        workspace_id,
        SimpleNamespace(
            interview_page_id="interview-1",
            interviews_database_id="interviews-db",
            interviews_source_id="interviews-source",
            title="Shopify Backend Technical Round",
            source_url="https://notion.test/interview-1",
            starts_at=datetime(2026, 9, 20, 18, tzinfo=UTC),
            local_date=date(2026, 9, 20),
            all_day=False,
            last_edited_at=synced_at,
            archived=False,
            active=True,
            properties={"Date": "2026-09-20"},
            url_candidates=({"url": "https://jobs.example/1", "source_kind": "block"},),
        ),
    )
    store.save_preparation_plan(
        PreparationPlanSnapshot(
            interview_page_id="interview-1",
            revision=1,
            generated_at=synced_at + timedelta(hours=1),
            plan_hash="hash-1",
            summary="Prepare API stories.",
            next_actions=("Practice API design.",),
            evidence=("posting:requirements",),
            plan={"daily_actions": ["Practice API design."]},
        )
    )

    upcoming = store.load_upcoming_interviews(now=synced_at)
    assert upcoming[0].interview_page_id == "interview-1"
    assert upcoming[0].url_candidates[0].url == "https://jobs.example/1"
    assert store.get_current_plan("interview-1").revision == 1
    assert store.health_summary()["upcoming_interview_count"] == 1


def test_jobs_calendar_items_are_upper_bounded_and_semantic_cache_is_optimistic(engine) -> None:
    with Session(engine) as session, session.begin():
        workspace = _workspace(session)
        JobInterviewRepository.upsert_interview_event(
            session,
            workspace_id=workspace.id,
            event=InterviewEventInput(
                interview_page_id="interview-all-day",
                title="All-day interview milestone",
                local_date=date(2026, 9, 20),
                notion_last_edited_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
                content_fingerprint="all-day-source",
                is_all_day=True,
            ),
        )
        JobInterviewRepository.upsert_interview_event(
            session,
            workspace_id=workspace.id,
            event=InterviewEventInput(
                interview_page_id="interview-inclusive-end",
                title="Noon endpoint interview",
                local_date=date(2026, 9, 20),
                date_start=datetime(2026, 9, 20, 16, tzinfo=UTC),
                notion_last_edited_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
                content_fingerprint="endpoint-source",
            ),
        )
        JobInterviewRepository.upsert_interview_event(
            session,
            workspace_id=workspace.id,
            event=InterviewEventInput(
                interview_page_id="interview-outside",
                title="After endpoint interview",
                local_date=date(2026, 9, 20),
                date_start=datetime(2026, 9, 20, 16, 1, tzinfo=UTC),
                notion_last_edited_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
                content_fingerprint="outside-source",
            ),
        )
        saved = JobInterviewRepository.save_interview_calendar_semantics(
            session,
            interview_page_id="interview-inclusive-end",
            semantics=CalendarSemanticResultInput(
                status="valid",
                overview="A technical interview at the inclusive window endpoint.",
                source_fingerprint="endpoint-source",
                source_last_edited_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
                model_identity="qwen-test",
                config_version="calendar-test",
                prompt_version="prompt-v1",
                analyzed_at=datetime(2026, 9, 9, 13, tzinfo=UTC),
                evidence_ids=("frag-1",),
                intent_value="regular",
                intent_status="valid",
                intent_rationale="Career interviews are ordinary calendar events.",
                intent_evidence_ids=("frag-2",),
            ),
        )
        stale = JobInterviewRepository.save_interview_calendar_semantics(
            session,
            interview_page_id="interview-inclusive-end",
            semantics=CalendarSemanticResultInput(
                status="valid",
                overview="Stale overview",
                description="Stale description",
                source_fingerprint="older-evidence-source",
                source_last_edited_at=datetime(2026, 9, 9, 11, 59, tzinfo=UTC),
                model_identity="qwen-test",
                config_version="calendar-test",
                prompt_version="prompt-v1",
                analyzed_at=datetime(2026, 9, 9, 14, tzinfo=UTC),
                evidence_ids=("frag-old",),
                description_evidence_ids=("frag-old",),
            ),
        )
        assert saved is True
        assert stale is False

    items = SQLAlchemyJobInterviewStore(engine).load_upcoming_calendar_items(
        occurrence=date(2026, 9, 10)
    )

    assert [item["event_id"] for item in items] == [
        "interview-all-day",
        "interview-inclusive-end",
    ]
    assert items[0]["is_all_day"] is True
    assert items[1]["semantic_status"] == "valid"
    assert items[1]["semantic_description"] is None
    assert items[1]["semantic_intent_value"] == "regular"
    assert items[1]["semantic_intent_status"] == "valid"
    assert items[1]["semantic_cache"]["intent_value"] == "regular"
    assert items[1]["semantic_cache"]["intent_evidence_ids"] == ("frag-2",)
    assert items[1]["semantic_cache"]["source_fingerprint"] == "endpoint-source"
