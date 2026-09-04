from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.finance.contracts import (
    EventCard,
    ExposureMapping,
    Holding,
    ImpactLabel,
    PortfolioSnapshot,
    QuantValue,
    SourceApproval,
    SourceDocument,
    SourceFetchResult,
    WatchlistItem,
)
from app.agents.finance.delivery import render_discord_briefing
from app.agents.finance.normalization import normalize_documents
from app.agents.finance.workflow import run_finance_briefing

NOW = datetime(2026, 9, 4, 13, tzinfo=UTC)
ALLOWLIST = "finance-sources-v1"
THESIS_ID = uuid4()


def approvals(*, approved: bool = True) -> tuple[SourceApproval, ...]:
    return tuple(
        SourceApproval(
            source_id=f"source{i}",
            name=f"Source {i}",
            base_url=f"https://source{i}.example",
            source_version="v1",
            allowlist_version=ALLOWLIST,
            license_note="Links and short excerpts permitted by test licence.",
            entitlement="test subscription",
            enabled=True,
            approved_at=NOW if approved else None,
            approval_audit_id=uuid4() if approved else None,
        )
        for i in range(1, 9)
    )


class Store:
    def __init__(self, source_records: tuple[SourceApproval, ...]) -> None:
        self.source_records = source_records
        self.payload = None
        self.entries = ()

    def load_approved_sources(self, *, allowlist_version):
        return self.source_records if allowlist_version == ALLOWLIST else ()

    def load_portfolio_snapshot(self):
        return PortfolioSnapshot(
            holdings=(
                Holding(
                    symbol="ACME",
                    name="ACME Corp",
                    quantity=3,
                    market_value=300,
                    tags=("industrial",),
                ),
            ),
            watchlist=(
                WatchlistItem(
                    symbol="ACME",
                    name="ACME Corp",
                    thesis_id=THESIS_ID,
                    themes=("earnings",),
                ),
            ),
        )

    def save_briefing_payload(self, payload):
        self.payload = payload

    def append_thesis_events(self, entries):
        self.entries = tuple(entries)


class Adapter:
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id
        self.calls = 0

    async def fetch(self, query):
        self.calls += 1
        return SourceFetchResult(
            source_id=query.source_id,
            documents=(
                SourceDocument(
                    source_id=query.source_id,
                    source_version=query.source_version,
                    external_id="shared-event",
                    title="ACME reports quarterly revenue growth",
                    url=f"https://{query.source_id}.example/report",
                    published_at=NOW,
                    retrieved_at=NOW,
                    license_allows_excerpt=True,
                    excerpt="Revenue increased in the reported quarter.",
                    tickers=("ACME",),
                    themes=("earnings",),
                    numbers=(
                        QuantValue(
                            label="Revenue growth",
                            value=12,
                            unit="percent",
                            as_of=date(2026, 9, 4),
                            source_ids=(query.source_id,),
                        ),
                    ),
                ),
            ),
        )


class Delivery:
    def __init__(self) -> None:
        self.payloads = []

    async def send_briefing(self, payload, *, idempotency_key):
        self.payloads.append((payload, idempotency_key))


@pytest.mark.asyncio
async def test_finance_run_stays_gated_without_approved_sources_and_does_not_deliver() -> None:
    store = Store(approvals(approved=False))
    delivery = Delivery()
    adapters = {source.source_id: Adapter(source.source_id) for source in approvals()}

    result = await run_finance_briefing(
        run_id=uuid4(),
        store=store,
        adapters=adapters,
        allowlist_version=ALLOWLIST,
        tickers=("ACME",),
        themes=("earnings",),
        delivery=delivery,
        now=NOW,
    )

    assert result["status"] == "approval_required"
    assert result["source_call_count"] == 0
    assert delivery.payloads == []
    assert all(adapter.calls == 0 for adapter in adapters.values())


@pytest.mark.asyncio
async def test_finance_run_normalizes_dedupes_maps_exposure_and_updates_thesis_journal() -> None:
    source_records = approvals()
    store = Store(source_records)
    adapters = {source.source_id: Adapter(source.source_id) for source in source_records}
    delivery = Delivery()

    result = await run_finance_briefing(
        run_id=uuid4(),
        store=store,
        adapters=adapters,
        allowlist_version=ALLOWLIST,
        tickers=("ACME",),
        themes=("earnings",),
        delivery=delivery,
        now=NOW,
    )

    assert result["status"] == "succeeded"
    assert result["source_call_count"] == 8
    assert result["card_count"] == 1
    assert store.payload is not None
    card = store.payload.cards[0]
    assert card.exposure.holding_symbols == ("ACME",)
    assert card.exposure.watchlist_symbols == ("ACME",)
    assert len(card.citations) == 8
    assert len(store.entries) == 1
    assert store.entries[0].thesis_id == THESIS_ID
    rendered = render_discord_briefing(store.payload)
    assert "ACME reports" in rendered
    assert "raw_body" not in rendered
    assert len(delivery.payloads) == 1
    assert delivery.payloads[0][1] == "finance:2026-09-04:market-open:v1"


def test_normalization_rejects_unlicensed_excerpt_and_stale_documents() -> None:
    with pytest.raises(ValidationError, match="excerpt is not permitted"):
        SourceDocument(
            source_id="source1",
            source_version="v1",
            external_id="event-1",
            title="ACME reports quarterly revenue growth",
            url="https://source1.example/report",
            published_at=NOW,
            retrieved_at=NOW,
            license_allows_excerpt=False,
            excerpt="This excerpt is not allowed.",
            tickers=("ACME",),
            themes=(),
        )

    stale = SourceDocument(
        source_id="source1",
        source_version="v1",
        external_id="event-1",
        title="ACME reports quarterly revenue growth",
        url="https://source1.example/report",
        published_at=NOW - timedelta(days=30),
        retrieved_at=NOW - timedelta(days=30),
        tickers=("ACME",),
        themes=(),
    )
    with pytest.raises(ValueError, match="freshness"):
        normalize_documents((stale,), approved_sources=approvals(), now=NOW)


def test_event_card_schema_rejects_trade_directives_and_uncited_numbers() -> None:
    exposure = ExposureMapping(event_id="event-1")
    with pytest.raises(ValidationError, match="trade directives"):
        EventCard(
            event_id="event-1",
            title="ACME update",
            verified_facts=("ACME issued guidance.",),
            uncertainty="There is execution risk.",
            counter_case="Buy ACME before next quarter.",
            impact_label=ImpactLabel.MONITOR,
            exposure=exposure,
            citations=("source1",),
        )

    with pytest.raises(ValidationError, match="sources present"):
        EventCard(
            event_id="event-1",
            title="ACME update",
            verified_facts=("ACME issued guidance.",),
            uncertainty="There is execution risk.",
            counter_case="The event may not persist.",
            impact_label=ImpactLabel.MONITOR,
            exposure=exposure,
            citations=("source1",),
            numbers=(
                QuantValue(
                    label="Derived growth",
                    value=5,
                    unit="percent",
                    as_of=date(2026, 9, 4),
                    source_ids=("source2",),
                    formula="source2 growth minus source1 baseline",
                ),
            ),
        )
