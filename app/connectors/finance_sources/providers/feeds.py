"""RSS and Atom parsing for reviewed public finance provider feeds."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.agents.finance.contracts import SourceClassification, SourceDocument, SourceQuery

from .common import (
    ProviderParseError,
    clamp_excerpt,
    clean_text,
    in_query_window,
    parse_datetime,
    stable_id,
    unique_lower,
    unique_upper,
)

ATOM_NS = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True, slots=True)
class FeedParseOptions:
    source_id: str
    source_version: str
    classification: SourceClassification
    endpoint_url: str
    endpoint_id: str
    tickers: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()
    issuer: str | None = None
    excerpt_allowed: bool = False
    excerpt_max_chars: int = 300
    require_canonical_host: str | None = None


def parse_feed_documents(
    payload: bytes | str,
    query: SourceQuery,
    retrieved_at: datetime,
    options: FeedParseOptions,
) -> tuple[SourceDocument, ...]:
    root = _parse_xml(payload)
    if root.tag == "rss" or root.tag.endswith("rss"):
        items = tuple(root.findall("./channel/item"))
        return tuple(
            document
            for item in items
            if (document := _rss_document(item, query, retrieved_at, options)) is not None
        )
    if root.tag == f"{ATOM_NS}feed" or root.tag.endswith("feed"):
        entries = tuple(root.findall(f"{ATOM_NS}entry"))
        return tuple(
            document
            for entry in entries
            if (document := _atom_document(entry, query, retrieved_at, options)) is not None
        )
    raise ProviderParseError("feed payload is neither RSS nor Atom")


def _parse_xml(payload: bytes | str) -> ET.Element:
    try:
        return ET.fromstring(payload)  # noqa: S314
    except ET.ParseError as exc:
        raise ProviderParseError("feed XML payload is malformed") from exc


def _rss_document(
    item: ET.Element,
    query: SourceQuery,
    retrieved_at: datetime,
    options: FeedParseOptions,
) -> SourceDocument | None:
    title = _child_text(item, "title") or f"{options.source_id} update"
    link = _child_text(item, "link") or options.endpoint_url
    _validate_link(link, options)
    published_at = parse_datetime(_child_text(item, "pubDate"))
    if not in_query_window(published_at, query, retrieved_at):
        return None
    guid = _child_text(item, "guid")
    description = _child_text(item, "description")
    return _document(
        external_id=guid or stable_id(options.source_id, title, link),
        title=title,
        url=link,
        published_at=published_at,
        retrieved_at=retrieved_at,
        query=query,
        options=options,
        excerpt=description,
    )


def _atom_document(
    entry: ET.Element,
    query: SourceQuery,
    retrieved_at: datetime,
    options: FeedParseOptions,
) -> SourceDocument | None:
    title = _child_text(entry, f"{ATOM_NS}title") or f"{options.source_id} update"
    link = _atom_link(entry) or options.endpoint_url
    _validate_link(link, options)
    published_at = parse_datetime(
        _child_text(entry, f"{ATOM_NS}published") or _child_text(entry, f"{ATOM_NS}updated")
    )
    if not in_query_window(published_at, query, retrieved_at):
        return None
    return _document(
        external_id=_child_text(entry, f"{ATOM_NS}id") or stable_id(options.source_id, title, link),
        title=title,
        url=link,
        published_at=published_at,
        retrieved_at=retrieved_at,
        query=query,
        options=options,
        excerpt=_child_text(entry, f"{ATOM_NS}summary"),
    )


def _document(
    *,
    external_id: str,
    title: str,
    url: str,
    published_at: datetime | None,
    retrieved_at: datetime,
    query: SourceQuery,
    options: FeedParseOptions,
    excerpt: str | None,
) -> SourceDocument:
    tickers = unique_upper((*query.tickers, *options.tickers))
    themes = unique_lower((*query.themes, *options.themes))
    try:
        return SourceDocument.model_validate(
            {
                "source_id": options.source_id,
                "source_version": options.source_version,
                "external_id": external_id[:255],
                "title": clean_text(title)[:500],
                "url": url,
                "issuer": options.issuer,
                "feed_endpoint": (
                    options.endpoint_url if options.issuer is not None else None
                ),
                "published_at": published_at,
                "retrieved_at": retrieved_at,
                "classification": options.classification,
                "license_allows_excerpt": options.excerpt_allowed,
                "excerpt": clamp_excerpt(excerpt, options.excerpt_max_chars)
                if options.excerpt_allowed
                else None,
                "tickers": tickers,
                "themes": themes,
            }
        )
    except ValidationError as exc:
        raise ProviderParseError("feed item did not satisfy finance source contract") from exc


def _child_text(element: ET.Element, path: str) -> str:
    found = element.find(path)
    return clean_text(found.text) if found is not None and found.text else ""


def _atom_link(entry: ET.Element) -> str:
    links: Iterable[ET.Element] = entry.findall(f"{ATOM_NS}link")
    for link in links:
        href = link.attrib.get("href")
        if href and link.attrib.get("rel", "alternate") == "alternate":
            return href
    return ""


def _validate_link(link: str, options: FeedParseOptions) -> None:
    if options.require_canonical_host is None:
        return
    parsed = urlsplit(link)
    if parsed.scheme != "https" or parsed.hostname != options.require_canonical_host:
        raise ProviderParseError("feed item canonical URL host is not reviewed")
