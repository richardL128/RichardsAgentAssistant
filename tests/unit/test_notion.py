"""Contract tests for the scoped Notion adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.agents.academic_planner.contracts import ProposedChange
from app.connectors.notion import (
    AcademicNotionWriter,
    ConfirmedPropertyChange,
    NotionAttachment,
    NotionConnector,
    NotionPageTarget,
    NotionWriteConflict,
)
from app.core.errors import LifeAgentError

IDS = {"courses": "courses-id", "assessments": "assessments-id", "study_blocks": "blocks-id"}
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
                "type": "title",
                "title": [{"plain_text": title}],
            },
            "Term": {"id": "term", "type": "select", "select": {"name": "Fall 2026"}},
            "Priority": {"id": "priority", "type": "number", "number": 2},
        },
    }


def _assessment_page(
    page_id: str,
    title: str,
    *,
    archived: bool = False,
    in_trash: bool = False,
) -> dict[str, object]:
    return {
        "id": page_id,
        "last_edited_time": EDITED,
        "url": f"https://www.notion.so/{page_id}",
        "archived": archived,
        "in_trash": in_trash,
        "properties": {
            "Name": _title_property(title),
            "Date": _date_property("2026-10-01"),
            "Notes": {
                "id": "notes",
                "type": "rich_text",
                "rich_text": [{"plain_text": "Read chapter 4"}],
            },
            "Kind": {"id": "kind", "type": "select", "select": {"name": "Quiz"}},
            "Tags": {
                "id": "tags",
                "type": "multi_select",
                "multi_select": [{"name": "graded"}, {"name": "short"}],
            },
            "Points": {"id": "points", "type": "number", "number": 10},
            "Related": {"id": "related", "type": "relation", "relation": [{"id": "page-x"}]},
            "Roll": {
                "id": "roll",
                "type": "rollup",
                "rollup": {"type": "array", "array": [{"type": "number", "number": 5}]},
            },
            "Formula": {
                "id": "formula",
                "type": "formula",
                "formula": {"type": "string", "string": "derived"},
            },
            "Done": {"id": "done", "type": "checkbox", "checkbox": True},
            "Link": {"id": "link", "type": "url", "url": "https://example.edu/assignment"},
            "People": {
                "id": "people",
                "type": "people",
                "people": [{"id": "user-1", "name": "Instructor"}],
            },
            "EmptySelect": {"id": "empty", "type": "select", "select": None},
            "Unsupported": {"id": "unsupported", "type": "unique_id", "unique_id": {"number": 1}},
            "Malformed": "not-a-property",
        },
    }


def _assessment_schema(
    *,
    date_property: dict[str, object] | None = None,
    title_property: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": "assessments-source-1",
        "properties": {
            "Name": title_property
            or {"id": "title-prop", "name": "Name", "type": "title", "title": {}},
            "Date": date_property
            or {"id": "date-prop", "name": "Date", "type": "date", "date": {}},
        },
    }


def _legacy_properties() -> dict[str, dict[str, str]]:
    return {
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


@pytest.mark.asyncio
async def test_discovers_nested_assessments_with_pagination_and_normalization() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        body = json.loads(request.content or b"{}")
        if path == "/v1/databases/courses-db":
            return httpx.Response(200, json={"data_sources": [{"id": "courses-source"}]})
        if path == "/v1/data_sources/courses-source/query":
            if body.get("start_cursor") == "course-cursor":
                return httpx.Response(
                    200,
                    json={
                        "results": [_course_page("course-2", "HIST 202")],
                        "has_more": False,
                        "next_cursor": None,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [_course_page("course-1", "BIO 101")],
                    "has_more": True,
                    "next_cursor": "course-cursor",
                },
            )
        if path == "/v1/blocks/course-1/children":
            if request.url.params.get("start_cursor") == "block-cursor":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "child-db-1",
                                "type": "child_database",
                                "child_database": {"title": "Assessments"},
                            }
                        ],
                        "has_more": False,
                        "next_cursor": None,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [{"id": "block-1", "type": "paragraph"}],
                    "has_more": True,
                    "next_cursor": "block-cursor",
                },
            )
        if path == "/v1/blocks/course-2/children":
            return httpx.Response(200, json={"results": [], "has_more": False, "next_cursor": None})
        if path == "/v1/databases/child-db-1":
            return httpx.Response(200, json={"data_sources": [{"id": "assessments-source-1"}]})
        if path == "/v1/data_sources/assessments-source-1":
            return httpx.Response(200, json=_assessment_schema())
        if path == "/v1/data_sources/assessments-source-1/query":
            if body.get("start_cursor") == "assessment-cursor":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            _assessment_page(
                                "assessment-2",
                                "Project 1",
                                archived=True,
                                in_trash=True,
                            )
                        ],
                        "has_more": False,
                        "next_cursor": None,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [_assessment_page("assessment-1", "Chapter 4")],
                    "has_more": True,
                    "next_cursor": "assessment-cursor",
                },
            )
        return httpx.Response(404, json={"message": "unexpected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        result = await connector.discover_course_assessments(page_size=1)

    assert [course.course_title for course in result.courses] == ["BIO 101", "HIST 202"]
    assert result.courses_source_id == "courses-source"
    assert result.diagnostics[0].code == "assessment_calendar_missing"
    course = result.courses[0]
    assert course.assessments_database_id == "child-db-1"
    assert course.child_data_source_id == "assessments-source-1"
    assert course.assessments_source_id == "assessments-source-1"
    assert course.title_property_id == "title-prop"
    assert course.title_property_name == "Name"
    assert course.date_property_id == "date-prop"
    assert course.date_property_name == "Date"
    assert course.term == "Fall 2026"
    assert course.priority == 2
    assert len(course.assessments) == 2
    assessment = course.assessments[0]
    assert assessment.current_title == "Chapter 4"
    assert assessment.due is not None
    assert assessment.due.start == "2026-10-01"
    assert assessment.term == "Fall 2026"
    assert assessment.weight is None
    assert assessment.properties["Notes"] == "Read chapter 4"
    assert assessment.properties["Kind"] == "Quiz"
    assert assessment.properties["Tags"] == ("graded", "short")
    assert assessment.properties["Points"] == 10
    assert assessment.properties["Related"] == ("page-x",)
    assert assessment.properties["Roll"] == (5,)
    assert assessment.properties["Formula"] == "derived"
    assert assessment.properties["Done"] is True
    assert assessment.properties["People"] == ({"id": "user-1", "name": "Instructor"},)
    assert assessment.properties["EmptySelect"] is None
    assert assessment.properties["Unsupported"] == {"unsupported_type": "unique_id"}
    assert assessment.properties["Malformed"] is None
    assert course.assessments[1].archived is True
    assert course.assessments[1].in_trash is True
    assert all(request.headers["Notion-Version"] == "2025-09-03" for request in requests)
    assert requests[0].url.path == "/v1/databases/courses-db"
    course_queries = [
        request
        for request in requests
        if request.url.path == "/v1/data_sources/courses-source/query"
    ]
    assert len(course_queries) == 2
    assert json.loads(course_queries[1].content)["start_cursor"] == "course-cursor"
    assert any(request.url.path == "/v1/blocks/course-1/children" for request in requests)
    assert any(
        request.url.path == "/v1/data_sources/assessments-source-1/query" for request in requests
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blocks", "code"),
    [
        ([], "assessment_calendar_missing"),
        (
            [
                {
                    "id": "child-db-1",
                    "type": "child_database",
                    "child_database": {"title": "Assessments"},
                },
                {
                    "id": "child-db-2",
                    "type": "child_database",
                    "child_database": {"title": "Assessment Calendar"},
                },
            ],
            "assessment_calendar_duplicate",
        ),
    ],
)
async def test_missing_or_duplicate_child_calendar_is_a_diagnostic(
    blocks: list[dict[str, object]], code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/databases/courses-db":
            return httpx.Response(200, json={"data_sources": [{"id": "courses-source"}]})
        if request.url.path == "/v1/data_sources/courses-source/query":
            return httpx.Response(
                200,
                json={
                    "results": [_course_page("course-1", "BIO 101")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if request.url.path == "/v1/blocks/course-1/children":
            return httpx.Response(
                200,
                json={"results": blocks, "has_more": False, "next_cursor": None},
            )
        return httpx.Response(404, json={"message": "unexpected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        result = await connector.discover_course_assessments()

    assert result.courses[0].assessments == ()
    assert result.diagnostics[0].code == code
    rendered = result.model_dump_json()
    assert "unexpected" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "code"),
    [
        (
            _assessment_schema(date_property={"id": "date-prop", "name": "Due", "type": "date"}),
            "assessment_date_property_invalid",
        ),
        (
            _assessment_schema(
                date_property={"id": "date-prop", "name": "Date", "type": "rich_text"}
            ),
            "assessment_date_property_invalid",
        ),
        (
            _assessment_schema(
                title_property={"id": "title-prop", "name": "Title", "type": "title"}
            ),
            "assessment_name_property_invalid",
        ),
    ],
)
async def test_invalid_assessment_schema_is_a_diagnostic(
    schema: dict[str, object], code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/databases/courses-db":
            return httpx.Response(200, json={"data_sources": [{"id": "courses-source"}]})
        if request.url.path == "/v1/data_sources/courses-source/query":
            return httpx.Response(
                200,
                json={
                    "results": [_course_page("course-1", "BIO 101")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if request.url.path == "/v1/blocks/course-1/children":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "child-db-1",
                            "type": "child_database",
                            "child_database": {"title": "Assessments"},
                        }
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if request.url.path == "/v1/databases/child-db-1":
            return httpx.Response(200, json={"data_sources": [{"id": "assessments-source-1"}]})
        if request.url.path == "/v1/data_sources/assessments-source-1":
            return httpx.Response(200, json=schema)
        return httpx.Response(404, json={"message": "unexpected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        result = await connector.discover_course_assessments()

    assert result.courses[0].assessments == ()
    assert result.diagnostics[0].code == code


@pytest.mark.asyncio
async def test_malformed_top_level_query_raises_safe_error_without_raw_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/databases/courses-db":
            return httpx.Response(200, json={"data_sources": [{"id": "courses-source"}]})
        if request.url.path == "/v1/data_sources/courses-source/query":
            return httpx.Response(
                200,
                json={"results": "RAW_VENDOR_BODY_SHOULD_NOT_LEAK", "has_more": False},
            )
        return httpx.Response(404, json={"message": "RAW_VENDOR_BODY_SHOULD_NOT_LEAK"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        with pytest.raises(LifeAgentError) as raised:
            await connector.discover_course_assessments()

    assert raised.value.record.diagnostic == "Notion returned invalid pages"
    assert "RAW_VENDOR_BODY_SHOULD_NOT_LEAK" not in raised.value.record.diagnostic


@pytest.mark.asyncio
async def test_discovery_rejects_missing_or_repeated_pagination_cursor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/databases/courses-db":
            return httpx.Response(200, json={"data_sources": [{"id": "courses-source"}]})
        return httpx.Response(
            200,
            json={"results": [], "has_more": True, "next_cursor": None},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        with pytest.raises(LifeAgentError) as raised:
            await connector.discover_course_assessments()

    assert raised.value.record.diagnostic == "Notion discovery returned invalid pagination"


@pytest.mark.asyncio
async def test_inaccessible_courses_database_returns_setup_diagnostic_without_raw_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "RAW_VENDOR_BODY_SHOULD_NOT_LEAK"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        result = await connector.discover_course_assessments()

    assert result.courses == ()
    assert result.diagnostics[0].code == "courses_database_unavailable"
    assert "RAW_VENDOR_BODY_SHOULD_NOT_LEAK" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_one_course_discovery_failure_does_not_discard_other_courses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/databases/courses-db":
            return httpx.Response(200, json={"data_sources": [{"id": "courses-source"}]})
        if request.url.path == "/v1/data_sources/courses-source/query":
            return httpx.Response(
                200,
                json={
                    "results": [
                        _course_page("course-failed", "PRIVATE COURSE"),
                        _course_page("course-valid", "BIO 101"),
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if request.url.path == "/v1/blocks/course-failed/children":
            return httpx.Response(500, json={"message": "RAW_VENDOR_BODY_SHOULD_NOT_LEAK"})
        if request.url.path == "/v1/blocks/course-valid/children":
            return httpx.Response(
                200,
                json={"results": [], "has_more": False, "next_cursor": None},
            )
        return httpx.Response(404, json={"message": "unexpected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        result = await connector.discover_course_assessments()

    assert [course.course_page_id for course in result.courses] == ["course-valid"]
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "course_discovery_failed",
        "assessment_calendar_missing",
    }
    assert "RAW_VENDOR_BODY_SHOULD_NOT_LEAK" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_guarded_title_rename_patches_only_title_property() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "assessment-1",
                    "last_edited_time": EDITED,
                    "url": "https://www.notion.so/assessment-1",
                    "archived": False,
                    "in_trash": False,
                    "properties": {
                        "Name": _title_property("Chapter 4"),
                        "Date": _date_property("2026-10-01"),
                    },
                },
            )
        return httpx.Response(200, json={"id": "assessment-1", "url": "https://www.notion.so/a"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        receipt = await connector.rename_assessment_title(
            page_id="assessment-1",
            title_property_id="title-prop",
            expected_title="Chapter 4",
            expected_last_edited_at=EDITED_AT,
            new_title="Quiz - Chapter 4",
        )

    assert receipt.page_id == "assessment-1"
    assert [request.method for request in requests] == ["GET", "PATCH"]
    body = json.loads(requests[1].content)
    assert body == {
        "properties": {
            "title-prop": {"title": [{"type": "text", "text": {"content": "Quiz - Chapter 4"}}]}
        }
    }
    assert "Date" not in requests[1].content.decode()


@pytest.mark.asyncio
async def test_guarded_title_rename_rejects_concurrent_edit_without_patch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "assessment-1",
                "last_edited_time": "2026-09-03T12:01:00.000Z",
                "properties": {"Name": _title_property("User changed")},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
        with pytest.raises(NotionWriteConflict):
            await connector.rename_assessment_title(
                page_id="assessment-1",
                title_property_id="title-prop",
                expected_title="Chapter 4",
                expected_last_edited_at=EDITED_AT,
                new_title="Quiz - Chapter 4",
            )

    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
async def test_attachment_download_is_bounded_and_rejects_other_hosts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"pdf")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(token="secret", courses_database_id="courses-db", client=client)
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

    properties = _legacy_properties()
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


@pytest.mark.asyncio
async def test_academic_writer_maps_exact_confirmation_to_allowlisted_patch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"id": "assignment-page-1", "url": "https://www.notion.so/page-1"}
        )

    properties = _legacy_properties()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = NotionConnector(
            token="secret", database_ids=IDS, property_ids=properties, client=client
        )
        writer = AcademicNotionWriter(
            connector=connector,
            targets={
                "notion-assignment-1": NotionPageTarget(
                    target_id="notion-assignment-1",
                    page_id="assignment-page-1",
                    database="assessments",
                )
            },
            property_ids=properties,
        )
        await writer.apply_confirmed_changes(
            (ProposedChange(field="completed", value="true", assessment_id="notion-assignment-1"),),
            proposal_id="proposal-1",
            confirmation_event="CONFIRM ACADEMIC proposal-1",
        )

    assert len(requests) == 1
    assert requests[0].method == "PATCH"
    assert requests[0].url.path == "/v1/pages/assignment-page-1"
    body = requests[0].content.decode()
    assert "assessments-status" in body
    assert "Completed" in body
