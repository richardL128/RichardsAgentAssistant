"""Least-privilege Notion adapter for academic-planner ingestion."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from app.core.errors import (
    ErrorCode,
    LifeAgentError,
    authorization_error,
    permanent_error,
    transient_error,
)

if TYPE_CHECKING:
    pass

NOTION_API_BASE_URL: Final[str] = "https://api.notion.com/v1"
NOTION_API_VERSION: Final[str] = "2022-06-28"
DatabaseName = Literal["courses", "assessments", "study_blocks"]
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
MAX_NOTION_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class NotionAttachment(BaseModel):
    """A bounded, host-validated attachment reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(default="attachment", min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=4_096)
    mime_type: str | None = Field(default=None, max_length=128)


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
    """A cursor page from one of the three configured databases."""

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

    # Property names are useful fallback names for files without a title.
    walk(value)
    return tuple(found)


class NotionConnector:
    """Scoped Notion reads for the three planner databases."""

    def __init__(
        self,
        *,
        token: SecretStr | str,
        database_ids: Mapping[str, str] | None = None,
        data_source_ids: Mapping[str, str] | None = None,
        property_ids: Mapping[str, Mapping[str, str]] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("Notion timeout must be between 0 and 120 seconds")
        selected_ids = database_ids if database_ids is not None else data_source_ids
        if selected_ids is None or frozenset(selected_ids) != _DATABASES:
            raise ValueError("Notion must configure courses, assessments, and study_blocks")
        self._collection_endpoint = (
            "data_sources" if data_source_ids is not None and database_ids is None else "databases"
        )
        self._token = token
        self._database_ids = {
            name: _validate_id(value, "database ID") for name, value in selected_ids.items()
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

    async def query_database(
        self,
        database: DatabaseName,
        *,
        last_edited_after: datetime | None = None,
        start_cursor: str | None = None,
        page_size: int = 100,
    ) -> NotionPageBatch:
        """Return one bounded delta page from a configured database."""

        if database not in _DATABASES:
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
            f"/{self._collection_endpoint}/{quote(self._database_ids[database], safe='')}/query",
            json_body=body,
        )
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
            edited: Any = raw_page.get("last_edited_time")
            properties: Any = raw_page.get("properties")
            if (
                not isinstance(page_id, str)
                or not isinstance(edited, str)
                or not isinstance(properties, Mapping)
            ):
                raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page")
            try:
                edited_at = datetime.fromisoformat(edited.replace("Z", "+00:00"))
                pages.append(
                    NotionPage(
                        page_id=_validate_page_id(page_id),
                        database=database,
                        last_edited_at=edited_at,
                        url=(raw_page.get("url") if isinstance(raw_page.get("url"), str) else None),
                        properties=cast(Mapping[str, Any], properties),
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

    async def retrieve_page(self, page_id: str, *, database: DatabaseName) -> NotionPage:
        """Retrieve one page from a configured database."""

        _validate_id(page_id.replace("-", ""), "page ID")
        if database not in _DATABASES:
            raise permanent_error(ErrorCode.INPUT_INVALID, "Notion database is not configured")
        response = await self._request("GET", f"/pages/{quote(page_id, safe='')}", json_body=None)
        data = self._json_object(response, "Notion page")
        page_id_value: Any = data.get("id")
        edited: Any = data.get("last_edited_time")
        properties: Any = data.get("properties")
        if (
            not isinstance(page_id_value, str)
            or not isinstance(edited, str)
            or not isinstance(properties, Mapping)
        ):
            raise transient_error(ErrorCode.CONNECTOR_TRANSIENT, "Notion returned invalid page")
        try:
            return NotionPage(
                page_id=_validate_page_id(page_id_value),
                database=database,
                last_edited_at=datetime.fromisoformat(edited.replace("Z", "+00:00")),
                url=data.get("url") if isinstance(data.get("url"), str) else None,
                properties=cast(Mapping[str, Any], properties),
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
            block_values: list[Mapping[str, Any]] = [
                cast(Mapping[str, Any], value) for value in cast(list[Any], values)
            ]
            return NotionBlockBatch(
                page_id=page_id,
                blocks=tuple(block_values),
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

__all__ = [
    "MAX_ATTACHMENT_BYTES",
    "NOTION_API_BASE_URL",
    "NOTION_API_VERSION",
    "ConfirmedPropertyChange",
    "DatabaseName",
    "NotionAppConnector",
    "NotionAttachment",
    "NotionBlockBatch",
    "NotionConnector",
    "NotionPage",
    "NotionPageBatch",
    "NotionWriteReceipt",
]
