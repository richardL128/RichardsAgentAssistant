"""Official technology-security provider parsers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from app.agents.finance.contracts import SourceClassification, SourceDocument, SourceQuery

from .common import (
    JsonObject,
    ProviderParseError,
    clamp_excerpt,
    in_query_window,
    parse_datetime,
    parse_json_object,
    text,
    unique_lower,
    unique_upper,
)
from .registries import TECH_SOURCE_VERSION, TECHNOLOGY_OFFICIAL_FEEDS

CISA_KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


def parse_cisa_kev(
    payload: bytes | str | JsonObject,
    query: SourceQuery,
    retrieved_at: datetime,
) -> tuple[SourceDocument, ...]:
    root = parse_json_object(payload)
    vulnerabilities = root.get("vulnerabilities")
    if not isinstance(vulnerabilities, Sequence) or isinstance(
        vulnerabilities, str | bytes | bytearray
    ):
        raise ProviderParseError("CISA KEV payload is missing vulnerabilities")
    documents: list[SourceDocument] = []
    for item in cast(Sequence[object], vulnerabilities):
        if not isinstance(item, Mapping):
            continue
        document = _cisa_document(cast(JsonObject, item), query, retrieved_at)
        if document is not None:
            documents.append(document)
    return tuple(documents)


def _cisa_document(
    item: JsonObject,
    query: SourceQuery,
    retrieved_at: datetime,
) -> SourceDocument | None:
    cve = text(item.get("cveID"))
    if not cve:
        return None
    published_at = parse_datetime(item.get("dateAdded"))
    if not in_query_window(published_at, query, retrieved_at):
        return None
    vendor_project = text(item.get("vendorProject"))
    product = text(item.get("product"))
    title = text(item.get("vulnerabilityName")) or f"{cve} known exploited vulnerability"
    try:
        return SourceDocument.model_validate(
            {
                "source_id": TECHNOLOGY_OFFICIAL_FEEDS,
                "source_version": TECH_SOURCE_VERSION,
                "external_id": cve,
                "title": title[:500],
                "url": CISA_KEV_URL,
                "published_at": published_at,
                "retrieved_at": retrieved_at,
                "classification": SourceClassification.PRIMARY,
                "license_allows_excerpt": True,
                "excerpt": clamp_excerpt(item.get("shortDescription"), 500),
                "tickers": unique_upper(query.tickers),
                "themes": unique_lower(
                    (
                        *query.themes,
                        "technology-security",
                        vendor_project,
                        product,
                    )
                ),
            }
        )
    except ValidationError:
        return None
