"""Normalize, validate, and deduplicate finance source documents."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from app.agents.finance.contracts import NormalizedEvent, SourceApproval, SourceDocument

MAX_EVENT_AGE = timedelta(days=14)


def normalize_documents(
    documents: Sequence[SourceDocument],
    *,
    approved_sources: Sequence[SourceApproval],
    now: datetime,
) -> tuple[NormalizedEvent, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    source_versions = {source.source_id: source.source_version for source in approved_sources}
    events: list[NormalizedEvent] = []
    for document in documents:
        if source_versions.get(document.source_id) != document.source_version:
            raise ValueError("finance source document version is not approved")
        published_at = document.published_at or document.retrieved_at
        if now.astimezone(UTC) - published_at > MAX_EVENT_AGE:
            raise ValueError("finance source document is outside the freshness window")
        evidence_id = _evidence_id(document)
        events.append(
            NormalizedEvent(
                event_id=_event_id(document),
                title=document.title,
                summary=document.title,
                published_at=document.published_at,
                retrieved_at=document.retrieved_at,
                source_ids=(document.source_id,),
                evidence_ids=(evidence_id,),
                tickers=document.tickers,
                themes=document.themes,
                verified_facts=(document.title,),
                numbers=document.numbers,
                permitted_excerpt=document.excerpt,
            )
        )
    return dedupe_events(events)


def dedupe_events(events: Sequence[NormalizedEvent]) -> tuple[NormalizedEvent, ...]:
    merged: dict[str, NormalizedEvent] = {}
    for event in events:
        existing = merged.get(event.event_id)
        if existing is None:
            merged[event.event_id] = event
            continue
        merged[event.event_id] = existing.model_copy(
            update={
                "source_ids": _unique((*existing.source_ids, *event.source_ids)),
                "evidence_ids": _unique((*existing.evidence_ids, *event.evidence_ids)),
                "tickers": _unique((*existing.tickers, *event.tickers)),
                "themes": _unique((*existing.themes, *event.themes)),
                "verified_facts": _unique((*existing.verified_facts, *event.verified_facts)),
                "numbers": (*existing.numbers, *event.numbers),
                "permitted_excerpt": existing.permitted_excerpt or event.permitted_excerpt,
                "retrieved_at": max(existing.retrieved_at, event.retrieved_at),
            }
        )
    return tuple(sorted(merged.values(), key=lambda item: (item.retrieved_at, item.event_id)))


def _evidence_id(document: SourceDocument) -> str:
    key = f"{document.source_id}:{document.external_id}:{document.url}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _event_id(document: SourceDocument) -> str:
    # Cross-source dedupe should collapse reported coverage of the same event.
    # Source identity is intentionally excluded.
    key = "|".join(
        (
            document.title.casefold(),
            ",".join(sorted(symbol.casefold() for symbol in document.tickers)),
            (document.published_at or document.retrieved_at).date().isoformat(),
        )
    )
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def source_metadata_for_ui(
    approvals: Sequence[SourceApproval], health: Mapping[str, str]
) -> tuple[dict[str, str | bool | None], ...]:
    """Return source metadata safe for a read-only operations console."""

    return tuple(
        {
            "source_id": source.source_id,
            "name": source.name,
            "base_url": str(source.base_url),
            "allowlist_version": source.allowlist_version,
            "source_version": source.source_version,
            "entitlement": source.entitlement,
            "license_note": source.license_note,
            "enabled": source.enabled,
            "approval_status": source.gate_status.value,
            "health": health.get(source.source_id),
        }
        for source in approvals
    )
