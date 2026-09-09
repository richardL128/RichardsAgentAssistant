"""Typed orchestration for the exactly-eight approved finance source calls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.agents.finance.contracts import (
    ETFExposure,
    SourceApproval,
    SourceDocument,
    SourceEndpointFailure,
    SourceFailure,
    SourceFetchMetadata,
    SourceFetchResult,
    SourceQuery,
    SourceStatus,
)
from app.connectors.finance_sources.cache import dedupe_new_items, high_watermark_for
from app.connectors.finance_sources.definitions import (
    RawSourcePayload,
    SourceDefinition,
    SourceEndpointDefinition,
)
from app.connectors.finance_sources.transport import (
    ConditionalHttpTransport,
    EndpointRequestAudit,
)

EXACT_SOURCE_COUNT = 8


class FinanceSourceAdapter(Protocol):
    source_id: str

    async def fetch(self, query: SourceQuery) -> SourceFetchResult: ...


@dataclass(frozen=True, slots=True)
class ParsedSourcePayload:
    documents: tuple[SourceDocument, ...] = ()
    etf_exposures: tuple[ETFExposure, ...] = ()
    stale: bool = False


@dataclass(frozen=True, slots=True)
class EndpointSelection:
    endpoints: tuple[SourceEndpointDefinition, ...]
    diagnostics: tuple[SourceEndpointFailure, ...] = ()


type PayloadParser = Callable[[RawSourcePayload, SourceQuery], ParsedSourcePayload]
type EndpointSelector = Callable[[SourceQuery, SourceDefinition], EndpointSelection]
type RequestParameters = Callable[[SourceEndpointDefinition, SourceQuery], Mapping[str, str]]
type RequestHeaders = Callable[[SourceEndpointDefinition], Mapping[str, str]]
type RequestAuditSink = Callable[[EndpointRequestAudit], None]


def _all_endpoints(_: SourceQuery, definition: SourceDefinition) -> EndpointSelection:
    return EndpointSelection(definition.endpoints)


def _no_parameters(_: SourceEndpointDefinition, __: SourceQuery) -> Mapping[str, str]:
    return {}


def _no_headers(_: SourceEndpointDefinition) -> Mapping[str, str]:
    return {}


@dataclass(slots=True)
class PublicSourceAdapter:
    """Fetch one logical source envelope from fixed, reviewed child endpoints."""

    source_id: str
    approval: SourceApproval
    definition: SourceDefinition
    transport: ConditionalHttpTransport
    parsers: Mapping[str, PayloadParser]
    selector: EndpointSelector = _all_endpoints
    request_parameters: RequestParameters = _no_parameters
    request_headers: RequestHeaders = _no_headers
    request_audit_sink: RequestAuditSink | None = None

    async def fetch(self, query: SourceQuery) -> SourceFetchResult:
        if (
            query.source_id != self.source_id
            or query.source_version != self.approval.source_version
            or query.source_version != self.definition.source_version
            or self.approval.allowlist_version != self.definition.allowlist_version
        ):
            return _public_failure(
                self.source_id,
                "input_invalid",
                "finance source query does not match its reviewed definition",
            )
        selection = self.selector(query, self.definition)
        if len(selection.endpoints) > self.definition.max_fanout:
            return _public_failure(
                self.source_id,
                "fanout_exceeded",
                "finance source registry fan-out exceeds its reviewed ceiling",
            )
        documents: list[SourceDocument] = []
        exposures: list[ETFExposure] = []
        audits: list[EndpointRequestAudit] = [
            EndpointRequestAudit(
                endpoint_id=failure.endpoint_id,
                url=str(self.approval.base_url),
                host=self.approval.base_url.host or "reviewed-registry",
                requested_at=query.window_end,
                error_code=failure.error_code,
                diagnostic=failure.diagnostic,
                request_counted=False,
            )
            for failure in selection.diagnostics
        ]
        stale = False
        for endpoint in selection.endpoints:
            outcome = await self.transport.fetch_endpoint(
                endpoint,
                headers=self.request_headers(endpoint),
                params=self.request_parameters(endpoint, query),
            )
            audit = outcome.audit
            payload = outcome.payload
            if payload is not None and not payload.not_modified:
                parser = self.parsers.get(endpoint.endpoint_id)
                if parser is None:
                    audit = replace(
                        audit,
                        error_code="parser_missing",
                        diagnostic="reviewed finance endpoint has no configured parser",
                    )
                else:
                    try:
                        parsed = parser(payload, query)
                    except (TypeError, ValueError):
                        audit = replace(
                            audit,
                            error_code="parser_failed",
                            diagnostic="finance source response could not be parsed",
                        )
                    else:
                        watermark = self.transport.state_store.get(endpoint.endpoint_id)
                        fresh_documents = dedupe_new_items(
                            parsed.documents,
                            watermark=watermark,
                            external_id=lambda document: document.external_id,
                            published_at=lambda document: document.published_at,
                            lookback_start=query.window_start,
                        )
                        documents.extend(fresh_documents)
                        exposures.extend(parsed.etf_exposures)
                        stale = stale or parsed.stale
                        self.transport.state_store.remember_watermark(
                            endpoint.endpoint_id,
                            high_watermark=high_watermark_for(
                                fresh_documents,
                                lambda document: document.published_at,
                            ),
                            seen_external_ids=(
                                document.external_id for document in fresh_documents
                            ),
                        )
            audits.append(audit)
            if self.request_audit_sink is not None and audit.request_counted:
                self.request_audit_sink(audit)
        endpoint_failures = tuple(
            failure for audit in audits if (failure := audit.failure) is not None
        )
        endpoint_count = max(1, len(audits))
        metadata = SourceFetchMetadata(
            transport=self.__class__.__name__,
            request_count=sum(audit.request_counted for audit in audits),
            endpoint_count=endpoint_count,
            not_modified_count=sum(audit.not_modified for audit in audits),
            ingestion_latency_seconds=_p95_ingestion_latency(documents),
            stale=stale,
            endpoint_failures=endpoint_failures,
        )
        failure = None
        actual_requests = tuple(audit for audit in audits if audit.request_counted)
        if (
            (
                (not selection.endpoints and endpoint_failures)
                or (actual_requests and all(audit.failure is not None for audit in actual_requests))
            )
            and not documents
            and not exposures
        ):
            failure = SourceFailure(
                source_id=self.source_id,
                error_code="source_unavailable",
                diagnostic="all selected reviewed finance endpoints failed",
            )
        return SourceFetchResult(
            source_id=self.source_id,
            documents=tuple(documents[:200]),
            etf_exposures=tuple(exposures[:5_000]),
            failure=failure,
            metadata=metadata,
        )


class JsonHttpSourceAdapter(PublicSourceAdapter):
    pass


class RssAtomSourceAdapter(PublicSourceAdapter):
    pass


class BulkFileSourceAdapter(PublicSourceAdapter):
    pass


class RegistrySourceAdapter(PublicSourceAdapter):
    pass


def _public_failure(source_id: str, error_code: str, diagnostic: str) -> SourceFetchResult:
    return SourceFetchResult(
        source_id=source_id,
        failure=SourceFailure(
            source_id=source_id,
            error_code=error_code,
            diagnostic=diagnostic,
        ),
    )


def _p95_ingestion_latency(documents: Sequence[SourceDocument]) -> float | None:
    latencies = sorted(
        max(0.0, (document.retrieved_at - document.published_at).total_seconds())
        for document in documents
        if document.published_at is not None
    )
    if not latencies:
        return None
    index = min(len(latencies) - 1, int((len(latencies) - 1) * 0.95))
    return latencies[index]


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
    issuer_tickers: Sequence[str] = (),
    etf_tickers: Sequence[str] = (),
) -> tuple[SourceQuery, ...]:
    source_map = _approved_map(approved_sources, allowlist_version=allowlist_version)
    return tuple(
        SourceQuery(
            source_id=source.source_id,
            source_version=source.source_version,
            window_start=window_start,
            window_end=window_end,
            tickers=tuple(tickers),
            issuer_tickers=tuple(issuer_tickers),
            etf_tickers=tuple(etf_tickers),
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
    issuer_tickers: Sequence[str] = (),
    etf_tickers: Sequence[str] = (),
) -> tuple[SourceFetchResult, ...]:
    """Issue one call to each approved source and never substitute a fallback."""

    queries = build_source_queries(
        approved_sources,
        allowlist_version=allowlist_version,
        window_start=window_start,
        window_end=window_end,
        tickers=tickers,
        themes=themes,
        issuer_tickers=issuer_tickers,
        etf_tickers=etf_tickers,
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
