"""Approved finance-source adapter orchestration.

This package intentionally exposes source-specific adapters only through a
fixed registry.  There is no browser/search fallback in Phase 6.
"""

from app.connectors.finance_sources.adapters import (
    EXACT_SOURCE_COUNT,
    FinanceSourceAdapter,
    SourceGateError,
    SourceGateResult,
    build_source_queries,
    check_source_gate,
    fetch_exactly_eight_sources,
)

__all__ = [
    "EXACT_SOURCE_COUNT",
    "FinanceSourceAdapter",
    "SourceGateError",
    "SourceGateResult",
    "build_source_queries",
    "check_source_gate",
    "fetch_exactly_eight_sources",
]
