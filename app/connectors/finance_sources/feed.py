"""RSS/Atom parsing boundary for public finance source feeds."""

from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

from app.connectors.finance_sources.definitions import (
    ParserKind,
    RawSourcePayload,
    SourceEndpointDefinition,
)

_XML_NAMESPACES: Final[dict[str, str]] = {"atom": "http://www.w3.org/2005/Atom"}
_HTML_TAG = re.compile(r"<[^>]+>")


class FeedParseError(ValueError):
    """Raised when a reviewed feed cannot be parsed safely."""

    def __init__(self, error_code: str, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.error_code = error_code
        self.diagnostic = diagnostic


@dataclass(frozen=True, slots=True)
class FeedEntry:
    endpoint_id: str
    external_id: str
    title: str
    url: str
    published_at: datetime | None
    updated_at: datetime | None
    retrieved_at: datetime
    excerpt: str | None = None
    categories: tuple[str, ...] = ()


def parse_rss_atom_payload(
    payload: RawSourcePayload,
    endpoint: SourceEndpointDefinition,
    *,
    max_entries: int = 200,
) -> tuple[FeedEntry, ...]:
    """Parse a fetched RSS/Atom payload without exposing raw feed content."""

    if payload.not_modified:
        return ()
    if endpoint.parser_kind not in {ParserKind.RSS, ParserKind.ATOM}:
        raise FeedParseError("parser_invalid", "finance feed endpoint parser kind is invalid")
    if b"<!DOCTYPE" in payload.body[:1_000].upper():
        raise FeedParseError("malformed_xml", "finance feed returned unsafe XML")
    try:
        root = ET.fromstring(payload.body)  # noqa: S314 - DTDs are rejected above.
    except ET.ParseError as exc:
        raise FeedParseError("malformed_xml", "finance feed returned malformed XML") from exc

    root_name = _local_name(root.tag)
    if root_name == "rss":
        return _parse_rss(root, payload, endpoint, max_entries=max_entries)
    if root_name == "feed":
        return _parse_atom(root, payload, endpoint, max_entries=max_entries)
    raise FeedParseError("unsupported_feed", "finance feed XML format is unsupported")


def _parse_rss(
    root: ET.Element,
    payload: RawSourcePayload,
    endpoint: SourceEndpointDefinition,
    *,
    max_entries: int,
) -> tuple[FeedEntry, ...]:
    channel = root.find("channel")
    if channel is None:
        raise FeedParseError("malformed_xml", "finance RSS feed is missing a channel")
    entries: list[FeedEntry] = []
    for item in channel.findall("item")[:max_entries]:
        title = _text(item.find("title"))
        url = _text(item.find("link")) or endpoint.url
        published = _parse_datetime(_text(item.find("pubDate")))
        guid = _text(item.find("guid"))
        description = _text(item.find("description"))
        categories = tuple(
            category for category in (_text(node) for node in item.findall("category")) if category
        )
        entries.append(
            FeedEntry(
                endpoint_id=payload.endpoint_id,
                external_id=_external_id(guid, url, title, published),
                title=title or "Finance source feed item",
                url=url,
                published_at=published,
                updated_at=None,
                retrieved_at=payload.retrieved_at,
                excerpt=_permitted_excerpt(description, endpoint),
                categories=categories,
            )
        )
    return tuple(entries)


def _parse_atom(
    root: ET.Element,
    payload: RawSourcePayload,
    endpoint: SourceEndpointDefinition,
    *,
    max_entries: int,
) -> tuple[FeedEntry, ...]:
    entries: list[FeedEntry] = []
    for entry in root.findall("atom:entry", _XML_NAMESPACES)[:max_entries]:
        title = _text(entry.find("atom:title", _XML_NAMESPACES))
        url = _atom_link(entry) or endpoint.url
        published = _parse_datetime(_text(entry.find("atom:published", _XML_NAMESPACES)))
        updated = _parse_datetime(_text(entry.find("atom:updated", _XML_NAMESPACES)))
        summary = _text(entry.find("atom:summary", _XML_NAMESPACES)) or _text(
            entry.find("atom:content", _XML_NAMESPACES)
        )
        categories = tuple(
            term
            for category in entry.findall("atom:category", _XML_NAMESPACES)
            if (term := category.get("term"))
        )
        entries.append(
            FeedEntry(
                endpoint_id=payload.endpoint_id,
                external_id=_external_id(
                    _text(entry.find("atom:id", _XML_NAMESPACES)),
                    url,
                    title,
                    published or updated,
                ),
                title=title or "Finance source feed item",
                url=url,
                published_at=published,
                updated_at=updated,
                retrieved_at=payload.retrieved_at,
                excerpt=_permitted_excerpt(summary, endpoint),
                categories=categories,
            )
        )
    return tuple(entries)


def _atom_link(entry: ET.Element) -> str:
    alternate: str | None = None
    first: str | None = None
    for link in entry.findall("atom:link", _XML_NAMESPACES):
        href = link.get("href")
        if not href:
            continue
        first = first or href
        if link.get("rel") in {None, "", "alternate"}:
            alternate = href
            break
    return alternate or first or ""


def _external_id(
    stable_id: str,
    url: str,
    title: str,
    published_at: datetime | None,
) -> str:
    if stable_id:
        return stable_id[:255]
    key = f"{url}:{title}:{published_at.isoformat() if published_at else ''}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _permitted_excerpt(value: str, endpoint: SourceEndpointDefinition) -> str | None:
    if not endpoint.excerpt_allowed or not value:
        return None
    max_chars = min(endpoint.excerpt_max_chars or 500, 500)
    return value[:max_chars].rstrip()


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _text(element: ET.Element | None) -> str:
    if element is None or element.text is None:
        return ""
    text = html.unescape(element.text)
    text = _HTML_TAG.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", maxsplit=1)[-1]
    return tag


__all__ = ["FeedEntry", "FeedParseError", "parse_rss_atom_payload"]
