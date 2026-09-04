"""Contract tests for the scoped Notion adapter."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.connectors.notion import (
    ConfirmedPropertyChange,
    NotionAttachment,
    NotionConnector,
)
from app.core.errors import LifeAgentError

IDS = {"courses": "courses-id", "assessments": "assessments-id", "study_blocks": "blocks-id"}


@pytest.mark.asyncio
async def test_query_delta_and_child_blocks_use_fixed_scoped_urls() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "page-1",
                            "last_edited_time": "2026-09-03T12:00:00.000Z",
                            "url": "https://www.notion.so/page-1",
                            "properties": {
                                "Course": {"type": "title", "title": []},
                                "Outline": {
                                    "type": "files",
                                    "files": [
                                        {
                                            "type": "file",
                                            "name": "outline.pdf",
                                            "file": {
                                                "url": "https://prod-files-secure.s3.us-west-2.amazonaws.com/a"
                                            },
                                        }
                                    ],
                                },
                            },
                        }
                    ],
                    "has_more": True,
                    "next_cursor": "cursor-1",
                },
            )
        return httpx.Response(200, json={"results": [], "has_more": False, "next_cursor": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", database_ids=IDS, client=client)
        result = await connector.query_database(
            "courses", last_edited_after=datetime(2026, 9, 1, tzinfo=UTC)
        )
        blocks = await connector.retrieve_block_children("page-1")

    assert result.pages[0].page_id == "page-1"
    assert result.pages[0].attachments[0].name == "outline.pdf"
    assert blocks.has_more is False
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/databases/courses-id/query"
    assert requests[1].url.path == "/v1/blocks/page-1/children"


@pytest.mark.asyncio
async def test_attachment_download_is_bounded_and_rejects_other_hosts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"pdf")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", database_ids=IDS, client=client)
        body = await connector.download_attachment(
            NotionAttachment(
                name="outline.pdf",
                url="https://prod-files-secure.s3.us-west-2.amazonaws.com/a",
            )
        )
    assert body == b"pdf"
    with pytest.raises(LifeAgentError, match="input_invalid"):
        await connector.download_attachment(
            NotionAttachment(name="bad", url="https://example.invalid/file")
        )


@pytest.mark.asyncio
async def test_confirmed_change_writes_one_allowlisted_property() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "page-1", "url": "https://www.notion.so/page-1"})

    properties = {
        database: {name: f"{database}-{name}" for name in names}
        for database, names in {
            "courses": {"course", "term", "priority", "outline", "policy"},
            "assessments": {
                "course",
                "type",
                "due",
                "grade_weight",
                "instructions",
                "rubric",
                "scope",
                "status",
                "estimated_time",
            },
            "study_blocks": {
                "assessment",
                "planned_duration",
                "actual_duration",
                "completion_state",
                "notes",
            },
        }.items()
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(
            token="secret", database_ids=IDS, property_ids=properties, client=client
        )
        receipt = await connector.apply_confirmed_change(
            ConfirmedPropertyChange(
                proposal_id="proposal-1",
                confirmation_token="confirm-1",
                page_id="page-1",
                database="assessments",
                property_id="assessments-due",
                value={"date": {"start": "2026-09-10"}},
            ),
            confirmation_event="confirm-1",
        )
    assert receipt.proposal_id == "proposal-1"
    assert requests[0].method == "PATCH"
    assert requests[0].url.path == "/v1/pages/page-1"
    assert requests[0].content.decode().count("assessments-due") == 1
