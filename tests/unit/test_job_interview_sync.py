from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents.job_interviews.sync import JobInterviewNotionSync
from app.connectors.notion import (
    NotionDateValue,
    NotionInterviewEvent,
    NotionInterviewUrlCandidate,
    NotionJobApplicationTable,
    NotionJobsDiscoveryResult,
    NotionJobTableRow,
)

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


class _Connector:
    def __init__(self, result: NotionJobsDiscoveryResult) -> None:
        self.result = result

    async def discover_jobs_workspace(self) -> NotionJobsDiscoveryResult:
        return self.result


class _Store:
    def __init__(self) -> None:
        self.workspace_id = uuid.uuid4()
        self.cursors: dict[str, str | None] = {}
        self.workspaces: list[Any] = []
        self.tables: list[tuple[Any, Any]] = []
        self.interviews: list[tuple[Any, Any]] = []
        self.diagnostics: list[Any] = []
        self.deactivated_seen: set[str] | None = None

    def save_sync_cursor(self, source_id: str, cursor: str | None) -> None:
        self.cursors[source_id] = cursor

    def record_jobs_diagnostic(self, diagnostic: Any, *, synced_at: datetime) -> None:
        self.diagnostics.append((diagnostic, synced_at))

    def upsert_jobs_workspace(self, workspace: Any) -> Any:
        self.workspaces.append(workspace)
        return SimpleNamespace(id=self.workspace_id)

    def upsert_application_table(self, workspace_id: Any, table: Any) -> Any:
        self.tables.append((workspace_id, table))
        return SimpleNamespace(id=uuid.uuid4())

    def upsert_interview_event(self, workspace_id: Any, interview: Any) -> Any:
        self.interviews.append((workspace_id, interview))
        return SimpleNamespace(id=uuid.uuid4())

    def deactivate_missing_interviews(self, workspace_id: Any, seen_page_ids: set[str]) -> int:
        self.deactivated_seen = seen_page_ids
        return 1


def _row(
    row_id: str,
    order: int,
    cells: tuple[str, ...],
    *,
    header: bool = False,
) -> NotionJobTableRow:
    return NotionJobTableRow(
        table_block_id="applications-table",
        row_block_id=row_id,
        row_order=order,
        is_header=header,
        cells=cells,
        last_seen_at=NOW,
        content_fingerprint=("a" if header else "b") * 64,
    )


def _table() -> NotionJobApplicationTable:
    return NotionJobApplicationTable(
        table_block_id="applications-table",
        table_order=0,
        has_column_header=True,
        row_count=2,
        column_count=3,
        rows=(
            _row("header-row", 0, ("Company", "Job", "Status"), header=True),
            _row("app-row", 1, ("Shopify", "Backend Developer", "Applied")),
        ),
        last_seen_at=NOW,
        content_fingerprint="c" * 64,
    )


def _interview(page_id: str, date_start: str | None) -> NotionInterviewEvent:
    return NotionInterviewEvent(
        interview_id=page_id,
        jobs_page_id="jobs-page",
        interviews_database_id="interviews-db",
        interviews_source_id="interviews-source",
        interviews_source_type="data_source",
        page_id=page_id,
        source_url=f"https://www.notion.so/{page_id}",
        title=f"{page_id} technical interview",
        title_property_id="title-prop",
        title_property_name="Name",
        date_property_id="date-prop",
        date_property_name="Date",
        date=NotionDateValue(start=date_start) if date_start is not None else None,
        last_edited_at=NOW,
        properties={"Tags": ("technical",)},
        url_candidates=(
            NotionInterviewUrlCandidate(
                url="https://jobs.example.com/posting",
                source_kind="property",
                source_id=f"{page_id}:property:posting",
                source_property_id="posting",
                source_name="Posting",
                order=0,
            ),
        ),
    )


def _result(*, interviews: tuple[NotionInterviewEvent, ...]) -> NotionJobsDiscoveryResult:
    return NotionJobsDiscoveryResult(
        courses_database_id="courses-db",
        courses_source_id="courses-source",
        courses_source_type="data_source",
        jobs_page_id="jobs-page",
        jobs_title="Jobs",
        jobs_last_edited_at=NOW,
        application_tables=(_table(),),
        interviews_database_id="interviews-db",
        interviews_source_id="interviews-source",
        interviews_source_type="data_source",
        interview_title_property_id="title-prop",
        interview_title_property_name="Name",
        interview_date_property_id="date-prop",
        interview_date_property_name="Date",
        interviews=interviews,
        synced_at=NOW,
    )


@pytest.mark.asyncio
async def test_sync_batches_application_rows_and_persists_only_schedulable_interviews() -> None:
    store = _Store()
    syncer = JobInterviewNotionSync(
        connector=_Connector(
            _result(
                interviews=(
                    _interview("valid-interview", "2026-10-02T17:30:00.000Z"),
                    _interview("invalid-interview", "not-a-date"),
                )
            )
        ),
        store=store,
    )

    result = await syncer.sync(now=NOW)

    assert result.status == "partial"
    assert result.application_row_count == 1
    assert result.interview_count == 1
    assert result.unscheduled_interview_count == 1
    assert result.inactive_interview_count == 1
    assert set(store.cursors) == {"courses-source", "interviews-source"}
    assert store.workspaces[0].discovery_status == "valid"
    assert store.workspaces[0].title_property_id == "title-prop"
    assert store.workspaces[0].date_property_id == "date-prop"
    assert len(store.tables) == 1
    assert [row.row_block_id for row in store.tables[0][1].rows] == ["header-row", "app-row"]
    assert store.tables[0][1].rows[1].normalized_cells == (
        "Shopify",
        "Backend Developer",
        "Applied",
    )
    assert len(store.interviews) == 1
    assert store.interviews[0][1].interview_page_id == "valid-interview"
    assert store.interviews[0][1].local_date.isoformat() == "2026-10-02"
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
