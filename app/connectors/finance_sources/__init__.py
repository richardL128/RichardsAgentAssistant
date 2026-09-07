"""Approved finance-source adapter orchestration.

This package intentionally exposes source-specific adapters only through a
fixed registry.  There is no browser/search fallback in Phase 6.
"""

from app.connectors.finance_sources.adapters import (
    EXACT_SOURCE_COUNT,
    BulkFileSourceAdapter,
    EndpointSelection,
    FinanceSourceAdapter,
    JsonHttpSourceAdapter,
    ParsedSourcePayload,
    PublicSourceAdapter,
    RegistrySourceAdapter,
    RssAtomSourceAdapter,
    SourceGateError,
    SourceGateResult,
    build_source_queries,
    check_source_gate,
    fetch_exactly_eight_sources,
)
from app.connectors.finance_sources.definitions import (
    ParserKind,
    RawSourcePayload,
    SourceDefinition,
    SourceEndpointDefinition,
    TransportKind,
)

__all__ = [
    "EXACT_SOURCE_COUNT",
    "BulkFileSourceAdapter",
    "EndpointSelection",
    "FinanceSourceAdapter",
    "JsonHttpSourceAdapter",
    "ParsedSourcePayload",
    "ParserKind",
    "PublicSourceAdapter",
    "RawSourcePayload",
    "RegistrySourceAdapter",
    "RssAtomSourceAdapter",
    "SourceDefinition",
    "SourceEndpointDefinition",
    "SourceGateError",
    "SourceGateResult",
    "TransportKind",
    "build_source_queries",
    "check_source_gate",
    "fetch_exactly_eight_sources",
]
