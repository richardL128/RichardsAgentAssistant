"""Durable persistence for Phase 6 finance briefing records.

Finance ORM mappings live here instead of the shared model module so this phase
can be integrated without competing edits to shared files.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.agents.finance.contracts import (
    BriefingPayload,
    ETFExposure,
    Holding,
    PortfolioSnapshot,
    SourceApproval,
    ThesisJournalEntry,
    WatchlistItem,
)
from app.db.models import Base


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _db_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _upsert(
    session: Session,
    model: type[Any],
    filters: Sequence[Any],
    values: Mapping[str, Any],
) -> Any:
    existing = session.scalar(select(model).where(*filters))
    if existing is not None:
        for key, value in values.items():
            setattr(existing, key, value)
        session.flush()
        return existing
    instance = model(**values)
    try:
        with session.begin_nested():
            session.add(instance)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(model).where(*filters))
        if existing is None:
            raise
        return existing
    return instance


class FinanceApprovedSource(Base):
    __tablename__ = "finance_approved_sources"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "allowlist_version",
            name="uq_finance_sources_source_allowlist",
        ),
        CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        CheckConstraint("length(name) > 0", name="name_nonempty"),
        CheckConstraint("length(base_url) > 0", name="base_url_nonempty"),
        CheckConstraint("length(license_note) > 0", name="license_note_nonempty"),
        CheckConstraint("length(entitlement) > 0", name="entitlement_nonempty"),
        CheckConstraint(
            "classification IN ('primary','reported','secondary')",
            name="classification_valid",
        ),
        CheckConstraint(
            "license_allows_excerpt OR (excerpt_max_chars IS NULL AND excerpt_max_words IS NULL)",
            name="excerpt_limits_require_permission",
        ),
        CheckConstraint(
            "excerpt_max_chars IS NULL OR (excerpt_max_chars >= 1 AND excerpt_max_chars <= 500)",
            name="excerpt_max_chars_valid",
        ),
        CheckConstraint(
            "excerpt_max_words IS NULL OR (excerpt_max_words >= 1 AND excerpt_max_words <= 500)",
            name="excerpt_max_words_valid",
        ),
        Index("ix_finance_sources_allowlist_enabled", "allowlist_version", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    allowlist_version: Mapped[str] = mapped_column(String(128), nullable=False)
    license_note: Mapped[str] = mapped_column(String(1000), nullable=False)
    entitlement: Mapped[str] = mapped_column(String(255), nullable=False)
    classification: Mapped[str] = mapped_column(String(64), nullable=False, default="reported")
    license_allows_excerpt: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    excerpt_max_chars: Mapped[int | None] = mapped_column(Integer)
    excerpt_max_words: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_audit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("audit_events.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class FinanceSourceHealth(Base):
    __tablename__ = "finance_source_health"
    __table_args__ = (
        UniqueConstraint("source_id", "source_version", name="uq_finance_source_health_version"),
        CheckConstraint("status IN ('healthy','attention','failed')", name="status_valid"),
        Index("ix_finance_source_health_status", "status", "checked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    diagnostic: Mapped[str | None] = mapped_column(String(1000))


class FinanceHolding(Base):
    __tablename__ = "finance_holdings"
    __table_args__ = (
        UniqueConstraint("symbol", name="uq_finance_holdings_symbol"),
        CheckConstraint("quantity >= 0", name="quantity_nonnegative"),
        CheckConstraint("market_value >= 0", name="market_value_nonnegative"),
        Index("ix_finance_holdings_enabled_symbol", "enabled", "symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    market_value: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class FinanceWatchlistEntry(Base):
    __tablename__ = "finance_watchlist"
    __table_args__ = (
        UniqueConstraint("symbol", name="uq_finance_watchlist_symbol"),
        Index("ix_finance_watchlist_enabled_symbol", "enabled", "symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    thesis_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("finance_investment_theses.id", ondelete="SET NULL")
    )
    themes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class FinanceETFExposure(Base):
    __tablename__ = "finance_etf_exposures"
    __table_args__ = (
        UniqueConstraint(
            "etf_symbol",
            "underlying_symbol",
            "as_of",
            name="uq_finance_etf_exposure_as_of",
        ),
        CheckConstraint("weight_percent >= 0 AND weight_percent <= 100", name="weight_valid"),
        Index("ix_finance_etf_exposure_underlying", "underlying_symbol", "as_of"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    etf_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    underlying_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    weight_percent: Mapped[float] = mapped_column(Float, nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FinanceSourceEndpoint(Base):
    __tablename__ = "finance_source_endpoints"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "allowlist_version"],
            [
                "finance_approved_sources.source_id",
                "finance_approved_sources.allowlist_version",
            ],
            ondelete="CASCADE",
            name="fk_finance_source_endpoints_source_allowlist",
        ),
        UniqueConstraint(
            "allowlist_version",
            "source_id",
            "endpoint_id",
            name="uq_finance_source_endpoints_allowlist_source_endpoint",
        ),
        CheckConstraint("length(allowlist_version) > 0", name="allowlist_version_nonempty"),
        CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        CheckConstraint("length(endpoint_id) > 0", name="endpoint_id_nonempty"),
        CheckConstraint("length(base_url) > 0", name="base_url_nonempty"),
        CheckConstraint("length(host) > 0", name="host_nonempty"),
        CheckConstraint(
            "transport_kind IN ('json_http','rss_atom','bulk_file')",
            name="transport_kind_valid",
        ),
        CheckConstraint(
            "parser_kind IN ('json','rss','atom','csv','xls','xlsx')",
            name="parser_kind_valid",
        ),
        CheckConstraint("length(registry_version) > 0", name="registry_version_nonempty"),
        CheckConstraint("expected_freshness_seconds > 0", name="expected_freshness_positive"),
        CheckConstraint("request_ceiling > 0", name="request_ceiling_positive"),
        CheckConstraint("length(license_note) > 0", name="license_note_nonempty"),
        CheckConstraint("length(retention_note) > 0", name="retention_note_nonempty"),
        CheckConstraint(
            "excerpt_allowed OR excerpt_max_chars IS NULL",
            name="endpoint_excerpt_limit_requires_permission",
        ),
        CheckConstraint(
            "excerpt_max_chars IS NULL OR (excerpt_max_chars >= 1 AND excerpt_max_chars <= 500)",
            name="endpoint_excerpt_max_chars_valid",
        ),
        Index(
            "ix_finance_source_endpoints_allowlist_source",
            "allowlist_version",
            "source_id",
            "enabled",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    allowlist_version: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    transport_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    registry_version: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expected_freshness_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    request_ceiling: Mapped[int] = mapped_column(Integer, nullable=False)
    license_note: Mapped[str] = mapped_column(String(1000), nullable=False)
    retention_note: Mapped[str] = mapped_column(String(1000), nullable=False)
    excerpt_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    excerpt_max_chars: Mapped[int | None] = mapped_column(Integer)
    issuer_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    cik_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    ticker_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class FinanceSourceCacheState(Base):
    __tablename__ = "finance_source_cache_state"
    __table_args__ = (
        ForeignKeyConstraint(
            ["allowlist_version", "source_id", "endpoint_id"],
            [
                "finance_source_endpoints.allowlist_version",
                "finance_source_endpoints.source_id",
                "finance_source_endpoints.endpoint_id",
            ],
            ondelete="CASCADE",
            name="fk_finance_source_cache_state_endpoint",
        ),
        UniqueConstraint(
            "allowlist_version",
            "source_id",
            "endpoint_id",
            name="uq_finance_source_cache_state_endpoint",
        ),
        CheckConstraint("length(allowlist_version) > 0", name="allowlist_version_nonempty"),
        CheckConstraint("length(source_id) > 0", name="source_id_nonempty"),
        CheckConstraint("length(endpoint_id) > 0", name="endpoint_id_nonempty"),
        Index(
            "ix_finance_source_cache_state_watermark",
            "allowlist_version",
            "source_id",
            "watermark_published_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    allowlist_version: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    etag: Mapped[str | None] = mapped_column(String(512))
    last_modified: Mapped[str | None] = mapped_column(String(512))
    watermark_external_id: Mapped[str | None] = mapped_column(String(512))
    watermark_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cached_artifact_key: Mapped[str | None] = mapped_column(String(512))
    payload_sha256: Mapped[str | None] = mapped_column(String(64))
    last_retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_not_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class FinanceSourceRequestAudit(Base):
    __tablename__ = "finance_source_request_audits"
    __table_args__ = (
        ForeignKeyConstraint(
            ["allowlist_version", "source_id", "endpoint_id"],
            [
                "finance_source_endpoints.allowlist_version",
                "finance_source_endpoints.source_id",
                "finance_source_endpoints.endpoint_id",
            ],
            ondelete="RESTRICT",
            name="fk_finance_source_request_audits_endpoint",
        ),
        CheckConstraint(
            "outcome IN ('succeeded','not_modified','failed')",
            name="outcome_valid",
        ),
        Index(
            "ix_finance_source_request_audits_endpoint_time",
            "allowlist_version",
            "source_id",
            "endpoint_id",
            "requested_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    allowlist_version: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_id: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128))
    not_modified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class FinanceInvestmentThesis(Base):
    __tablename__ = "finance_investment_theses"
    __table_args__ = (
        UniqueConstraint("symbol", "version", name="uq_finance_theses_symbol_version"),
        CheckConstraint("status IN ('active','paused','closed')", name="status_valid"),
        Index("ix_finance_theses_status_symbol", "status", "symbol"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    thesis_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class FinanceThesisEvent(Base):
    __tablename__ = "finance_thesis_events"
    __table_args__ = (
        UniqueConstraint("thesis_id", "event_id", name="uq_finance_thesis_events_once"),
        CheckConstraint(
            "impact_label IN ('monitor','revisit thesis','no action')",
            name="impact_label_valid",
        ),
        Index("ix_finance_thesis_events_thesis_created", "thesis_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    thesis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("finance_investment_theses.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    impact_label: Mapped[str] = mapped_column(String(32), nullable=False)
    rationale: Mapped[str] = mapped_column(String(1000), nullable=False)
    counter_case: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FinanceBriefing(Base):
    __tablename__ = "finance_briefings"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_finance_briefings_run"),
        CheckConstraint("status IN ('succeeded','attention')", name="status_valid"),
        CheckConstraint("card_count >= 0", name="card_count_nonnegative"),
        Index("ix_finance_briefings_generated", "generated_at", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    source_allowlist_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tickers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    themes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    card_count: Mapped[int] = mapped_column(nullable=False)
    source_failure_count: Mapped[int] = mapped_column(nullable=False)
    redacted_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


@dataclass(frozen=True, slots=True)
class FinanceSourceRecord:
    source_id: str
    name: str
    base_url: str
    classification: str
    entitlement: str
    license_note: str
    license_allows_excerpt: bool
    excerpt_max_chars: int | None
    excerpt_max_words: int | None
    source_version: str
    allowlist_version: str
    enabled: bool
    approved_at: datetime | None
    health: str | None
    health_checked_at: datetime | None


@dataclass(frozen=True, slots=True)
class FinanceSourceEndpointRecord:
    allowlist_version: str
    source_id: str
    endpoint_id: str
    base_url: str
    host: str
    transport_kind: str
    parser_kind: str
    registry_version: str
    enabled: bool
    expected_freshness_seconds: int
    request_ceiling: int
    license_note: str
    retention_note: str
    excerpt_allowed: bool
    excerpt_max_chars: int | None
    issuer_scope: tuple[str, ...]
    cik_scope: tuple[str, ...]
    ticker_scope: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinanceSourceCacheStateRecord:
    allowlist_version: str
    source_id: str
    endpoint_id: str
    etag: str | None
    last_modified: str | None
    watermark_external_id: str | None
    watermark_published_at: datetime | None
    cached_artifact_key: str | None
    payload_sha256: str | None
    last_retrieved_at: datetime | None
    last_not_modified_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FinanceRunFilterMetadata:
    run_id: uuid.UUID
    generated_at: datetime
    status: str
    source_allowlist_version: str
    tickers: tuple[str, ...]
    themes: tuple[str, ...]


class FinanceRepository:
    @staticmethod
    def upsert_approved_source(
        session: Session,
        *,
        source_id: str,
        name: str,
        base_url: str,
        source_version: str,
        allowlist_version: str,
        license_note: str,
        entitlement: str,
        classification: str = "reported",
        license_allows_excerpt: bool = False,
        excerpt_max_chars: int | None = None,
        excerpt_max_words: int | None = None,
        enabled: bool = False,
        approved_at: datetime | None = None,
        approval_audit_id: uuid.UUID | None = None,
    ) -> FinanceApprovedSource:
        if approved_at is not None:
            approved_at = _utc(approved_at, "approved_at")
        return _upsert(
            session,
            FinanceApprovedSource,
            [
                FinanceApprovedSource.source_id == source_id,
                FinanceApprovedSource.allowlist_version == allowlist_version,
            ],
            {
                "source_id": source_id,
                "name": name,
                "base_url": base_url,
                "source_version": source_version,
                "allowlist_version": allowlist_version,
                "license_note": license_note,
                "entitlement": entitlement,
                "classification": classification,
                "license_allows_excerpt": license_allows_excerpt,
                "excerpt_max_chars": excerpt_max_chars,
                "excerpt_max_words": excerpt_max_words,
                "enabled": enabled,
                "approved_at": approved_at,
                "approval_audit_id": approval_audit_id,
                "updated_at": datetime.now(UTC),
            },
        )

    @staticmethod
    def record_source_health(
        session: Session,
        *,
        source_id: str,
        source_version: str,
        status: str,
        checked_at: datetime,
        diagnostic: str | None = None,
    ) -> FinanceSourceHealth:
        return _upsert(
            session,
            FinanceSourceHealth,
            [
                FinanceSourceHealth.source_id == source_id,
                FinanceSourceHealth.source_version == source_version,
            ],
            {
                "source_id": source_id,
                "source_version": source_version,
                "status": status,
                "checked_at": _utc(checked_at, "checked_at"),
                "diagnostic": diagnostic,
            },
        )

    @staticmethod
    def load_approved_sources(
        session: Session, *, allowlist_version: str
    ) -> tuple[SourceApproval, ...]:
        rows = session.scalars(
            select(FinanceApprovedSource)
            .where(FinanceApprovedSource.allowlist_version == allowlist_version)
            .order_by(FinanceApprovedSource.source_id)
        )
        return tuple(
            SourceApproval.model_validate(
                {
                    "source_id": row.source_id,
                    "name": row.name,
                    "base_url": row.base_url,
                    "source_version": row.source_version,
                    "allowlist_version": row.allowlist_version,
                    "license_note": row.license_note,
                    "entitlement": row.entitlement,
                    "classification": row.classification,
                    "license_allows_excerpt": row.license_allows_excerpt,
                    "excerpt_max_chars": row.excerpt_max_chars,
                    "excerpt_max_words": row.excerpt_max_words,
                    "enabled": row.enabled,
                    "approved_at": _db_utc(row.approved_at),
                    "approval_audit_id": row.approval_audit_id,
                }
            )
            for row in rows
        )

    @staticmethod
    def source_approval_gate(session: Session, *, allowlist_version: str) -> bool:
        sources = FinanceRepository.load_approved_sources(
            session, allowlist_version=allowlist_version
        )
        return (
            len(sources) == 8
            and all(source.enabled for source in sources)
            and all(source.approved_at is not None for source in sources)
            and all(source.approval_audit_id is not None for source in sources)
            and all(source.license_note for source in sources)
            and all(source.entitlement for source in sources)
        )

    @staticmethod
    def upsert_holding(
        session: Session,
        *,
        symbol: str,
        name: str,
        quantity: float,
        market_value: float,
        currency: str = "USD",
        tags: Sequence[str] = (),
        enabled: bool = True,
    ) -> FinanceHolding:
        return _upsert(
            session,
            FinanceHolding,
            [FinanceHolding.symbol == symbol.upper()],
            {
                "symbol": symbol.upper(),
                "name": name,
                "quantity": quantity,
                "market_value": market_value,
                "currency": currency.upper(),
                "tags": list(tags),
                "enabled": enabled,
            },
        )

    @staticmethod
    def upsert_thesis(
        session: Session,
        *,
        symbol: str,
        version: str,
        title: str,
        thesis_summary: str,
        status: str = "active",
    ) -> FinanceInvestmentThesis:
        return _upsert(
            session,
            FinanceInvestmentThesis,
            [
                FinanceInvestmentThesis.symbol == symbol.upper(),
                FinanceInvestmentThesis.version == version,
            ],
            {
                "symbol": symbol.upper(),
                "version": version,
                "title": title,
                "thesis_summary": thesis_summary,
                "status": status,
                "updated_at": datetime.now(UTC),
            },
        )

    @staticmethod
    def upsert_watchlist_entry(
        session: Session,
        *,
        symbol: str,
        name: str,
        thesis_id: uuid.UUID | None = None,
        themes: Sequence[str] = (),
        enabled: bool = True,
    ) -> FinanceWatchlistEntry:
        return _upsert(
            session,
            FinanceWatchlistEntry,
            [FinanceWatchlistEntry.symbol == symbol.upper()],
            {
                "symbol": symbol.upper(),
                "name": name,
                "thesis_id": thesis_id,
                "themes": list(themes),
                "enabled": enabled,
            },
        )

    @staticmethod
    def upsert_etf_exposure(
        session: Session,
        *,
        etf_symbol: str,
        underlying_symbol: str,
        weight_percent: float,
        source_id: str,
        as_of: date,
        source_url: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> FinanceETFExposure:
        if retrieved_at is not None:
            retrieved_at = _utc(retrieved_at, "retrieved_at")
        return _upsert(
            session,
            FinanceETFExposure,
            [
                FinanceETFExposure.etf_symbol == etf_symbol.upper(),
                FinanceETFExposure.underlying_symbol == underlying_symbol.upper(),
                FinanceETFExposure.as_of == as_of,
            ],
            {
                "etf_symbol": etf_symbol.upper(),
                "underlying_symbol": underlying_symbol.upper(),
                "weight_percent": weight_percent,
                "source_id": source_id,
                "as_of": as_of,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
            },
        )

    @staticmethod
    def upsert_etf_exposures(
        session: Session, exposures: Sequence[ETFExposure]
    ) -> tuple[FinanceETFExposure, ...]:
        return tuple(
            FinanceRepository.upsert_etf_exposure(
                session,
                etf_symbol=exposure.etf_symbol,
                underlying_symbol=exposure.underlying_symbol,
                weight_percent=exposure.weight_percent,
                source_id=exposure.source_id,
                as_of=exposure.as_of,
                source_url=str(exposure.source_url) if exposure.source_url is not None else None,
                retrieved_at=exposure.retrieved_at,
            )
            for exposure in exposures
        )

    @staticmethod
    def upsert_source_endpoint(
        session: Session,
        *,
        allowlist_version: str,
        source_id: str,
        endpoint_id: str,
        base_url: str,
        host: str,
        transport_kind: str,
        parser_kind: str,
        registry_version: str,
        expected_freshness_seconds: int,
        request_ceiling: int,
        license_note: str,
        retention_note: str,
        enabled: bool = True,
        excerpt_allowed: bool = False,
        excerpt_max_chars: int | None = None,
        issuer_scope: Sequence[str] = (),
        cik_scope: Sequence[str] = (),
        ticker_scope: Sequence[str] = (),
    ) -> FinanceSourceEndpoint:
        return _upsert(
            session,
            FinanceSourceEndpoint,
            [
                FinanceSourceEndpoint.allowlist_version == allowlist_version,
                FinanceSourceEndpoint.source_id == source_id,
                FinanceSourceEndpoint.endpoint_id == endpoint_id,
            ],
            {
                "allowlist_version": allowlist_version,
                "source_id": source_id,
                "endpoint_id": endpoint_id,
                "base_url": base_url,
                "host": host,
                "transport_kind": transport_kind,
                "parser_kind": parser_kind,
                "registry_version": registry_version,
                "enabled": enabled,
                "expected_freshness_seconds": expected_freshness_seconds,
                "request_ceiling": request_ceiling,
                "license_note": license_note,
                "retention_note": retention_note,
                "excerpt_allowed": excerpt_allowed,
                "excerpt_max_chars": excerpt_max_chars,
                "issuer_scope": list(issuer_scope),
                "cik_scope": list(cik_scope),
                "ticker_scope": list(ticker_scope),
                "updated_at": datetime.now(UTC),
            },
        )

    @staticmethod
    def list_source_endpoints(
        session: Session,
        *,
        allowlist_version: str,
        source_id: str | None = None,
        enabled_only: bool = False,
    ) -> tuple[FinanceSourceEndpointRecord, ...]:
        statement = select(FinanceSourceEndpoint).where(
            FinanceSourceEndpoint.allowlist_version == allowlist_version
        )
        if source_id is not None:
            statement = statement.where(FinanceSourceEndpoint.source_id == source_id)
        if enabled_only:
            statement = statement.where(FinanceSourceEndpoint.enabled.is_(True))
        statement = statement.order_by(
            FinanceSourceEndpoint.source_id,
            FinanceSourceEndpoint.endpoint_id,
        )
        return tuple(
            FinanceSourceEndpointRecord(
                allowlist_version=row.allowlist_version,
                source_id=row.source_id,
                endpoint_id=row.endpoint_id,
                base_url=row.base_url,
                host=row.host,
                transport_kind=row.transport_kind,
                parser_kind=row.parser_kind,
                registry_version=row.registry_version,
                enabled=row.enabled,
                expected_freshness_seconds=row.expected_freshness_seconds,
                request_ceiling=row.request_ceiling,
                license_note=row.license_note,
                retention_note=row.retention_note,
                excerpt_allowed=row.excerpt_allowed,
                excerpt_max_chars=row.excerpt_max_chars,
                issuer_scope=tuple(row.issuer_scope),
                cik_scope=tuple(row.cik_scope),
                ticker_scope=tuple(row.ticker_scope),
            )
            for row in session.scalars(statement)
        )

    @staticmethod
    def load_source_cache_state(
        session: Session,
        *,
        allowlist_version: str,
        source_id: str,
        endpoint_id: str,
    ) -> FinanceSourceCacheStateRecord | None:
        row = session.scalar(
            select(FinanceSourceCacheState).where(
                FinanceSourceCacheState.allowlist_version == allowlist_version,
                FinanceSourceCacheState.source_id == source_id,
                FinanceSourceCacheState.endpoint_id == endpoint_id,
            )
        )
        if row is None:
            return None
        return FinanceSourceCacheStateRecord(
            allowlist_version=row.allowlist_version,
            source_id=row.source_id,
            endpoint_id=row.endpoint_id,
            etag=row.etag,
            last_modified=row.last_modified,
            watermark_external_id=row.watermark_external_id,
            watermark_published_at=_db_utc(row.watermark_published_at),
            cached_artifact_key=row.cached_artifact_key,
            payload_sha256=row.payload_sha256,
            last_retrieved_at=_db_utc(row.last_retrieved_at),
            last_not_modified_at=_db_utc(row.last_not_modified_at),
            updated_at=_db_utc(row.updated_at) or row.updated_at,
        )

    @staticmethod
    def save_source_cache_state(
        session: Session,
        *,
        allowlist_version: str,
        source_id: str,
        endpoint_id: str,
        etag: str | None = None,
        last_modified: str | None = None,
        watermark_external_id: str | None = None,
        watermark_published_at: datetime | None = None,
        cached_artifact_key: str | None = None,
        payload_sha256: str | None = None,
        last_retrieved_at: datetime | None = None,
        last_not_modified_at: datetime | None = None,
    ) -> FinanceSourceCacheState:
        if watermark_published_at is not None:
            watermark_published_at = _utc(watermark_published_at, "watermark_published_at")
        if last_retrieved_at is not None:
            last_retrieved_at = _utc(last_retrieved_at, "last_retrieved_at")
        if last_not_modified_at is not None:
            last_not_modified_at = _utc(last_not_modified_at, "last_not_modified_at")
        return _upsert(
            session,
            FinanceSourceCacheState,
            [
                FinanceSourceCacheState.allowlist_version == allowlist_version,
                FinanceSourceCacheState.source_id == source_id,
                FinanceSourceCacheState.endpoint_id == endpoint_id,
            ],
            {
                "allowlist_version": allowlist_version,
                "source_id": source_id,
                "endpoint_id": endpoint_id,
                "etag": etag,
                "last_modified": last_modified,
                "watermark_external_id": watermark_external_id,
                "watermark_published_at": watermark_published_at,
                "cached_artifact_key": cached_artifact_key,
                "payload_sha256": payload_sha256,
                "last_retrieved_at": last_retrieved_at,
                "last_not_modified_at": last_not_modified_at,
                "updated_at": datetime.now(UTC),
            },
        )

    @staticmethod
    def record_source_request_audit(
        session: Session,
        *,
        allowlist_version: str,
        source_id: str,
        endpoint_id: str,
        requested_at: datetime,
        status_code: int | None,
        error_code: str | None,
        not_modified: bool,
    ) -> FinanceSourceRequestAudit:
        outcome = (
            "failed"
            if error_code is not None
            else ("not_modified" if not_modified else "succeeded")
        )
        record = FinanceSourceRequestAudit(
            allowlist_version=allowlist_version,
            source_id=source_id,
            endpoint_id=endpoint_id,
            requested_at=_utc(requested_at, "requested_at"),
            status_code=status_code,
            outcome=outcome,
            error_code=error_code,
            not_modified=not_modified,
        )
        session.add(record)
        session.flush()
        return record

    @staticmethod
    def load_portfolio_snapshot(session: Session) -> PortfolioSnapshot:
        holdings = tuple(
            Holding(
                symbol=row.symbol,
                name=row.name,
                quantity=row.quantity,
                market_value=row.market_value,
                currency=row.currency,
                tags=tuple(row.tags),
            )
            for row in session.scalars(
                select(FinanceHolding)
                .where(FinanceHolding.enabled.is_(True))
                .order_by(FinanceHolding.symbol)
            )
        )
        watchlist = tuple(
            WatchlistItem(
                symbol=row.symbol,
                name=row.name,
                thesis_id=row.thesis_id,
                themes=tuple(row.themes),
            )
            for row in session.scalars(
                select(FinanceWatchlistEntry)
                .where(FinanceWatchlistEntry.enabled.is_(True))
                .order_by(FinanceWatchlistEntry.symbol)
            )
        )
        etf_exposures = tuple(
            ETFExposure.model_validate(
                {
                    "etf_symbol": row.etf_symbol,
                    "underlying_symbol": row.underlying_symbol,
                    "weight_percent": row.weight_percent,
                    "source_id": row.source_id,
                    "as_of": row.as_of,
                    "source_url": row.source_url,
                    "retrieved_at": _db_utc(row.retrieved_at),
                }
            )
            for row in session.scalars(
                select(FinanceETFExposure).order_by(
                    FinanceETFExposure.etf_symbol,
                    FinanceETFExposure.underlying_symbol,
                )
            )
        )
        return PortfolioSnapshot(
            holdings=holdings,
            watchlist=watchlist,
            etf_exposures=etf_exposures,
        )

    @staticmethod
    def save_briefing_payload(session: Session, payload: BriefingPayload) -> FinanceBriefing:
        return _upsert(
            session,
            FinanceBriefing,
            [FinanceBriefing.run_id == payload.run_id],
            {
                "run_id": payload.run_id,
                "source_allowlist_version": payload.source_allowlist_version,
                "status": payload.status,
                "generated_at": payload.generated_at,
                "tickers": list(payload.tickers),
                "themes": list(payload.themes),
                "card_count": len(payload.cards),
                "source_failure_count": len(payload.source_failures),
                "redacted_payload": payload.model_dump(mode="json"),
            },
        )

    @staticmethod
    def append_thesis_events(
        session: Session, entries: Sequence[ThesisJournalEntry]
    ) -> tuple[FinanceThesisEvent, ...]:
        return tuple(
            _upsert(
                session,
                FinanceThesisEvent,
                [
                    FinanceThesisEvent.thesis_id == entry.thesis_id,
                    FinanceThesisEvent.event_id == entry.event_id,
                ],
                {
                    "thesis_id": entry.thesis_id,
                    "event_id": entry.event_id,
                    "impact_label": entry.impact_label.value,
                    "rationale": entry.rationale,
                    "counter_case": entry.counter_case,
                    "created_at": entry.created_at,
                },
            )
            for entry in entries
        )

    @staticmethod
    def list_source_records(
        session: Session, *, allowlist_version: str | None = None
    ) -> tuple[FinanceSourceRecord, ...]:
        statement = select(FinanceApprovedSource).order_by(
            FinanceApprovedSource.allowlist_version,
            FinanceApprovedSource.source_id,
        )
        if allowlist_version is not None:
            statement = statement.where(
                FinanceApprovedSource.allowlist_version == allowlist_version
            )
        records: list[FinanceSourceRecord] = []
        for source in session.scalars(statement):
            health = session.scalar(
                select(FinanceSourceHealth)
                .where(
                    FinanceSourceHealth.source_id == source.source_id,
                    FinanceSourceHealth.source_version == source.source_version,
                )
                .order_by(FinanceSourceHealth.checked_at.desc())
            )
            records.append(
                FinanceSourceRecord(
                    source_id=source.source_id,
                    name=source.name,
                    base_url=source.base_url,
                    classification=source.classification,
                    entitlement=source.entitlement,
                    license_note=source.license_note,
                    license_allows_excerpt=source.license_allows_excerpt,
                    excerpt_max_chars=source.excerpt_max_chars,
                    excerpt_max_words=source.excerpt_max_words,
                    source_version=source.source_version,
                    allowlist_version=source.allowlist_version,
                    enabled=source.enabled,
                    approved_at=_db_utc(source.approved_at),
                    health=health.status if health is not None else None,
                    health_checked_at=_db_utc(health.checked_at if health is not None else None),
                )
            )
        return tuple(records)

    @staticmethod
    def list_run_filter_metadata(
        session: Session, *, limit: int = 100
    ) -> tuple[FinanceRunFilterMetadata, ...]:
        if limit <= 0 or limit > 500:
            raise ValueError("finance run filter metadata limit must be between 1 and 500")
        rows = session.scalars(
            select(FinanceBriefing).order_by(FinanceBriefing.generated_at.desc()).limit(limit)
        )
        return tuple(
            FinanceRunFilterMetadata(
                run_id=row.run_id,
                generated_at=row.generated_at,
                status=row.status,
                source_allowlist_version=row.source_allowlist_version,
                tickers=tuple(row.tickers),
                themes=tuple(row.themes),
            )
            for row in rows
        )


class SQLAlchemyFinanceStore:
    def __init__(self, engine: Engine, *, allowlist_version: str) -> None:
        self._engine = engine
        self._allowlist_version = allowlist_version

    def load_approved_sources(self, *, allowlist_version: str) -> Sequence[SourceApproval]:
        if allowlist_version != self._allowlist_version:
            return ()
        with Session(self._engine) as session:
            return FinanceRepository.load_approved_sources(
                session, allowlist_version=allowlist_version
            )

    def load_portfolio_snapshot(self) -> PortfolioSnapshot:
        with Session(self._engine) as session:
            return FinanceRepository.load_portfolio_snapshot(session)

    def save_briefing_payload(self, payload: BriefingPayload) -> None:
        with Session(self._engine) as session, session.begin():
            FinanceRepository.save_briefing_payload(session, payload)

    def append_thesis_events(self, entries: Sequence[ThesisJournalEntry]) -> None:
        with Session(self._engine) as session, session.begin():
            FinanceRepository.append_thesis_events(session, entries)

    def upsert_etf_exposures(self, exposures: Sequence[ETFExposure]) -> None:
        with Session(self._engine) as session, session.begin():
            FinanceRepository.upsert_etf_exposures(session, exposures)

    def record_source_health(
        self,
        *,
        source_id: str,
        source_version: str,
        status: str,
        checked_at: datetime,
        diagnostic: str | None = None,
    ) -> None:
        with Session(self._engine) as session, session.begin():
            FinanceRepository.record_source_health(
                session,
                source_id=source_id,
                source_version=source_version,
                status=status,
                checked_at=checked_at,
                diagnostic=diagnostic,
            )

    def list_source_records(self) -> tuple[FinanceSourceRecord, ...]:
        with Session(self._engine) as session:
            return FinanceRepository.list_source_records(
                session, allowlist_version=self._allowlist_version
            )

    def list_source_endpoints(
        self, *, source_id: str | None = None, enabled_only: bool = False
    ) -> tuple[FinanceSourceEndpointRecord, ...]:
        with Session(self._engine) as session:
            return FinanceRepository.list_source_endpoints(
                session,
                allowlist_version=self._allowlist_version,
                source_id=source_id,
                enabled_only=enabled_only,
            )

    def load_source_cache_state(
        self, *, source_id: str, endpoint_id: str
    ) -> FinanceSourceCacheStateRecord | None:
        with Session(self._engine) as session:
            return FinanceRepository.load_source_cache_state(
                session,
                allowlist_version=self._allowlist_version,
                source_id=source_id,
                endpoint_id=endpoint_id,
            )

    def save_source_cache_state(
        self,
        *,
        source_id: str,
        endpoint_id: str,
        etag: str | None = None,
        last_modified: str | None = None,
        watermark_external_id: str | None = None,
        watermark_published_at: datetime | None = None,
        cached_artifact_key: str | None = None,
        payload_sha256: str | None = None,
        last_retrieved_at: datetime | None = None,
        last_not_modified_at: datetime | None = None,
    ) -> None:
        with Session(self._engine) as session, session.begin():
            FinanceRepository.save_source_cache_state(
                session,
                allowlist_version=self._allowlist_version,
                source_id=source_id,
                endpoint_id=endpoint_id,
                etag=etag,
                last_modified=last_modified,
                watermark_external_id=watermark_external_id,
                watermark_published_at=watermark_published_at,
                cached_artifact_key=cached_artifact_key,
                payload_sha256=payload_sha256,
                last_retrieved_at=last_retrieved_at,
                last_not_modified_at=last_not_modified_at,
            )

    def record_source_request_audit(
        self,
        *,
        source_id: str,
        endpoint_id: str,
        requested_at: datetime,
        status_code: int | None,
        error_code: str | None,
        not_modified: bool,
    ) -> None:
        with Session(self._engine) as session, session.begin():
            FinanceRepository.record_source_request_audit(
                session,
                allowlist_version=self._allowlist_version,
                source_id=source_id,
                endpoint_id=endpoint_id,
                requested_at=requested_at,
                status_code=status_code,
                error_code=error_code,
                not_modified=not_modified,
            )

    def list_run_filter_metadata(self, *, limit: int = 100) -> tuple[FinanceRunFilterMetadata, ...]:
        with Session(self._engine) as session:
            return FinanceRepository.list_run_filter_metadata(session, limit=limit)

    def source_approval_gate(self) -> bool:
        with Session(self._engine) as session:
            return FinanceRepository.source_approval_gate(
                session, allowlist_version=self._allowlist_version
            )


__all__ = [
    "FinanceApprovedSource",
    "FinanceBriefing",
    "FinanceETFExposure",
    "FinanceHolding",
    "FinanceInvestmentThesis",
    "FinanceRepository",
    "FinanceRunFilterMetadata",
    "FinanceSourceCacheState",
    "FinanceSourceCacheStateRecord",
    "FinanceSourceEndpoint",
    "FinanceSourceEndpointRecord",
    "FinanceSourceHealth",
    "FinanceSourceRecord",
    "FinanceSourceRequestAudit",
    "FinanceThesisEvent",
    "FinanceWatchlistEntry",
    "SQLAlchemyFinanceStore",
]
