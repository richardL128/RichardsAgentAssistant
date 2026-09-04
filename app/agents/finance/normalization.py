"""Normalize, validate, and deduplicate finance source documents."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, overload

from pydantic import Field

from app.agents.finance.contracts import (
    FinanceModel,
    NormalizedEvent,
    SourceApproval,
    SourceDocument,
)

MAX_EVENT_AGE = timedelta(days=14)


class NormalizationDiagnostic(FinanceModel):
    source_id: str
    external_id: str
    reason: Literal["version_mismatch", "stale_document", "future_document"]
    diagnostic: str = Field(min_length=1, max_length=1_000)


class NormalizationResult(FinanceModel):
    events: tuple[NormalizedEvent, ...]
    diagnostics: tuple[NormalizationDiagnostic, ...] = ()


@overload
def normalize_documents(
    documents: Sequence[SourceDocument],
    *,
    approved_sources: Sequence[SourceApproval],
    now: datetime,
    include_diagnostics: Literal[False] = False,
) -> tuple[NormalizedEvent, ...]: ...


@overload
def normalize_documents(
    documents: Sequence[SourceDocument],
    *,
    approved_sources: Sequence[SourceApproval],
    now: datetime,
    include_diagnostics: Literal[True],
) -> NormalizationResult: ...


def normalize_documents(
    documents: Sequence[SourceDocument],
    *,
    approved_sources: Sequence[SourceApproval],
    now: datetime,
    include_diagnostics: bool = False,
) -> tuple[NormalizedEvent, ...] | NormalizationResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = now.astimezone(UTC)
    source_versions = {source.source_id: source.source_version for source in approved_sources}
    events: list[NormalizedEvent] = []
    diagnostics: list[NormalizationDiagnostic] = []
    for document in documents:
        approved_version = source_versions.get(document.source_id)
        if approved_version is None:
            raise ValueError("finance source document source is not approved")
        if approved_version != document.source_version:
            diagnostics.append(
                NormalizationDiagnostic(
                    source_id=document.source_id,
                    external_id=document.external_id,
                    reason="version_mismatch",
                    diagnostic="finance source document version is not approved",
                )
            )
            continue
        published_at = (document.published_at or document.retrieved_at).astimezone(UTC)
        if published_at > current:
            diagnostics.append(
                NormalizationDiagnostic(
                    source_id=document.source_id,
                    external_id=document.external_id,
                    reason="future_document",
                    diagnostic="finance source document timestamp is after the run time",
                )
            )
            continue
        if current - published_at > MAX_EVENT_AGE:
            diagnostics.append(
                NormalizationDiagnostic(
                    source_id=document.source_id,
                    external_id=document.external_id,
                    reason="stale_document",
                    diagnostic="finance source document is outside the freshness window",
                )
            )
            continue
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
    result = NormalizationResult(events=dedupe_events(events), diagnostics=tuple(diagnostics))
    return result if include_diagnostics else result.events


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
) -> tuple[dict[str, str | bool | int | None], ...]:
    """Return source metadata safe for a read-only operations console."""

    return tuple(
        {
            "source_id": source.source_id,
            "name": source.name,
            "base_url": str(source.base_url),
            "allowlist_version": source.allowlist_version,
            "source_version": source.source_version,
            "classification": source.classification.value,
            "entitlement": source.entitlement,
            "license_note": source.license_note,
            "license_allows_excerpt": source.license_allows_excerpt,
            "excerpt_max_chars": source.excerpt_max_chars,
            "excerpt_max_words": source.excerpt_max_words,
            "enabled": source.enabled,
            "approval_status": source.gate_status.value,
            "health": health.get(source.source_id),
        }
        for source in approvals
    )
