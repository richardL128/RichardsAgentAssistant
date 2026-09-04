"""Finance briefing domain package."""

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
    SourceFailure,
    SourceFetchResult,
    SourceQuery,
    ThesisJournalEntry,
    WatchlistItem,
)
from app.agents.finance.workflow import run_finance_briefing

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
    "SourceFailure",
    "SourceFetchResult",
    "SourceQuery",
    "ThesisJournalEntry",
    "WatchlistItem",
    "run_finance_briefing",
]
