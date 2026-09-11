from __future__ import annotations

import json
from datetime import UTC

import httpx
import pytest

from app.connectors.notion import NotionConnector

EDITED = "2026-09-03T12:00:00.000Z"


def _rich_text(text: str) -> list[dict[str, object]]:
    return [{"type": "text", "plain_text": text, "text": {"content": text}}]


def _title_property(text: str, *, prop_id: str = "title-prop") -> dict[str, object]:
    return {
        "id": prop_id,
        "name": "Name",
        "type": "title",
        "title": _rich_text(text),
    }


def _date_property(start: str | None, *, prop_id: str = "date-prop") -> dict[str, object]:
    return {
        "id": prop_id,
        "name": "Date",
        "type": "date",
        "date": {"start": start, "end": None, "time_zone": None} if start else None,
    }


def _course_page(page_id: str, title: str) -> dict[str, object]:
    return {
        "id": page_id,
        "last_edited_time": EDITED,
        "url": f"https://www.notion.so/{page_id}",
        "archived": False,
        "in_trash": False,
        "properties": {
            "Course": {
                "id": "course-title",
                "name": "Course",
                "type": "title",
                "title": _rich_text(title),
            },
        },
    }


def _table_row(row_id: str, cells: list[str]) -> dict[str, object]:
    return {
        "id": row_id,
        "type": "table_row",
        "has_children": False,
        "table_row": {"cells": [_rich_text(cell) for cell in cells]},
    }


def _interview_page(
    page_id: str,
    title: str,
    *,
    date_start: str | None = "2026-10-02T17:30:00.000Z",
) -> dict[str, object]:
    return {
        "id": page_id,
        "last_edited_time": EDITED,
        "url": f"https://www.notion.so/{page_id}",
        "archived": False,
        "in_trash": False,
        "properties": {
            "Name": _title_property(title),
            "Date": _date_property(date_start),
            "Tags": {
                "id": "tags-prop",
                "name": "Tags",
                "type": "multi_select",
                "multi_select": [{"name": "technical"}],
            },
            "Posting": {
                "id": "posting-prop",
                "name": "Posting",
                "type": "url",
                "url": "https://jobs.example.com/shopify-backend",
            },
        },
    }


def _interview_schema() -> dict[str, object]:
    return {
        "id": "interviews-source",
        "properties": {
            "Name": {"id": "title-prop", "name": "Name", "type": "title", "title": {}},
            "Date": {"id": "date-prop", "name": "Date", "type": "date", "date": {}},
        },
    }


@pytest.mark.asyncio
async def test_discovers_jobs_tables_interviews_urls_and_excludes_jobs_from_academics() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path == "/v1/databases/courses-db":
            return httpx.Response(200, json={"data_sources": [{"id": "courses-source"}]})
        if path == "/v1/data_sources/courses-source/query":
            return httpx.Response(
                200,
                json={
                    "results": [
                        _course_page("jobs-page", "Jobs"),
                        _course_page("bio-page", "BIO 101"),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if path == "/v1/blocks/jobs-page/children":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "applications-table",
                            "type": "table",
                            "has_children": True,
                            "table": {"table_width": 3, "has_column_header": True},
                        },
                        {
                            "id": "interviews-db",
                            "type": "child_database",
                            "has_children": False,
                            "child_database": {"title": "Interviews"},
                        },
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if path == "/v1/blocks/applications-table/children":
            return httpx.Response(
                200,
                json={
                    "results": [
                        _table_row("header-row", ["Company", "Job", "Status"]),
                        _table_row("app-row-1", ["Shopify", "Backend Developer", "Applied"]),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if path == "/v1/databases/interviews-db":
            return httpx.Response(200, json={"data_sources": [{"id": "interviews-source"}]})
        if path == "/v1/data_sources/interviews-source":
            return httpx.Response(200, json=_interview_schema())
        if path == "/v1/data_sources/interviews-source/query":
            assert body["page_size"] == 2
            return httpx.Response(
                200,
                json={
                    "results": [_interview_page("interview-1", "Shopify backend technical")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if path == "/v1/blocks/interview-1/children":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "body-link",
                            "type": "paragraph",
                            "has_children": False,
                            "paragraph": {
                                "rich_text": _rich_text(
                                    "Prep notes https://careers.example.com/shopify-backend"
                                )
                            },
                        }
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if path == "/v1/blocks/bio-page/children":
            return httpx.Response(200, json={"results": [], "has_more": False, "next_cursor": None})
        return httpx.Response(404, json={"message": "unexpected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        jobs = await connector.discover_jobs_workspace(page_size=2)
        academic = await connector.discover_course_assessments(page_size=2)

    assert jobs.jobs_page_id == "jobs-page"
    assert jobs.application_tables[0].has_column_header is True
    assert jobs.application_tables[0].rows[0].is_header is True
    assert jobs.application_tables[0].rows[1].cells == ("Shopify", "Backend Developer", "Applied")
    assert len(jobs.application_tables[0].rows[1].content_fingerprint) == 64
    assert jobs.interviews_database_id == "interviews-db"
    assert jobs.interviews_source_id == "interviews-source"
    assert jobs.interview_title_property_id == "title-prop"
    assert jobs.interview_date_property_id == "date-prop"
    assert jobs.interviews[0].date is not None
    assert jobs.interviews[0].date.start == "2026-10-02T17:30:00.000Z"
    assert {candidate.source_kind for candidate in jobs.interviews[0].url_candidates} == {
        "property",
        "page_body",
    }
    assert [course.course_title for course in academic.courses] == ["BIO 101"]
    assert {diagnostic.course_page_id for diagnostic in academic.diagnostics} == {"bio-page"}
    assert any(request.url.path == "/v1/blocks/interview-1/children" for request in requests)


@pytest.mark.asyncio
async def test_duplicate_jobs_page_is_actionable_diagnostic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/databases/courses-db":
            return httpx.Response(200, json={"data_sources": [{"id": "courses-source"}]})
        if request.url.path == "/v1/data_sources/courses-source/query":
            return httpx.Response(
                200,
                json={
                    "results": [_course_page("jobs-1", "Jobs"), _course_page("jobs-2", "jobs")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        return httpx.Response(404, json={"message": "unexpected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        result = await connector.discover_jobs_workspace()

    assert result.jobs_page_id is None
    assert result.diagnostics[0].code == "jobs_page_duplicate"
    assert result.diagnostics[0].count == 2
    assert result.synced_at.tzinfo is UTC
