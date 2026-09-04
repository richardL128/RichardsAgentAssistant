"""Typed orchestration for the exactly-eight approved finance source calls."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.agents.finance.contracts import (
    SourceApproval,
    SourceFailure,
    SourceFetchResult,
    SourceQuery,
    SourceStatus,
)

EXACT_SOURCE_COUNT = 8


class FinanceSourceAdapter(Protocol):
    source_id: str

    async def fetch(self, query: SourceQuery) -> SourceFetchResult: ...


class SourceGateError(RuntimeError):
    """Raised when finance source approval is insufficient for a live run."""


class SourceGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    allowlist_version: str
    statuses: Mapping[str, SourceStatus]
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class SourceWindow:
    window_start: datetime
    window_end: datetime
    tickers: tuple[str, ...]
    themes: tuple[str, ...]


def _approved_map(
    approved_sources: Sequence[SourceApproval], *, allowlist_version: str
) -> dict[str, SourceApproval]:
    source_map: dict[str, SourceApproval] = {}
    for source in approved_sources:
        if source.source_id in source_map:
            raise SourceGateError("finance source approval records must be unique")
        source_map[source.source_id] = source
    if len(source_map) != EXACT_SOURCE_COUNT:
        raise SourceGateError("finance briefing requires exactly eight approved sources")
    for source in source_map.values():
        if source.allowlist_version != allowlist_version:
            raise SourceGateError("finance source allowlist version mismatch")
        if source.gate_status is not SourceStatus.APPROVED:
            raise SourceGateError("finance source approval gate is not satisfied")
    return source_map


def check_source_gate(
    approved_sources: Sequence[SourceApproval], *, allowlist_version: str
) -> SourceGateResult:
    statuses: dict[str, SourceStatus] = {}
    try:
        source_map = _approved_map(approved_sources, allowlist_version=allowlist_version)
    except SourceGateError as exc:
        for source in approved_sources:
            status = source.gate_status
            if source.allowlist_version != allowlist_version:
                status = SourceStatus.VERSION_MISMATCH
            statuses[source.source_id] = status
        return SourceGateResult(
            enabled=False,
            allowlist_version=allowlist_version,
            statuses=statuses,
            diagnostic=str(exc),
        )
    return SourceGateResult(
        enabled=True,
        allowlist_version=allowlist_version,
        statuses=dict.fromkeys(source_map, SourceStatus.APPROVED),
    )


def build_source_queries(
    approved_sources: Sequence[SourceApproval],
    *,
    allowlist_version: str,
    window_start: datetime,
    window_end: datetime,
    tickers: Sequence[str],
    themes: Sequence[str],
) -> tuple[SourceQuery, ...]:
    source_map = _approved_map(approved_sources, allowlist_version=allowlist_version)
    return tuple(
        SourceQuery(
            source_id=source.source_id,
            source_version=source.source_version,
            window_start=window_start,
            window_end=window_end,
            tickers=tuple(tickers),
            themes=tuple(themes),
        )
        for source in sorted(source_map.values(), key=lambda item: item.source_id)
    )


async def fetch_exactly_eight_sources(
    *,
    adapters: Mapping[str, FinanceSourceAdapter],
    approved_sources: Sequence[SourceApproval],
    allowlist_version: str,
    window_start: datetime,
    window_end: datetime,
    tickers: Sequence[str],
    themes: Sequence[str],
) -> tuple[SourceFetchResult, ...]:
    """Issue one call to each approved source and never substitute a fallback."""

    queries = build_source_queries(
        approved_sources,
        allowlist_version=allowlist_version,
        window_start=window_start,
        window_end=window_end,
        tickers=tickers,
        themes=themes,
    )
    missing = [query.source_id for query in queries if query.source_id not in adapters]
    if missing:
        raise SourceGateError("finance source adapter registry is incomplete")

    async def guarded_fetch(query: SourceQuery) -> SourceFetchResult:
        adapter = adapters[query.source_id]
        try:
            result = await adapter.fetch(query)
        except Exception:
            return SourceFetchResult(
                source_id=query.source_id,
                failure=SourceFailure(
                    source_id=query.source_id,
                    error_code="connector_transient",
                    diagnostic="finance source adapter failed",
                ),
            )
        if result.source_id != query.source_id:
            return SourceFetchResult(
                source_id=query.source_id,
                failure=SourceFailure(
                    source_id=query.source_id,
                    error_code="input_invalid",
                    diagnostic="finance source adapter returned the wrong source identity",
                ),
            )
        return result

    return tuple(await asyncio.gather(*(guarded_fetch(query) for query in queries)))
