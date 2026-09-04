"""Pydantic contracts for the Phase 6 finance briefing workflow."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class FinanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


_TRADE_DIRECTIVE = re.compile(
    r"\b(?:buy|sell|short|cover|trim|add|increase|decrease|reduce|"
    r"position\s*size|target\s*weight|rebalance)\b",
    re.IGNORECASE,
)


def _reject_trade_directive(value: str) -> str:
    if _TRADE_DIRECTIVE.search(value):
        raise ValueError("finance briefing text must not contain trade directives")
    return value


class SourceStatus(StrEnum):
    APPROVED = "approved"
    DISABLED = "disabled"
    MISSING_APPROVAL = "missing_approval"
    VERSION_MISMATCH = "version_mismatch"


class ImpactLabel(StrEnum):
    MONITOR = "monitor"
    REVISIT_THESIS = "revisit thesis"
    NO_ACTION = "no action"


class SourceClassification(StrEnum):
    PRIMARY = "primary"
    REPORTED = "reported"
    SECONDARY = "secondary"


class SourceApproval(FinanceModel):
    source_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=200)
    base_url: HttpUrl
    source_version: str = Field(min_length=1, max_length=128)
    allowlist_version: str = Field(min_length=1, max_length=128)
    license_note: str = Field(min_length=1, max_length=1_000)
    entitlement: str = Field(min_length=1, max_length=255)
    enabled: bool = True
    approved_at: datetime | None = None
    approval_audit_id: UUID | None = None

    @field_validator("approved_at")
    @classmethod
    def approved_at_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None

    @property
    def gate_status(self) -> SourceStatus:
        if not self.enabled:
            return SourceStatus.DISABLED
        if self.approved_at is None or self.approval_audit_id is None:
            return SourceStatus.MISSING_APPROVAL
        return SourceStatus.APPROVED


class SourceHealth(FinanceModel):
    source_id: str
    status: Literal["healthy", "attention", "failed"]
    checked_at: datetime
    source_version: str
    diagnostic: str | None = Field(default=None, max_length=1_000)

    @field_validator("checked_at")
    @classmethod
    def checked_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class SourceQuery(FinanceModel):
    source_id: str
    source_version: str
    window_start: datetime
    window_end: datetime
    tickers: tuple[str, ...] = Field(max_length=100)
    themes: tuple[str, ...] = Field(max_length=50)

    @field_validator("window_start", "window_end")
    @classmethod
    def window_aware(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def window_is_ordered(self) -> SourceQuery:
        if self.window_end <= self.window_start:
            raise ValueError("source query window must end after it starts")
        return self


class QuantValue(FinanceModel):
    label: str = Field(min_length=1, max_length=120)
    value: float
    unit: str = Field(min_length=1, max_length=40)
    as_of: date
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    formula: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def derived_values_have_formula_and_sources(self) -> QuantValue:
        if self.formula is not None and len(self.source_ids) < 1:
            raise ValueError("derived finance numbers must cite source inputs")
        return self


class SourceDocument(FinanceModel):
    source_id: str
    source_version: str
    external_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    published_at: datetime | None = None
    retrieved_at: datetime
    language: str = Field(default="en", min_length=2, max_length=16)
    classification: SourceClassification = SourceClassification.REPORTED
    license_allows_excerpt: bool = False
    excerpt: str | None = Field(default=None, max_length=500)
    tickers: tuple[str, ...] = Field(max_length=100)
    themes: tuple[str, ...] = Field(max_length=50)
    numbers: tuple[QuantValue, ...] = Field(default=(), max_length=50)

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None

    @field_validator("language")
    @classmethod
    def language_is_supported(cls, value: str) -> str:
        if value.casefold() not in {"en", "en-us", "en-ca", "en-gb"}:
            raise ValueError("finance source document language is unsupported")
        return value.casefold()

    @model_validator(mode="after")
    def excerpt_obeys_license(self) -> SourceDocument:
        if self.excerpt and not self.license_allows_excerpt:
            raise ValueError("source excerpt is not permitted by the licence record")
        return self


class SourceFailure(FinanceModel):
    source_id: str
    error_code: str = Field(min_length=1, max_length=128)
    diagnostic: str = Field(min_length=1, max_length=1_000)


class SourceFetchResult(FinanceModel):
    source_id: str
    documents: tuple[SourceDocument, ...] = Field(default=(), max_length=200)
    failure: SourceFailure | None = None


class NormalizedEvent(FinanceModel):
    event_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=1_000)
    published_at: datetime | None = None
    retrieved_at: datetime
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    tickers: tuple[str, ...] = Field(max_length=100)
    themes: tuple[str, ...] = Field(max_length=50)
    verified_facts: tuple[str, ...] = Field(min_length=1, max_length=12)
    numbers: tuple[QuantValue, ...] = Field(default=(), max_length=50)
    permitted_excerpt: str | None = Field(default=None, max_length=500)

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def event_times_aware(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None

    @field_validator("summary")
    @classmethod
    def summary_has_no_trade_directive(cls, value: str) -> str:
        return _reject_trade_directive(value)

    @field_validator("verified_facts")
    @classmethod
    def facts_have_no_trade_directive(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _reject_trade_directive(item)
        return value


class Holding(FinanceModel):
    symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    quantity: float = Field(ge=0)
    market_value: float = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3, default="USD")
    tags: tuple[str, ...] = Field(default=(), max_length=50)


class WatchlistItem(FinanceModel):
    symbol: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=200)
    thesis_id: UUID | None = None
    themes: tuple[str, ...] = Field(default=(), max_length=50)


class ETFExposure(FinanceModel):
    etf_symbol: str = Field(min_length=1, max_length=32)
    underlying_symbol: str = Field(min_length=1, max_length=32)
    weight_percent: float = Field(ge=0, le=100)
    source_id: str = Field(min_length=1, max_length=64)
    as_of: date


class PortfolioSnapshot(FinanceModel):
    holdings: tuple[Holding, ...] = ()
    watchlist: tuple[WatchlistItem, ...] = ()
    etf_exposures: tuple[ETFExposure, ...] = ()


class ExposureMapping(FinanceModel):
    event_id: str
    holding_symbols: tuple[str, ...] = ()
    watchlist_symbols: tuple[str, ...] = ()
    etf_symbols: tuple[str, ...] = ()
    thesis_ids: tuple[UUID, ...] = ()
    exposure_notes: tuple[str, ...] = Field(default=(), max_length=20)
    derived_numbers: tuple[QuantValue, ...] = Field(default=(), max_length=20)


class EventCard(FinanceModel):
    event_id: str
    title: str = Field(min_length=1, max_length=300)
    verified_facts: tuple[str, ...] = Field(min_length=1, max_length=12)
    uncertainty: str = Field(min_length=1, max_length=1_000)
    counter_case: str = Field(min_length=1, max_length=1_000)
    impact_label: ImpactLabel
    exposure: ExposureMapping
    citations: tuple[str, ...] = Field(min_length=1, max_length=20)
    numbers: tuple[QuantValue, ...] = Field(default=(), max_length=70)

    @field_validator("title", "uncertainty", "counter_case")
    @classmethod
    def text_has_no_trade_directive(cls, value: str) -> str:
        return _reject_trade_directive(value)

    @field_validator("verified_facts", "citations")
    @classmethod
    def tuple_text_has_no_trade_directive(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _reject_trade_directive(item)
        return value

    @model_validator(mode="after")
    def citations_cover_numbers(self) -> EventCard:
        cited_sources = set(self.citations)
        for number in self.numbers:
            if not set(number.source_ids).issubset(cited_sources):
                raise ValueError("every finance number must cite sources present on the card")
            if number.formula is not None and not number.source_ids:
                raise ValueError("derived finance numbers must include source inputs")
        return self


class ThesisJournalEntry(FinanceModel):
    thesis_id: UUID
    event_id: str
    impact_label: ImpactLabel
    rationale: str = Field(min_length=1, max_length=1_000)
    counter_case: str = Field(min_length=1, max_length=1_000)
    created_at: datetime

    @field_validator("rationale", "counter_case")
    @classmethod
    def journal_text_has_no_trade_directive(cls, value: str) -> str:
        return _reject_trade_directive(value)

    @field_validator("created_at")
    @classmethod
    def created_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


class BriefingPayload(FinanceModel):
    run_id: UUID
    source_allowlist_version: str
    generated_at: datetime
    status: Literal["succeeded", "attention"]
    cards: tuple[EventCard, ...] = Field(max_length=20)
    source_failures: tuple[SourceFailure, ...] = Field(default=(), max_length=8)
    tickers: tuple[str, ...] = Field(default=(), max_length=100)
    themes: tuple[str, ...] = Field(default=(), max_length=50)

    @field_validator("generated_at")
    @classmethod
    def generated_at_aware(cls, value: datetime) -> datetime:
        return _aware(value)


__all__ = [
    "BriefingPayload",
    "ETFExposure",
    "EventCard",
    "ExposureMapping",
    "FinanceModel",
    "Holding",
    "ImpactLabel",
    "NormalizedEvent",
    "PortfolioSnapshot",
    "QuantValue",
    "SourceApproval",
    "SourceClassification",
    "SourceDocument",
    "SourceFailure",
    "SourceFetchResult",
    "SourceHealth",
    "SourceQuery",
    "SourceStatus",
    "ThesisJournalEntry",
    "WatchlistItem",
]
