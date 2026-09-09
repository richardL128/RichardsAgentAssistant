"""Finance briefing domain package with lazy workflow exports."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.agents.finance.contracts import (
    BriefingPayload,
    EventCard,
    ExposureMapping,
    Holding,
    ImpactLabel,
    NormalizedEvent,
    PortfolioSnapshot,
    QuantValue,
    SourceApproval,
    SourceDocument,
    SourceEndpointFailure,
    SourceFailure,
    SourceFetchMetadata,
    SourceFetchResult,
    SourceQuery,
    ThesisJournalEntry,
    WatchlistItem,
)

if TYPE_CHECKING:
    from app.agents.finance.workflow import run_finance_briefing


def __getattr__(name: str) -> object:
    if name == "run_finance_briefing":
        from app.agents.finance.workflow import run_finance_briefing

        return run_finance_briefing
    raise AttributeError(name)


__all__ = [
    "BriefingPayload",
    "EventCard",
    "ExposureMapping",
    "Holding",
    "ImpactLabel",
    "NormalizedEvent",
    "PortfolioSnapshot",
    "QuantValue",
    "SourceApproval",
    "SourceDocument",
    "SourceEndpointFailure",
    "SourceFailure",
    "SourceFetchMetadata",
    "SourceFetchResult",
    "SourceQuery",
    "ThesisJournalEntry",
    "WatchlistItem",
    "run_finance_briefing",
]
