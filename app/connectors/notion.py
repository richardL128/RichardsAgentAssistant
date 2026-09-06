"""Least-privilege Notion adapter for academic-planner ingestion."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, cast
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.core.errors import (
    ErrorCategory,
    ErrorCode,
    ErrorRecord,
    LifeAgentError,
    authorization_error,
    permanent_error,
    transient_error,
)

NOTION_API_BASE_URL: Final[str] = "https://api.notion.com/v1"
NOTION_API_VERSION: Final[str] = "2025-09-03"
DatabaseName = Literal["courses", "assessments", "study_blocks"]
NotionSourceType = Literal["database", "data_source"]
DiagnosticSeverity = Literal["info", "warning", "error"]
_DATABASES: Final[frozenset[str]] = frozenset({"courses", "assessments", "study_blocks"})
_REQUIRED_PROPERTIES: Final[dict[str, frozenset[str]]] = {
    "courses": frozenset({"course", "term", "priority", "outline", "policy"}),
    "assessments": frozenset(
        {
            "course",
            "type",
            "due",
            "grade_weight",
            "instructions",
            "rubric",
            "scope",
            "status",
            "estimated_time",
        }
    ),
    "study_blocks": frozenset(
        {"assessment", "planned_duration", "actual_duration", "completion_state", "notes"}
    ),
}
_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ATTACHMENT_HOSTS: Final[tuple[str, ...]] = (
    "prod-files-secure.s3.us-west-2.amazonaws.com",
    "s3.us-west-2.amazonaws.com",
    "prod-files-secure.notion-static.com",
)
_ASSESSMENT_DATABASE_TITLES: Final[frozenset[str]] = frozenset(
    {"assessments", "assessmentcalendar"}
)
MAX_NOTION_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_DISCOVERY_CURSOR_PAGES = 100
MAX_DISCOVERY_RESULTS = 500


class NotionAttachment(BaseModel):
    """A bounded, host-validated attachment reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(default="attachment", min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=4_096)
    mime_type: str | None = Field(default=None, max_length=128)


class NotionDateValue(BaseModel):
    """Normalized Notion date range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: str | None = Field(default=None, max_length=128)
    end: str | None = Field(default=None, max_length=128)
    time_zone: str | None = Field(default=None, max_length=128)


class NotionDiscoveryDiagnostic(BaseModel):
    """Bounded, non-secret discovery diagnostic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1, max_length=80)
    severity: DiagnosticSeverity = "warning"
    message: str = Field(min_length=1, max_length=300)
    source_id: str | None = Field(default=None, max_length=128)
    source_type: NotionSourceType | None = None
    course_page_id: str | None = Field(default=None, max_length=128)
    course_title: str | None = Field(default=None, max_length=255)
    property_id: str | None = Field(default=None, max_length=128)
    property_name: str | None = Field(default=None, max_length=128)
    count: int | None = Field(default=None, ge=0, le=100)


class NotionAssessment(BaseModel):
    """Normalized assessment event discovered from an inline course database."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment_id: str = Field(pattern=_ID_PATTERN.pattern)
    course_id: str = Field(pattern=_ID_PATTERN.pattern)
    course_page_id: str = Field(pattern=_ID_PATTERN.pattern)
    raw_parent_id: str = Field(pattern=_ID_PATTERN.pattern)
    child_database_id: str = Field(pattern=_ID_PATTERN.pattern)
    assessments_source_id: str = Field(pattern=_ID_PATTERN.pattern)
    assessments_source_type: NotionSourceType
    page_id: str = Field(pattern=_ID_PATTERN.pattern)
    source_url: str | None = Field(default=None, max_length=4_096)
    current_title: str = Field(max_length=1_024)
    title_property_id: str = Field(min_length=1, max_length=128)
    title_property_name: str = Field(min_length=1, max_length=128)
    date_property_id: str = Field(min_length=1, max_length=128)
    date_property_name: str = Field(min_length=1, max_length=128)
    due: NotionDateValue | None = None
    last_edited_at: datetime
    archived: bool = False
    in_trash: bool = False
    term: str | None = Field(default=None, max_length=128)
    priority: str | float | int | None = None
    weight: float | int | None = None
    estimated_minutes: float | int | None = None
    status: str | None = Field(default=None, max_length=128)
    properties: Mapping[str, Any]
    attachments: tuple[NotionAttachment, ...] = Field(default=(), max_length=50)


class NotionCourse(BaseModel):
    """Normalized course row and its discovered assessments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    course_id: str = Field(pattern=_ID_PATTERN.pattern)
    course_page_id: str = Field(pattern=_ID_PATTERN.pattern)
    course_title: str = Field(max_length=255)
    raw_parent_id: str = Field(pattern=_ID_PATTERN.pattern)
    courses_source_id: str = Field(pattern=_ID_PATTERN.pattern)
    courses_source_type: NotionSourceType
    source_url: str | None = Field(default=None, max_length=4_096)
    last_edited_at: datetime
    archived: bool = False
    in_trash: bool = False
    term: str | None = Field(default=None, max_length=128)
    priority: str | float | int | None = None
    assessments_database_id: str | None = Field(default=None, max_length=128)
    child_data_source_id: str | None = Field(default=None, max_length=128)
    assessments_source_id: str | None = Field(default=None, max_length=128)
    assessments_source_type: NotionSourceType | None = None
    title_property_id: str | None = Field(default=None, max_length=128)
    title_property_name: str | None = Field(default=None, max_length=128)
    date_property_id: str | None = Field(default=None, max_length=128)
    date_property_name: str | None = Field(default=None, max_length=128)
    assessments: tuple[NotionAssessment, ...] = Field(default=(), max_length=500)
    properties: Mapping[str, Any]


class NotionDiscoveryResult(BaseModel):
    """Complete bounded discovery result for the configured Courses database."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    courses_database_id: str = Field(pattern=_ID_PATTERN.pattern)
    courses_source_id: str | None = Field(default=None, max_length=128)
    courses_source_type: NotionSourceType | None = None
    courses: tuple[NotionCourse, ...] = Field(default=(), max_length=500)
    diagnostics: tuple[NotionDiscoveryDiagnostic, ...] = Field(default=(), max_length=1_000)
    synced_at: datetime


class NotionTitlePrecondition(BaseModel):
    """Current title state used to guard a title-only Notion write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_id: str = Field(pattern=_ID_PATTERN.pattern)
    title_property_id: str = Field(min_length=1, max_length=128)
    title_property_name: str = Field(min_length=1, max_length=128)
    current_title: str = Field(max_length=1_024)
    last_edited_at: datetime
    source_url: str | None = Field(default=None, max_length=4_096)
    archived: bool = False
    in_trash: bool = False


class NotionPage(BaseModel):
    """Metadata and normalized properties for one Notion database page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_id: str = Field(pattern=_ID_PATTERN.pattern)
    database: DatabaseName
    last_edited_at: datetime
    url: str | None = Field(default=None, max_length=4_096)
    properties: Mapping[str, Any]
    attachments: tuple[NotionAttachment, ...] = Field(max_length=50)


class NotionPageBatch(BaseModel):
    """A cursor page from a configured Notion collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database: DatabaseName
    pages: tuple[NotionPage, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=128)
    has_more: bool


class NotionBlockBatch(BaseModel):
    """One bounded page of child blocks for a Notion page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_id: str = Field(pattern=_ID_PATTERN.pattern)
    blocks: tuple[Mapping[str, Any], ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=128)
    has_more: bool


class ConfirmedPropertyChange(BaseModel):
    """One user-confirmed, allowlisted Notion property update."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str = Field(pattern=_ID_PATTERN.pattern)
    confirmation_token: str = Field(min_length=1, max_length=255)
    page_id: str = Field(pattern=_ID_PATTERN.pattern)
    database: DatabaseName
    property_id: str = Field(pattern=_ID_PATTERN.pattern)
    value: Any


class NotionPageTarget(BaseModel):
    """Allowlisted page target associated with a planner assessment/event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(pattern=_ID_PATTERN.pattern)
    page_id: str = Field(pattern=_ID_PATTERN.pattern)
    database: DatabaseName


class NotionWriteReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: str
    page_id: str
    url: str | None = None
    property_id: str | None = None


class NotionWriteConflict(LifeAgentError):  # noqa: N818
    """Raised when a guarded Notion title write would overwrite a user edit."""

    def __init__(
        self,
        diagnostic: str = "Notion page changed since clarification was prepared",
        *,
        current: NotionTitlePrecondition | None = None,
    ) -> None:
        super().__init__(
            ErrorRecord(
                code=ErrorCode.INPUT_INVALID,
                category=ErrorCategory.PERMANENT,
                retryable=False,
                diagnostic=diagnostic,
            )
        )
        self.current = current


class PlannerProposedChange(Protocol):
    """Minimal planner-change shape accepted by the concrete Notion writer."""

    field: str
    value: str
    assessment_id: str | None


def _secret(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _validate_id(value: str, label: str) -> str:
    if not _ID_PATTERN.fullmatch(value):
        raise permanent_error(ErrorCode.INPUT_INVALID, f"Notion {label} is invalid")
    return value


def _validate_page_id(value: str) -> str:
    _validate_id(value.replace("-", ""), "page ID")
    return value


def _validate_attachment_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    allowed = any(
        hostname == suffix or hostname.endswith("." + suffix) for suffix in _ATTACHMENT_HOSTS
    )
    if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
        raise permanent_error(ErrorCode.INPUT_INVALID, "Notion attachment URL is not allowlisted")
    return value


def _edited_after(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("last_edited_after must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_edited(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing edited timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("edited timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _plain_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in cast(list[Any], value):
        if not isinstance(item, Mapping):
            continue
        item_map = cast(Mapping[str, Any], item)
        plain = item_map.get("plain_text")
        if isinstance(plain, str):
            parts.append(plain)
            continue
        text = item_map.get("text")
        if isinstance(text, Mapping):
            content = cast(Mapping[str, Any], text).get("content")
            if isinstance(content, str):
                parts.append(content)
    return "".join(parts).strip()


def _date_value(value: Any) -> NotionDateValue | None:
    if not isinstance(value, Mapping):
        return None
    value_map = cast(Mapping[str, Any], value)
    start = value_map.get("start")
    end = value_map.get("end")
    time_zone = value_map.get("time_zone")
    return NotionDateValue(
        start=start if isinstance(start, str) else None,
        end=end if isinstance(end, str) else None,
        time_zone=time_zone if isinstance(time_zone, str) else None,
    )


def _title_segments(title: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": title}}]


def _property_id(name: str, value: Mapping[str, Any]) -> str:
    prop_id = value.get("id")
    return prop_id if isinstance(prop_id, str) and prop_id else name


def _property_name(name: str, value: Mapping[str, Any]) -> str:
    prop_name = value.get("name")
    return prop_name if isinstance(prop_name, str) and prop_name else name


def _attachments(value: Any) -> tuple[NotionAttachment, ...]:
    found: list[NotionAttachment] = []
    seen: set[str] = set()

    def walk(node: Any, name: str = "attachment") -> None:
        if len(found) >= 50:
            return
        if isinstance(node, Mapping):
            node_map = cast(Mapping[str, Any], node)
            kind: Any = node_map.get("type")
            if kind == "file" or kind == "external":
                source: Any = node_map.get("file") if kind == "file" else node_map.get("external")
                if isinstance(source, Mapping):
                    source_map = cast(Mapping[str, Any], source)
                    url_value: Any = source_map.get("url")
                else:
                    url_value = None
                if isinstance(url_value, str):
                    url = url_value
                    if url not in seen:
                        try:
                            _validate_attachment_url(url)
                            found.append(
                                NotionAttachment(
                                    name=str(node_map.get("name") or name)[:255],
                                    url=url,
                                    mime_type=None,
                                )
                            )
                            seen.add(url)
                        except (ValidationError, LifeAgentError):
                            # Invalid attachment references are ignored; the
                            # page itself remains syncable and auditable.
                            pass
            for key, child in node_map.items():
                walk(child, str(key)[:255])
        elif isinstance(node, list):
            for child in cast(list[Any], node):
                walk(child, name)

    walk(value)
    return tuple(found)


def _normalize_formula(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    value_map = cast(Mapping[str, Any], value)
    kind = value_map.get("type")
    if kind in {"string", "number", "boolean"}:
        return value_map.get(kind)
    if kind == "date":
        return _normalize_value({"type": "date", "date": value_map.get("date")})
    return None


def _normalize_rollup(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    value_map = cast(Mapping[str, Any], value)
    kind = value_map.get("type")
    if kind == "array":
        array = value_map.get("array")
        if not isinstance(array, list):
            return ()
        return tuple(
            _normalize_value(item) for item in cast(list[Any], array) if isinstance(item, Mapping)
        )
    if kind in {"number", "date"}:
        return _normalize_value({"type": kind, kind: value_map.get(kind)})
    return None


def _normalize_value(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    value_map = cast(Mapping[str, Any], value)
    kind = value_map.get("type")
    if kind == "title":
        return _plain_text(value_map.get("title"))
    if kind == "rich_text":
        return _plain_text(value_map.get("rich_text"))
    if kind in {"select", "status"}:
        selected = value_map.get(kind)
        if not isinstance(selected, Mapping):
            return None
        name = cast(Mapping[str, Any], selected).get("name")
        return name if isinstance(name, str) else None
    if kind == "multi_select":
        selected = value_map.get("multi_select")
        if not isinstance(selected, list):
            return ()
        names: list[str] = []
        for item in cast(list[Any], selected):
            if isinstance(item, Mapping):
                name = cast(Mapping[str, Any], item).get("name")
                if isinstance(name, str):
                    names.append(name)
        return tuple(names)
    if kind == "number":
        number = value_map.get("number")
        return number if isinstance(number, int | float) and not isinstance(number, bool) else None
    if kind == "date":
        return _date_value(value_map.get("date"))
    if kind == "relation":
        relation = value_map.get("relation")
        if not isinstance(relation, list):
            return ()
        ids: list[str] = []
        for item in cast(list[Any], relation):
            if isinstance(item, Mapping):
                item_id = cast(Mapping[str, Any], item).get("id")
                if isinstance(item_id, str):
                    ids.append(item_id)
        return tuple(ids)
    if kind == "rollup":
        return _normalize_rollup(value_map.get("rollup"))
    if kind == "formula":
        return _normalize_formula(value_map.get("formula"))
    if kind == "checkbox":
        checked = value_map.get("checkbox")
        return checked if isinstance(checked, bool) else None
    if kind == "url":
        url = value_map.get("url")
        return url if isinstance(url, str) else None
    if kind == "people":
        people = value_map.get("people")
        if not isinstance(people, list):
            return ()
        users: list[dict[str, str]] = []
        for person in cast(list[Any], people):
            if isinstance(person, Mapping):
                person_map = cast(Mapping[str, Any], person)
                user_id = person_map.get("id")
                name = person_map.get("name")
                normalized: dict[str, str] = {}
                if isinstance(user_id, str):
                    normalized["id"] = user_id
                if isinstance(name, str):
                    normalized["name"] = name
                if normalized:
                    users.append(normalized)
        return tuple(users)
    if kind in {"files", "created_time", "last_edited_time", "email", "phone_number"}:
        raw = value_map.get(kind)
        return raw if raw is None or isinstance(raw, str) else None
    if kind is None:
        return None
    return {"unsupported_type": str(kind)[:64]}


def _normalize_properties(properties: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name, value in properties.items():
        if isinstance(value, Mapping):
            normalized[name] = _normalize_value(value)
        else:
            normalized[name] = None
    return normalized


def _find_title_property(properties: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    candidates: list[tuple[str, Mapping[str, Any]]] = []
    for name, value in properties.items():
        if isinstance(value, Mapping) and cast(Mapping[str, Any], value).get("type") == "title":
            candidates.append((name, cast(Mapping[str, Any], value)))
    if not candidates:
        return None
    preferred = {"course", "name", "title"}
    exact = [(name, value) for name, value in candidates if _normalized_name(name) in preferred]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _schema_property(
    properties: Mapping[str, Any], *, expected_name: str, expected_type: str
) -> tuple[str, Mapping[str, Any], str] | None:
    normalized_expected = _normalized_name(expected_name)
    candidates: list[tuple[str, Mapping[str, Any], str]] = []
    for key, value in properties.items():
        if not isinstance(value, Mapping):
            continue
        value_map = cast(Mapping[str, Any], value)
        display_name = _property_name(key, value_map)
        if (
            value_map.get("type") == expected_type
            and _normalized_name(display_name) == normalized_expected
        ):
            candidates.append((_property_id(key, value_map), value_map, display_name))
    if len(candidates) == 1:
        return candidates[0]
    return None


def _notion_value_for_change(change: PlannerProposedChange) -> Any:
    if change.field == "completed":
        normalized = change.value.strip().casefold()
        status = "Completed" if normalized in {"true", "done", "complete", "completed"} else "To Do"
        return {"status": {"name": status}}
    if change.field == "new_deadline":
        return {"date": {"start": change.value.strip()}}
    if change.field == "actual_minutes":
        try:
            minutes = int(change.value)
        except ValueError:
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "actual minutes must be an integer"
            ) from None
        if minutes < 0:
            raise permanent_error(ErrorCode.INPUT_INVALID, "actual minutes must not be negative")
        return {"number": minutes}
    raise permanent_error(
        ErrorCode.INPUT_INVALID, "planner change is not an allowlisted Notion write"
    )


class NotionConnector:
    """Scoped Notion reads for the canonical Courses database."""

    def __init__(
        self,
        *,
        token: SecretStr | str,
        courses_database_id: str | None = None,
        database_ids: Mapping[str, str] | None = None,
        data_source_ids: Mapping[str, str] | None = None,
        property_ids: Mapping[str, Mapping[str, str]] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("Notion timeout must be between 0 and 120 seconds")
        selected_ids = database_ids if database_ids is not None else data_source_ids
        if courses_database_id is None and selected_ids is None:
            raise ValueError("Notion must configure a Courses database ID")
        if (
            courses_database_id is None
            and selected_ids is not None
            and frozenset(selected_ids) != _DATABASES
        ):
            raise ValueError(
                "Notion legacy mapping must cover courses, assessments, and study_blocks"
            )
        self._legacy_collection_endpoint = (
            "data_sources" if data_source_ids is not None and database_ids is None else "databases"
        )
        self._token = token
        self._courses_database_id = _validate_id(
            courses_database_id or cast(Mapping[str, str], selected_ids)["courses"], "database ID"
        )
        self._legacy_ids = {
            name: _validate_id(value, "database ID")
            for name, value in (selected_ids or {"courses": self._courses_database_id}).items()
        }
        self._property_ids = self.validate_property_mapping(property_ids) if property_ids else {}
        self._client = client
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def validate_property_mapping(
        property_ids: Mapping[str, Mapping[str, str]],
    ) -> dict[str, dict[str, str]]:
        """Validate stable Notion property-ID mappings before sync startup."""

        if frozenset(property_ids) != _DATABASES:
            raise ValueError("Notion property mapping must cover all three databases")
        normalized: dict[str, dict[str, str]] = {}
        for database, values in property_ids.items():
            missing = _REQUIRED_PROPERTIES[database] - set(values)
            if missing:
                raise ValueError(f"Notion {database} property mapping is missing required fields")
            normalized[database] = {
                name: _validate_id(value, "property ID") for name, value in values.items()
            }
        return normalized

    async def discover_course_assessments(self, *, page_size: int = 100) -> NotionDiscoveryResult:
        """Discover course rows and their seeded inline Assessments databases."""

        if page_size < 1 or page_size > 100:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion page size is invalid")
        diagnostics: list[NotionDiscoveryDiagnostic] = []
        try:
            courses_database = await self._retrieve_database(self._courses_database_id)
            courses_source = self._unique_source(
                courses_database,
                source_id=self._courses_database_id,
                context="Courses database",
                diagnostics=diagnostics,
            )
        except LifeAgentError:
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="courses_database_unavailable",
                    severity="error",
                    message="Courses database is missing, inaccessible, or not shared",
                    source_id=self._courses_database_id,
                    source_type="database",
                )
            )
            return NotionDiscoveryResult(
                courses_database_id=self._courses_database_id,
                courses=(),
                diagnostics=tuple(diagnostics[:1_000]),
                synced_at=datetime.now(UTC),
            )
        if courses_source is None:
            return NotionDiscoveryResult(
                courses_database_id=self._courses_database_id,
                courses=(),
                diagnostics=tuple(diagnostics[:1_000]),
                synced_at=datetime.now(UTC),
            )

        courses: list[NotionCourse] = []
        async for raw_course in self._query_all_source_pages(
            courses_source[0], source_type=courses_source[1], page_size=page_size
        ):
            try:
                course = await self._discover_course(
                    raw_course,
                    courses_source_id=courses_source[0],
                    courses_source_type=courses_source[1],
                    diagnostics=diagnostics,
                    page_size=page_size,
                )
            except LifeAgentError:
                properties = raw_course.get("properties")
                title_property = (
                    _find_title_property(cast(Mapping[str, Any], properties))
                    if isinstance(properties, Mapping)
                    else None
                )
                title = (
                    _plain_text(title_property[1].get("title"))
                    if title_property is not None
                    else None
                )
                page_id = raw_course.get("id")
                diagnostics.append(
                    NotionDiscoveryDiagnostic(
                        code="course_discovery_failed",
                        severity="error",
                        message="A course calendar could not be discovered and was skipped",
                        source_id=courses_source[0],
                        source_type=courses_source[1],
                        course_page_id=page_id if isinstance(page_id, str) else None,
                        course_title=title[:255] if title else None,
                    )
                )
                continue
            if course is not None:
                courses.append(course)
        return NotionDiscoveryResult(
            courses_database_id=self._courses_database_id,
            courses_source_id=courses_source[0],
            courses_source_type=courses_source[1],
            courses=tuple(courses),
            diagnostics=tuple(diagnostics[:1_000]),
            synced_at=datetime.now(UTC),
        )

    async def retrieve_title_precondition(
        self, page_id: str, title_property_id: str
    ) -> NotionTitlePrecondition:
        """Retrieve current title and edited timestamp for a guarded write."""

        _validate_page_id(page_id)
        if not title_property_id:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion title property is invalid")
        response = await self._request("GET", f"/pages/{quote(page_id, safe='')}", json_body=None)
        data = self._json_object(response, "Notion page")
        properties = data.get("properties")
        if not isinstance(properties, Mapping):
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page")
        title_property = self._property_by_id(
            cast(Mapping[str, Any], properties), title_property_id
        )
        if title_property is None:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion title property is missing")
        name, value = title_property
        if value.get("type") != "title":
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion title property is invalid")
        page_id_value = data.get("id")
        if not isinstance(page_id_value, str):
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page")
        try:
            return NotionTitlePrecondition(
                page_id=_validate_page_id(page_id_value),
                title_property_id=_property_id(name, value),
                title_property_name=_property_name(name, value),
                current_title=_plain_text(value.get("title")),
                last_edited_at=_parse_edited(data.get("last_edited_time")),
                source_url=data.get("url") if isinstance(data.get("url"), str) else None,
                archived=data.get("archived") is True,
                in_trash=data.get("in_trash") is True,
            )
        except (ValueError, ValidationError, LifeAgentError):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page"
            ) from None

    async def rename_assessment_title(
        self,
        *,
        page_id: str,
        title_property_id: str,
        expected_title: str,
        expected_last_edited_at: datetime,
        new_title: str,
    ) -> NotionWriteReceipt:
        """Patch only the discovered title property when the page is unchanged."""

        if not new_title.strip():
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion title must not be empty")
        if len(new_title) > 1_024:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion title is too large")
        expected_edited = expected_last_edited_at
        if expected_edited.tzinfo is None or expected_edited.utcoffset() is None:
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "Notion edited timestamp must be timezone-aware"
            )
        current = await self.retrieve_title_precondition(page_id, title_property_id)
        if (
            current.current_title != expected_title
            or current.last_edited_at != expected_edited.astimezone(UTC)
        ):
            raise NotionWriteConflict(current=current)
        response = await self._request(
            "PATCH",
            f"/pages/{quote(page_id, safe='')}",
            json_body={
                "properties": {
                    title_property_id: {
                        "title": _title_segments(new_title),
                    }
                }
            },
        )
        data = self._json_object(response, "Notion page update")
        patched_id = data.get("id")
        if not isinstance(patched_id, str):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page receipt"
            )
        return NotionWriteReceipt(
            proposal_id="title-rename",
            page_id=_validate_page_id(patched_id),
            url=data.get("url") if isinstance(data.get("url"), str) else None,
            property_id=title_property_id,
        )

    async def query_database(
        self,
        database: DatabaseName,
        *,
        last_edited_after: datetime | None = None,
        start_cursor: str | None = None,
        page_size: int = 100,
    ) -> NotionPageBatch:
        """Return one bounded delta page from a configured legacy collection."""

        if database not in _DATABASES or database not in self._legacy_ids:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion database is not configured")
        if page_size < 1 or page_size > 100:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion page size is invalid")
        if start_cursor is not None:
            _validate_id(start_cursor, "cursor")
        body: dict[str, Any] = {"page_size": page_size}
        if start_cursor is not None:
            body["start_cursor"] = start_cursor
        if last_edited_after is not None:
            body["filter"] = {
                "timestamp": "last_edited_time",
                "last_edited_time": {"after": _edited_after(last_edited_after)},
            }
        response = await self._request(
            "POST",
            (
                f"/{self._legacy_collection_endpoint}/"
                f"{quote(self._legacy_ids[database], safe='')}/query"
            ),
            json_body=body,
        )
        return self._page_batch_from_response(response, database)

    async def retrieve_page(self, page_id: str, *, database: DatabaseName) -> NotionPage:
        """Retrieve one page from a configured database."""

        _validate_id(page_id.replace("-", ""), "page ID")
        if database not in _DATABASES:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion database is not configured")
        response = await self._request("GET", f"/pages/{quote(page_id, safe='')}", json_body=None)
        data = self._json_object(response, "Notion page")
        properties: Any = data.get("properties")
        page_id_value: Any = data.get("id")
        if not isinstance(page_id_value, str) or not isinstance(properties, Mapping):
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page")
        try:
            return NotionPage(
                page_id=_validate_page_id(page_id_value),
                database=database,
                last_edited_at=_parse_edited(data.get("last_edited_time")),
                url=data.get("url") if isinstance(data.get("url"), str) else None,
                properties=_normalize_properties(cast(Mapping[str, Any], properties)),
                attachments=_attachments(properties),
            )
        except (ValueError, ValidationError, LifeAgentError):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page"
            ) from None

    async def retrieve_block_children(
        self, page_id: str, *, start_cursor: str | None = None, page_size: int = 100
    ) -> NotionBlockBatch:
        """Read child blocks for a page without exposing arbitrary endpoints."""

        _validate_page_id(page_id)
        if page_size < 1 or page_size > 100:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion block page size is invalid")
        if start_cursor is not None:
            _validate_id(start_cursor, "cursor")
        query = f"?page_size={page_size}"
        if start_cursor is not None:
            query += f"&start_cursor={quote(start_cursor, safe='')}"
        response = await self._request(
            "GET", f"/blocks/{quote(page_id, safe='')}/children{query}", json_body=None
        )
        data = self._json_object(response, "Notion block query")
        values: Any = data.get("results")
        if not isinstance(values, list) or any(
            not isinstance(value, Mapping) for value in cast(list[Any], values)
        ):
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid blocks")
        cursor = data.get("next_cursor")
        more = data.get("has_more")
        if cursor is not None and not isinstance(cursor, str):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid block cursor"
            )
        if not isinstance(more, bool):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid block pagination"
            )
        try:
            return NotionBlockBatch(
                page_id=page_id,
                blocks=tuple(cast(Mapping[str, Any], value) for value in cast(list[Any], values)),
                next_cursor=cursor,
                has_more=more,
            )
        except ValidationError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion returned too many blocks"
            ) from None

    async def apply_confirmed_change(
        self, change: ConfirmedPropertyChange, *, confirmation_event: str
    ) -> NotionWriteReceipt:
        """Apply exactly one confirmed property change to a configured page."""

        if confirmation_event != change.confirmation_token:
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "Notion confirmation does not match proposal"
            )
        _validate_page_id(change.page_id)
        allowed = self._property_ids.get(change.database, {})
        if change.property_id not in allowed.values():
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion property is not allowlisted")
        try:
            encoded = json.dumps(change.value, ensure_ascii=True, separators=(",", ":"))
        except (TypeError, ValueError):
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "Notion property value is invalid"
            ) from None
        if len(encoded.encode("utf-8")) > 100_000:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion property value is too large")
        response = await self._request(
            "PATCH",
            f"/pages/{quote(change.page_id, safe='')}",
            json_body={"properties": {change.property_id: change.value}},
        )
        data = self._json_object(response, "Notion page update")
        page_id = data.get("id")
        if not isinstance(page_id, str):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page receipt"
            )
        return NotionWriteReceipt(
            proposal_id=change.proposal_id,
            page_id=_validate_page_id(page_id),
            url=data.get("url") if isinstance(data.get("url"), str) else None,
        )

    async def download_attachment(
        self, attachment: NotionAttachment, *, max_bytes: int = MAX_ATTACHMENT_BYTES
    ) -> bytes:
        """Download one signed Notion attachment within a strict byte bound."""

        if max_bytes <= 0 or max_bytes > MAX_ATTACHMENT_BYTES:
            raise ValueError("Notion attachment size limit is invalid")
        _validate_attachment_url(attachment.url)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds))
        try:
            async with client.stream(
                "GET", attachment.url, timeout=self._timeout_seconds
            ) as response:
                if response.status_code in {401, 403}:
                    raise authorization_error("Notion attachment authorization is invalid")
                if response.status_code == 429 or response.status_code >= 500:
                    raise transient_error(
                        ErrorCode.CONNECTOR_TRANSIENT,
                        "Notion attachment is temporarily unavailable",
                    )
                if response.status_code >= 400:
                    raise permanent_error(
                        ErrorCode.INPUT_INVALID, "Notion rejected attachment request"
                    )
                content_length = response.headers.get("content-length")
                if (
                    content_length is not None
                    and content_length.isdigit()
                    and int(content_length) > max_bytes
                ):
                    raise permanent_error(
                        ErrorCode.INPUT_INVALID, "Notion attachment exceeds the size limit"
                    )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise permanent_error(
                            ErrorCode.INPUT_INVALID, "Notion attachment exceeds the size limit"
                        )
                return bytes(body)
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion attachment transport is unavailable"
            ) from None
        finally:
            if owns_client:
                await client.aclose()

    async def _discover_course(
        self,
        raw_course: Mapping[str, Any],
        *,
        courses_source_id: str,
        courses_source_type: NotionSourceType,
        diagnostics: list[NotionDiscoveryDiagnostic],
        page_size: int,
    ) -> NotionCourse | None:
        properties = raw_course.get("properties")
        page_id = raw_course.get("id")
        if not isinstance(page_id, str) or not isinstance(properties, Mapping):
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="course_page_malformed",
                    severity="warning",
                    message="A course row returned by Notion was malformed and was skipped",
                    source_id=courses_source_id,
                    source_type=courses_source_type,
                )
            )
            return None
        try:
            course_page_id = _validate_page_id(page_id)
            edited_at = _parse_edited(raw_course.get("last_edited_time"))
        except (ValueError, LifeAgentError):
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="course_page_malformed",
                    severity="warning",
                    message="A course row returned by Notion had invalid metadata and was skipped",
                    source_id=courses_source_id,
                    source_type=courses_source_type,
                )
            )
            return None
        normalized_properties = _normalize_properties(cast(Mapping[str, Any], properties))
        title_property = _find_title_property(cast(Mapping[str, Any], properties))
        course_title = (
            _plain_text(title_property[1].get("title"))
            if title_property is not None
            else course_page_id
        )
        course_title = course_title or course_page_id
        child_database_ids = await self._assessment_child_database_ids(
            course_page_id, course_title=course_title, diagnostics=diagnostics, page_size=page_size
        )
        assessments: tuple[NotionAssessment, ...] = ()
        child_database_id: str | None = None
        child_source_id: str | None = None
        child_source_type: NotionSourceType | None = None
        title_property_id: str | None = None
        title_property_name: str | None = None
        date_property_id: str | None = None
        date_property_name: str | None = None
        if len(child_database_ids) == 1:
            child_database_id = child_database_ids[0]
            child_source = await self._assessment_source(
                child_database_id,
                course_page_id=course_page_id,
                course_title=course_title,
                diagnostics=diagnostics,
            )
            if child_source is not None:
                child_source_id, child_source_type, title_schema, date_schema = child_source
                title_property_id, title_property_name, _ = title_schema
                date_property_id, date_property_name, _ = date_schema
                found = await self._assessment_pages(
                    child_source_id,
                    child_source_type=child_source_type,
                    child_database_id=child_database_id,
                    course_page_id=course_page_id,
                    course_title=course_title,
                    course_properties=normalized_properties,
                    title_schema=title_schema,
                    date_schema=date_schema,
                    diagnostics=diagnostics,
                    page_size=page_size,
                )
                assessments = tuple(found)
        return NotionCourse(
            course_id=course_page_id,
            course_page_id=course_page_id,
            course_title=course_title[:255],
            raw_parent_id=self._courses_database_id,
            courses_source_id=courses_source_id,
            courses_source_type=courses_source_type,
            source_url=raw_course.get("url") if isinstance(raw_course.get("url"), str) else None,
            last_edited_at=edited_at,
            archived=raw_course.get("archived") is True,
            in_trash=raw_course.get("in_trash") is True,
            term=self._small_text(normalized_properties, "term"),
            priority=self._first_value(normalized_properties, ("priority",)),
            assessments_database_id=child_database_id,
            child_data_source_id=child_source_id,
            assessments_source_id=child_source_id,
            assessments_source_type=child_source_type,
            title_property_id=title_property_id,
            title_property_name=title_property_name,
            date_property_id=date_property_id,
            date_property_name=date_property_name,
            assessments=assessments,
            properties=normalized_properties,
        )

    async def _assessment_child_database_ids(
        self,
        course_page_id: str,
        *,
        course_title: str,
        diagnostics: list[NotionDiscoveryDiagnostic],
        page_size: int,
    ) -> tuple[str, ...]:
        candidate_ids: list[str] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_count = 0
        while True:
            page_count += 1
            if page_count > MAX_DISCOVERY_CURSOR_PAGES:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Notion child discovery exceeded its pagination limit",
                )
            batch = await self.retrieve_block_children(
                course_page_id, start_cursor=cursor, page_size=page_size
            )
            for block in batch.blocks:
                if block.get("type") != "child_database":
                    continue
                child = block.get("child_database")
                title = ""
                if isinstance(child, Mapping):
                    child_title = cast(Mapping[str, Any], child).get("title")
                    title = child_title if isinstance(child_title, str) else ""
                if _normalized_name(title) in _ASSESSMENT_DATABASE_TITLES:
                    block_id = block.get("id")
                    if isinstance(block_id, str):
                        candidate_ids.append(_validate_page_id(block_id))
            if not batch.has_more:
                break
            if batch.next_cursor is None or batch.next_cursor in seen_cursors:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Notion child discovery returned invalid pagination",
                )
            cursor = batch.next_cursor
            seen_cursors.add(cursor)
        if len(candidate_ids) == 0:
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="assessment_calendar_missing",
                    severity="error",
                    message="Course page does not contain a seeded Assessments calendar",
                    course_page_id=course_page_id,
                    course_title=course_title[:255],
                )
            )
        elif len(candidate_ids) > 1:
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="assessment_calendar_duplicate",
                    severity="error",
                    message="Course page contains multiple matching Assessments calendars",
                    course_page_id=course_page_id,
                    course_title=course_title[:255],
                    count=min(len(candidate_ids), 100),
                )
            )
        return tuple(candidate_ids) if len(candidate_ids) == 1 else ()

    async def _assessment_source(
        self,
        child_database_id: str,
        *,
        course_page_id: str,
        course_title: str,
        diagnostics: list[NotionDiscoveryDiagnostic],
    ) -> (
        tuple[
            str,
            NotionSourceType,
            tuple[str, str, str],
            tuple[str, str, str],
        ]
        | None
    ):
        try:
            child_database = await self._retrieve_database(child_database_id)
            source = self._unique_source(
                child_database,
                source_id=child_database_id,
                context="Assessments child database",
                diagnostics=diagnostics,
                course_page_id=course_page_id,
                course_title=course_title,
            )
            if source is None:
                return None
            source_schema = (
                await self._retrieve_data_source(source[0])
                if source[1] == "data_source"
                else child_database
            )
        except LifeAgentError:
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="assessment_calendar_inaccessible",
                    severity="error",
                    message="Assessments calendar is inaccessible or not shared",
                    source_id=child_database_id,
                    source_type="database",
                    course_page_id=course_page_id,
                    course_title=course_title[:255],
                )
            )
            return None
        properties = source_schema.get("properties")
        if not isinstance(properties, Mapping):
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="assessment_schema_malformed",
                    severity="error",
                    message="Assessments calendar schema is malformed",
                    source_id=source[0],
                    source_type=source[1],
                    course_page_id=course_page_id,
                    course_title=course_title[:255],
                )
            )
            return None
        title_property = _schema_property(
            cast(Mapping[str, Any], properties), expected_name="Name", expected_type="title"
        )
        date_property = _schema_property(
            cast(Mapping[str, Any], properties), expected_name="Date", expected_type="date"
        )
        if title_property is None:
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="assessment_name_property_invalid",
                    severity="error",
                    message="Assessments calendar must have exactly one title property named Name",
                    source_id=source[0],
                    source_type=source[1],
                    course_page_id=course_page_id,
                    course_title=course_title[:255],
                )
            )
            return None
        if date_property is None:
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="assessment_date_property_invalid",
                    severity="error",
                    message="Assessments calendar must have exactly one date property named Date",
                    source_id=source[0],
                    source_type=source[1],
                    course_page_id=course_page_id,
                    course_title=course_title[:255],
                )
            )
            return None
        return (
            source[0],
            source[1],
            (title_property[0], title_property[2], "title"),
            (date_property[0], date_property[2], "date"),
        )

    async def _assessment_pages(
        self,
        source_id: str,
        *,
        child_source_type: NotionSourceType,
        child_database_id: str,
        course_page_id: str,
        course_title: str,
        course_properties: Mapping[str, Any],
        title_schema: tuple[str, str, str],
        date_schema: tuple[str, str, str],
        diagnostics: list[NotionDiscoveryDiagnostic],
        page_size: int,
    ) -> list[NotionAssessment]:
        assessments: list[NotionAssessment] = []
        async for raw_page in self._query_all_source_pages(
            source_id, source_type=child_source_type, page_size=page_size
        ):
            assessment = self._normalize_assessment(
                raw_page,
                source_id=source_id,
                source_type=child_source_type,
                child_database_id=child_database_id,
                course_page_id=course_page_id,
                course_title=course_title,
                course_properties=course_properties,
                title_schema=title_schema,
                date_schema=date_schema,
                diagnostics=diagnostics,
            )
            if assessment is not None:
                assessments.append(assessment)
        return assessments

    def _normalize_assessment(
        self,
        raw_page: Mapping[str, Any],
        *,
        source_id: str,
        source_type: NotionSourceType,
        child_database_id: str,
        course_page_id: str,
        course_title: str,
        course_properties: Mapping[str, Any],
        title_schema: tuple[str, str, str],
        date_schema: tuple[str, str, str],
        diagnostics: list[NotionDiscoveryDiagnostic],
    ) -> NotionAssessment | None:
        properties = raw_page.get("properties")
        page_id = raw_page.get("id")
        if not isinstance(page_id, str) or not isinstance(properties, Mapping):
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="assessment_page_malformed",
                    severity="warning",
                    message="Assessment page returned by Notion was malformed and was skipped",
                    source_id=source_id,
                    source_type=source_type,
                    course_page_id=course_page_id,
                    course_title=course_title[:255],
                )
            )
            return None
        title_id, title_name, _ = title_schema
        date_id, date_name, _ = date_schema
        title_value = self._property_by_id(cast(Mapping[str, Any], properties), title_id)
        date_value = self._property_by_id(cast(Mapping[str, Any], properties), date_id)
        if title_value is None or date_value is None:
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="assessment_page_missing_required_property",
                    severity="warning",
                    message="Assessment page is missing the required Name or Date property",
                    source_id=source_id,
                    source_type=source_type,
                    course_page_id=course_page_id,
                    course_title=course_title[:255],
                )
            )
            return None
        try:
            normalized_properties = _normalize_properties(cast(Mapping[str, Any], properties))
            status = self._small_text(normalized_properties, "status")
            return NotionAssessment(
                assessment_id=_validate_page_id(page_id),
                course_id=course_page_id,
                course_page_id=course_page_id,
                raw_parent_id=self._courses_database_id,
                child_database_id=child_database_id,
                assessments_source_id=source_id,
                assessments_source_type=source_type,
                page_id=_validate_page_id(page_id),
                source_url=raw_page.get("url") if isinstance(raw_page.get("url"), str) else None,
                current_title=str(_normalize_value(title_value[1]))[:1_024],
                title_property_id=title_id,
                title_property_name=title_name,
                date_property_id=date_id,
                date_property_name=date_name,
                due=_date_value(date_value[1].get("date")),
                last_edited_at=_parse_edited(raw_page.get("last_edited_time")),
                archived=raw_page.get("archived") is True,
                in_trash=raw_page.get("in_trash") is True,
                term=self._small_text(course_properties, "term"),
                priority=self._first_value(course_properties, ("priority",)),
                weight=self._number_value(
                    normalized_properties, ("grade_weight", "gradeweight", "weight")
                ),
                estimated_minutes=self._number_value(
                    normalized_properties, ("estimated_time", "estimatedtime", "estimatedminutes")
                ),
                status=status,
                properties=normalized_properties,
                attachments=_attachments(properties),
            )
        except (ValueError, ValidationError, LifeAgentError):
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="assessment_page_malformed",
                    severity="warning",
                    message=(
                        "Assessment page returned by Notion had invalid metadata and was skipped"
                    ),
                    source_id=source_id,
                    source_type=source_type,
                    course_page_id=course_page_id,
                    course_title=course_title[:255],
                )
            )
            return None

    async def _retrieve_database(self, database_id: str) -> dict[str, Any]:
        response = await self._request(
            "GET", f"/databases/{quote(database_id, safe='')}", json_body=None
        )
        return self._json_object(response, "Notion database")

    async def _retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        response = await self._request(
            "GET", f"/data_sources/{quote(data_source_id, safe='')}", json_body=None
        )
        return self._json_object(response, "Notion data source")

    async def _query_all_source_pages(
        self, source_id: str, *, source_type: NotionSourceType, page_size: int
    ) -> AsyncIterator[Mapping[str, Any]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        page_count = 0
        result_count = 0
        while True:
            page_count += 1
            if page_count > MAX_DISCOVERY_CURSOR_PAGES:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Notion discovery exceeded its pagination limit",
                )
            body: dict[str, Any] = {"page_size": page_size}
            if cursor is not None:
                body["start_cursor"] = cursor
            endpoint = "data_sources" if source_type == "data_source" else "databases"
            response = await self._request(
                "POST", f"/{endpoint}/{quote(source_id, safe='')}/query", json_body=body
            )
            data = self._json_object(response, "Notion data source query")
            results = data.get("results")
            if not isinstance(results, list):
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid pages"
                )
            for page in cast(list[Any], results):
                result_count += 1
                if result_count > MAX_DISCOVERY_RESULTS:
                    raise transient_error(
                        ErrorCode.CONNECTOR_TRANSIENT,
                        "Notion discovery exceeded its result limit",
                    )
                if isinstance(page, Mapping):
                    yield cast(Mapping[str, Any], page)
                else:
                    raise transient_error(
                        ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page"
                    )
            has_more = data.get("has_more")
            next_cursor = data.get("next_cursor")
            if not isinstance(has_more, bool) or (
                next_cursor is not None and not isinstance(next_cursor, str)
            ):
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid pagination"
                )
            if not has_more:
                return
            if next_cursor is None or next_cursor in seen_cursors:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Notion discovery returned invalid pagination",
                )
            cursor = next_cursor
            seen_cursors.add(cursor)

    def _unique_source(
        self,
        container: Mapping[str, Any],
        *,
        source_id: str,
        context: str,
        diagnostics: list[NotionDiscoveryDiagnostic],
        course_page_id: str | None = None,
        course_title: str | None = None,
    ) -> tuple[str, NotionSourceType] | None:
        data_sources = container.get("data_sources")
        if isinstance(data_sources, list):
            ids: list[str] = []
            for item in cast(list[Any], data_sources):
                if not isinstance(item, Mapping):
                    continue
                item_id = cast(Mapping[str, Any], item).get("id")
                if isinstance(item_id, str):
                    ids.append(item_id)
            unique_ids = tuple(dict.fromkeys(ids))
            if len(unique_ids) == 1:
                return unique_ids[0], "data_source"
            diagnostics.append(
                NotionDiscoveryDiagnostic(
                    code="data_source_missing" if len(unique_ids) == 0 else "data_source_duplicate",
                    severity="error",
                    message=f"{context} must expose exactly one data source",
                    source_id=source_id,
                    source_type="database",
                    course_page_id=course_page_id,
                    course_title=course_title[:255] if course_title else None,
                    count=min(len(unique_ids), 100),
                )
            )
            return None
        if isinstance(container.get("properties"), Mapping):
            return source_id, "database"
        diagnostics.append(
            NotionDiscoveryDiagnostic(
                code="data_source_missing",
                severity="error",
                message=f"{context} did not expose a usable data source",
                source_id=source_id,
                source_type="database",
                course_page_id=course_page_id,
                course_title=course_title[:255] if course_title else None,
            )
        )
        return None

    def _page_batch_from_response(
        self, response: httpx.Response, database: DatabaseName
    ) -> NotionPageBatch:
        data = self._json_object(response, "Notion database query")
        raw_pages = data.get("results")
        if not isinstance(raw_pages, list):
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid pages")
        pages: list[NotionPage] = []
        for raw in cast(list[Any], raw_pages):
            if not isinstance(raw, Mapping):
                raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page")
            raw_page = cast(Mapping[str, Any], raw)
            page_id: Any = raw_page.get("id")
            properties: Any = raw_page.get("properties")
            if not isinstance(page_id, str) or not isinstance(properties, Mapping):
                raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page")
            try:
                pages.append(
                    NotionPage(
                        page_id=_validate_page_id(page_id),
                        database=database,
                        last_edited_at=_parse_edited(raw_page.get("last_edited_time")),
                        url=raw_page.get("url") if isinstance(raw_page.get("url"), str) else None,
                        properties=_normalize_properties(cast(Mapping[str, Any], properties)),
                        attachments=_attachments(properties),
                    )
                )
            except (ValueError, ValidationError, LifeAgentError):
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page"
                ) from None
        next_cursor = data.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid cursor")
        has_more = data.get("has_more")
        if not isinstance(has_more, bool):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid pagination"
            )
        return NotionPageBatch(
            database=database,
            pages=tuple(pages),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _property_by_id(
        self, properties: Mapping[str, Any], property_id: str
    ) -> tuple[str, Mapping[str, Any]] | None:
        for name, value in properties.items():
            if not isinstance(value, Mapping):
                continue
            value_map = cast(Mapping[str, Any], value)
            if _property_id(name, value_map) == property_id or name == property_id:
                return name, value_map
        return None

    @staticmethod
    def _small_text(properties: Mapping[str, Any], expected_name: str) -> str | None:
        normalized_expected = _normalized_name(expected_name)
        for name, value in properties.items():
            if _normalized_name(name) == normalized_expected and isinstance(value, str):
                return value[:128]
        return None

    @staticmethod
    def _first_value(properties: Mapping[str, Any], expected_names: Sequence[str]) -> Any:
        expected = {_normalized_name(name) for name in expected_names}
        for name, value in properties.items():
            if _normalized_name(name) in expected:
                return value
        return None

    @staticmethod
    def _number_value(
        properties: Mapping[str, Any], expected_names: Sequence[str]
    ) -> float | int | None:
        value = NotionConnector._first_value(properties, expected_names)
        return value if isinstance(value, int | float) and not isinstance(value, bool) else None

    async def _request(
        self,
        method: Literal["GET", "POST", "PATCH"],
        path: str,
        *,
        json_body: Mapping[str, Any] | None,
    ) -> httpx.Response:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds))
        headers = {
            "Authorization": f"Bearer {_secret(self._token)}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        }
        try:
            response = await client.request(
                method,
                f"{NOTION_API_BASE_URL}{path}",
                headers=headers,
                json=json_body,
                timeout=self._timeout_seconds,
            )
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion transport is unavailable"
            ) from None
        finally:
            if owns_client:
                await client.aclose()
        if len(response.content) > MAX_NOTION_RESPONSE_BYTES:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion response exceeds the size limit")
        if response.status_code in {401, 403}:
            raise authorization_error("Notion authorization is invalid")
        if response.status_code == 429 or response.status_code >= 500:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, "Notion is temporarily unavailable"
            )
        if response.status_code >= 400:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion rejected the request")
        return response

    @staticmethod
    def _json_object(response: httpx.Response, diagnostic: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, diagnostic + " response is invalid"
            ) from None
        if not isinstance(data, dict):
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT, diagnostic + " response is invalid"
            )
        return cast(dict[str, Any], data)


# Friendly name used by integrations that call all source adapters connectors.
NotionAppConnector = NotionConnector


class AcademicNotionWriter:
    """Apply exactly confirmed planner changes to allowlisted Notion properties."""

    _FIELD_TO_PROPERTY: Final[dict[str, tuple[DatabaseName, str]]] = {
        "completed": ("assessments", "status"),
        "new_deadline": ("assessments", "due"),
        "actual_minutes": ("study_blocks", "actual_duration"),
    }

    def __init__(
        self,
        *,
        connector: NotionConnector,
        targets: Mapping[str, NotionPageTarget],
        property_ids: Mapping[str, Mapping[str, str]],
    ) -> None:
        self._connector = connector
        self._targets = dict(targets)
        self._property_ids = NotionConnector.validate_property_mapping(property_ids)

    async def apply_confirmed_changes(
        self,
        changes: Sequence[PlannerProposedChange],
        *,
        proposal_id: Any,
        confirmation_event: str,
    ) -> None:
        """Patch each change only with the exact confirmation event attached."""

        if not confirmation_event.strip():
            raise permanent_error(ErrorCode.INPUT_INVALID, "confirmation event is required")
        for change in changes:
            target_key = change.assessment_id
            if target_key is None:
                raise permanent_error(
                    ErrorCode.INPUT_INVALID, "Notion target is required for this change"
                )
            target = self._targets.get(target_key)
            if target is None:
                raise permanent_error(ErrorCode.INPUT_INVALID, "Notion target is not allowlisted")
            database, property_name = self._database_property_for(change)
            if target.database != database:
                raise permanent_error(
                    ErrorCode.INPUT_INVALID, "Notion target database does not match change"
                )
            property_id = self._property_ids[database][property_name]
            await self._connector.apply_confirmed_change(
                ConfirmedPropertyChange(
                    proposal_id=str(proposal_id),
                    confirmation_token=confirmation_event,
                    page_id=target.page_id,
                    database=database,
                    property_id=property_id,
                    value=_notion_value_for_change(change),
                ),
                confirmation_event=confirmation_event,
            )

    def _database_property_for(self, change: PlannerProposedChange) -> tuple[DatabaseName, str]:
        mapped = self._FIELD_TO_PROPERTY.get(change.field)
        if mapped is None:
            raise permanent_error(
                ErrorCode.INPUT_INVALID, "planner change is not an allowlisted Notion write"
            )
        return mapped


__all__ = [
    "MAX_ATTACHMENT_BYTES",
    "NOTION_API_BASE_URL",
    "NOTION_API_VERSION",
    "AcademicNotionWriter",
    "ConfirmedPropertyChange",
    "DatabaseName",
    "NotionAppConnector",
    "NotionAssessment",
    "NotionAttachment",
    "NotionBlockBatch",
    "NotionConnector",
    "NotionCourse",
    "NotionDateValue",
    "NotionDiscoveryDiagnostic",
    "NotionDiscoveryResult",
    "NotionPage",
    "NotionPageBatch",
    "NotionPageTarget",
    "NotionTitlePrecondition",
    "NotionWriteConflict",
    "NotionWriteReceipt",
    "PlannerProposedChange",
]
