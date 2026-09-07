from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.connectors.notion import NotionConnector, NotionWriteConflict
from app.core.errors import LifeAgentError

EDITED = "2026-09-03T12:00:00.000Z"
EDITED_AT = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


def _title_property(text: str, *, prop_id: str = "title-prop") -> dict[str, object]:
    return {
        "id": prop_id,
        "name": "Name",
        "type": "title",
        "title": [{"plain_text": text}],
    }


def _date_property(start: str, *, prop_id: str = "date-prop") -> dict[str, object]:
    return {
        "id": prop_id,
        "name": "Date",
        "type": "date",
        "date": {"start": start, "end": None, "time_zone": None},
    }


def _page(title: str, *, edited: str = EDITED) -> dict[str, object]:
    return {
        "id": "assessment-page-1",
        "last_edited_time": edited,
        "url": "https://www.notion.so/assessment-page-1",
        "archived": False,
        "in_trash": False,
        "properties": {
            "Name": _title_property(title),
            "Date": _date_property("2026-09-20"),
        },
    }


def _receipt() -> dict[str, object]:
    return {"id": "assessment-page-1", "url": "https://www.notion.so/assessment-page-1"}


@pytest.mark.asyncio
async def test_create_assessment_page_posts_data_source_parent_with_title_and_date_only() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_receipt())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        receipt = await connector.create_assessment_page(
            proposal_id="proposal-1",
            data_source_id="assessments-source-1",
            title_property_id="title-prop",
            date_property_id="date-prop",
            title="Lab 1",
            due="2026-09-20",
        )

    assert receipt.proposal_id == "proposal-1"
    assert receipt.page_id == "assessment-page-1"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/pages"
    assert requests[0].headers["Notion-Version"] == "2025-09-03"
    assert json.loads(requests[0].content) == {
        "parent": {"type": "data_source_id", "data_source_id": "assessments-source-1"},
        "properties": {
            "title-prop": {"title": [{"type": "text", "text": {"content": "Lab 1"}}]},
            "date-prop": {"date": {"start": "2026-09-20"}},
        },
    }


@pytest.mark.asyncio
async def test_guarded_update_patches_title_date_only_after_precondition() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_page("Old title"))
        return httpx.Response(200, json=_receipt())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        receipt = await connector.guarded_update_assessment_page(
            proposal_id="proposal-2",
            page_id="assessment-page-1",
            title_property_id="title-prop",
            date_property_id="date-prop",
            expected_title="Old title",
            expected_last_edited_at=EDITED_AT,
            title="New title",
            due=datetime(2026, 9, 21, 17, 30, tzinfo=UTC),
        )

    assert receipt.proposal_id == "proposal-2"
    assert [request.method for request in requests] == ["GET", "PATCH"]
    assert requests[0].url.path == "/v1/pages/assessment-page-1"
    assert requests[1].url.path == "/v1/pages/assessment-page-1"
    assert json.loads(requests[1].content) == {
        "properties": {
            "title-prop": {"title": [{"type": "text", "text": {"content": "New title"}}]},
            "date-prop": {"date": {"start": "2026-09-21T17:30:00Z"}},
        }
    }


@pytest.mark.asyncio
async def test_guarded_update_assessment_page_rejects_stale_page_without_patch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_page("User changed", edited="2026-09-03T12:01:00.000Z"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        with pytest.raises(NotionWriteConflict):
            await connector.guarded_update_assessment_page(
                proposal_id="proposal-3",
                page_id="assessment-page-1",
                title_property_id="title-prop",
                date_property_id="date-prop",
                expected_title="Old title",
                expected_last_edited_at=EDITED_AT,
                due="2026-09-22",
            )

    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_guarded_archive_assessment_page_verifies_precondition_then_archives() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_page("Delete me"))
        return httpx.Response(200, json=_receipt())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        receipt = await connector.guarded_archive_assessment_page(
            proposal_id="proposal-4",
            page_id="assessment-page-1",
            title_property_id="title-prop",
            expected_title="Delete me",
            expected_last_edited_at=EDITED_AT,
        )

    assert receipt.proposal_id == "proposal-4"
    assert [request.method for request in requests] == ["GET", "PATCH"]
    assert requests[1].url.path == "/v1/pages/assessment-page-1"
    assert json.loads(requests[1].content) == {"archived": True}


@pytest.mark.asyncio
async def test_guarded_archive_assessment_page_rejects_stale_page_without_patch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page("Already moved"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        with pytest.raises(NotionWriteConflict):
            await connector.guarded_archive_assessment_page(
                proposal_id="proposal-5",
                page_id="assessment-page-1",
                title_property_id="title-prop",
                expected_title="Delete me",
                expected_last_edited_at=EDITED_AT,
            )

    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "", "due": "2026-09-20"},
        {"title": "Lab", "due": "2026-09-20T12:00:00"},
        {"title": "Lab", "due": "not-a-date"},
    ],
)
@pytest.mark.asyncio
async def test_create_assessment_page_rejects_invalid_title_or_due_without_request(
    kwargs: dict[str, Any],
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_receipt())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        with pytest.raises(LifeAgentError):
            await connector.create_assessment_page(
                proposal_id="proposal-6",
                data_source_id="assessments-source-1",
                title_property_id="title-prop",
                date_property_id="date-prop",
                **kwargs,
            )

    assert requests == []
