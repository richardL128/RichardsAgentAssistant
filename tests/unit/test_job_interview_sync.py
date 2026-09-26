from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.job_interviews.sync import JobInterviewNotionSync
from app.connectors.notion import (
    NotionApplicationRecord,
    NotionDateValue,
    NotionInterviewRecord,
)

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


class _Connector:
    def __init__(
        self,
        *,
        applications: tuple[NotionApplicationRecord, ...],
        interviews: tuple[NotionInterviewRecord, ...],
    ) -> None:
        self.applications = applications
        self.interviews = interviews

    async def read_applications(self) -> tuple[NotionApplicationRecord, ...]:
        return self.applications

    async def read_interviews(self) -> tuple[NotionInterviewRecord, ...]:
        return self.interviews


class _Store:
    def __init__(self) -> None:
        self.workspace_id = uuid.uuid4()
        self.cursors: dict[str, str | None] = {}
        self.workspaces: list[Any] = []
        self.tables: list[tuple[Any, Any]] = []
        self.applications: list[tuple[Any, Any]] = []
        self.interviews: list[tuple[Any, Any]] = []
        self.diagnostics: list[Any] = []
        self.deactivated_application_seen: set[str] | None = None
        self.deactivated_seen: set[str] | None = None

    def save_sync_cursor(self, source_id: str, cursor: str | None) -> None:
        self.cursors[source_id] = cursor

    def record_jobs_diagnostic(self, diagnostic: Any, *, synced_at: datetime) -> None:
        self.diagnostics.append((diagnostic, synced_at))

    def upsert_jobs_workspace(self, workspace: Any) -> Any:
        self.workspaces.append(workspace)
        return SimpleNamespace(id=self.workspace_id)

    def upsert_career_application(self, workspace_id: Any, application: Any) -> Any:
        self.applications.append((workspace_id, application))
        return SimpleNamespace(id=uuid.uuid4())

    def deactivate_missing_career_applications(
        self,
        workspace_id: Any,
        seen_application_ids: set[str],
    ) -> int:
        self.deactivated_application_seen = seen_application_ids
        return 0

    def upsert_interview_event(self, workspace_id: Any, interview: Any) -> Any:
        self.interviews.append((workspace_id, interview))
        return SimpleNamespace(id=uuid.uuid4())

    def deactivate_missing_interviews(self, workspace_id: Any, seen_page_ids: set[str]) -> int:
        self.deactivated_seen = seen_page_ids
        return 1


def _interview(page_id: str, date_start: str | None) -> NotionInterviewRecord:
    return NotionInterviewRecord(
        page_id=page_id,
        database_id="interviews-db",
        source_id="interviews-source",
        source_type="data_source",
        source_url=f"https://www.notion.so/{page_id}",
        title=f"{page_id} technical interview",
        date=NotionDateValue(start=date_start) if date_start is not None else None,
        last_edited_at=NOW,
        property_ids={"Name": "title-prop", "Date": "date-prop", "Application": "app-rel"},
        properties={
            "Date": NotionDateValue(start=date_start) if date_start is not None else None,
        },
        stage="Technical",
        status="Scheduled",
        prep_status="To do",
        meeting_url="https://meet.example/interview",
        application_ids=("application-1",),
    )


def _application() -> NotionApplicationRecord:
    return NotionApplicationRecord(
        page_id="application-1",
        database_id="applications-db",
        source_id="applications-source",
        source_type="data_source",
        source_url="https://www.notion.so/application-1",
        title="Backend Developer",
        last_edited_at=NOW,
        property_ids={
            "Role": "role-prop",
            "Company": "company-prop",
            "Pipeline Status": "status-prop",
            "Next Action Due": "next-due-prop",
        },
        properties={},
        company="Shopify",
        role="Backend Developer",
        pipeline_status="Interviewing",
        next_action="Send follow-up note",
        next_action_due=NotionDateValue(start="2026-09-22"),
        posting_url="https://jobs.example.com/posting",
    )


@pytest.mark.asyncio
async def test_sync_batches_application_rows_and_persists_only_schedulable_interviews() -> None:
    store = _Store()
    syncer = JobInterviewNotionSync(
        connector=_Connector(
            applications=(_application(),),
            interviews=(
                _interview("valid-interview", "2026-10-02T17:30:00.000Z"),
                _interview("invalid-interview", "not-a-date"),
            )
        ),
        store=store,
    )

    result = await syncer.sync(now=NOW)

    assert result.status == "partial"
    assert result.application_row_count == 1
    assert result.table_count == 0
    assert result.interview_count == 1
    assert result.unscheduled_interview_count == 1
    assert result.inactive_interview_count == 1
    assert set(store.cursors) == {"applications-source", "interviews-source"}
    assert store.workspaces[0].discovery_status == "valid"
    assert store.workspaces[0].title_property_id == "title-prop"
    assert store.workspaces[0].date_property_id == "date-prop"
    assert store.tables == []
    assert len(store.applications) == 1
    assert store.applications[0][1].application_id == "application-1"
    assert store.applications[0][1].company_name == "Shopify"
    assert store.applications[0][1].status == "Interviewing"
    assert store.applications[0][1].next_action_temporal.start_date.isoformat() == "2026-09-22"
    assert store.deactivated_application_seen == {"application-1"}
    assert len(store.interviews) == 1
    assert store.interviews[0][1].interview_page_id == "valid-interview"
    assert store.interviews[0][1].application_id == "application-1"
    assert store.interviews[0][1].stage == "Technical"
    assert store.interviews[0][1].local_date.isoformat() == "2026-10-02"
    assert store.interviews[0][1].property_snapshot["Date"] == {
        "start": "2026-10-02T17:30:00.000Z",
        "end": None,
        "time_zone": None,
    }
    json.dumps(store.interviews[0][1].property_snapshot)
    assert store.deactivated_seen == {"valid-interview"}
    assert {item[0].code for item in store.diagnostics} == {"interview_date_invalid"}


@pytest.mark.asyncio
async def test_missing_connector_records_setup_without_persistence() -> None:
    store = _Store()
    syncer = JobInterviewNotionSync(connector=None, store=store)

    result = await syncer.sync(now=NOW)

    assert result.status == "setup_required"
    assert result.diagnostic_codes == ("notion_configuration_missing",)
    assert store.workspaces == []
    assert store.tables == []
    assert store.interviews == []
    assert store.diagnostics[0][0].code == "notion_configuration_missing"
