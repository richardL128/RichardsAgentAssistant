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
    SourceEndpointFailure,
    SourceFailure,
    SourceFetchMetadata,
    SourceFetchResult,
    WatchlistItem,
)
from app.agents.finance.delivery import render_discord_briefing
from app.agents.finance.normalization import normalize_documents
from app.agents.finance.workflow import (
    configure_finance_runtime,
    run_finance,
    run_finance_briefing,
)

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
        self.health = []
        self.etf_exposures = ()
        self.request_audits = []

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

    def upsert_etf_exposures(self, exposures):
        self.etf_exposures = tuple(exposures)

    def record_source_request_audit(self, **audit):
        self.request_audits.append(audit)

    def record_source_health(
        self,
        *,
        source_id,
        source_version,
        status,
        checked_at,
        diagnostic=None,
    ):
        self.health.append(
            {
                "source_id": source_id,
                "source_version": source_version,
                "status": status,
                "checked_at": checked_at,
                "diagnostic": diagnostic,
            }
        )


class Adapter:
    def __init__(self, source_id: str, *, fail: bool = False) -> None:
        self.source_id = source_id
        self.fail = fail
        self.calls = 0

    async def fetch(self, query):
        self.calls += 1
        if self.fail:
            return SourceFetchResult(
                source_id=query.source_id,
                failure=SourceFailure(
                    source_id=query.source_id,
                    error_code="connector_timeout",
                    diagnostic="finance source request timed out",
                ),
            )
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


class PartialRegistryAdapter(Adapter):
    async def fetch(self, query):
        result = await super().fetch(query)
        return result.model_copy(
            update={
                "metadata": SourceFetchMetadata(
                    transport="registry",
                    request_count=2,
                    endpoint_count=2,
                    endpoint_failures=(
                        SourceEndpointFailure(
                            endpoint_id="issuer-missing",
                            error_code="connector_timeout",
                            diagnostic="reviewed issuer endpoint timed out",
                        ),
                    ),
                )
            }
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
    assert store.health == []


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
    assert len(store.health) == 8
    assert {record["status"] for record in store.health} == {"healthy"}


@pytest.mark.asyncio
async def test_finance_run_records_failed_adapter_health_as_attention() -> None:
    source_records = approvals()
    store = Store(source_records)
    adapters = {
        source.source_id: Adapter(source.source_id, fail=source.source_id == "source8")
        for source in source_records
    }

    result = await run_finance_briefing(
        run_id=uuid4(),
        store=store,
        adapters=adapters,
        allowlist_version=ALLOWLIST,
        tickers=("ACME",),
        themes=("earnings",),
        now=NOW,
    )

    assert result["status"] == "attention"
    assert result["source_failure_count"] == 1
    statuses = {record["source_id"]: record["status"] for record in store.health}
    assert statuses["source8"] == "attention"
    assert set(statuses.values()) == {"healthy", "attention"}


@pytest.mark.asyncio
async def test_partial_registry_failure_stays_one_logical_call_and_marks_attention() -> None:
    source_records = approvals()
    store = Store(source_records)
    adapters = {
        source.source_id: (
            PartialRegistryAdapter(source.source_id)
            if source.source_id == "source8"
            else Adapter(source.source_id)
        )
        for source in source_records
    }

    result = await run_finance_briefing(
        run_id=uuid4(),
        store=store,
        adapters=adapters,
        allowlist_version=ALLOWLIST,
        tickers=("ACME",),
        themes=("earnings",),
        now=NOW,
    )

    assert result["status"] == "attention"
    assert result["source_call_count"] == 8
    assert result["source_request_count"] == 2
    assert result["source_failure_count"] == 1
    assert sum(adapter.calls for adapter in adapters.values()) == 8
    health = {record["source_id"]: record for record in store.health}
    assert health["source8"]["status"] == "attention"
    assert "reviewed finance endpoints failed" in health["source8"]["diagnostic"]


def test_importing_worker_does_not_register_finance_model_handler() -> None:
    import app.queue.worker  # noqa: F401
    from app.agents.finance.workflow import run_finance
    from app.queue import tasks

    assert tasks._handlers.get("finance") is not run_finance
    assert "finance" not in tasks._handlers


@pytest.mark.asyncio
async def test_finance_worker_entry_uses_injected_runtime_and_stays_gated(monkeypatch) -> None:
    from app.agents.finance import workflow

    monkeypatch.setattr(workflow, "_runtime", None)
    store = Store(approvals(approved=False))
    adapters = {source.source_id: Adapter(source.source_id) for source in approvals()}
    configure_finance_runtime(store, adapters, allowlist_version=ALLOWLIST)
    run_id = uuid4()
    key = "finance:2026-09-04:market-open:v1"

    result = await run_finance(str(run_id), key)

    assert result == {
        "status": "approval_required",
        "run_id": str(run_id),
        "delivered": False,
        "source_call_count": 0,
        "diagnostic": "finance source approval gate is not satisfied",
        "idempotency_key": key,
    }
    assert all(adapter.calls == 0 for adapter in adapters.values())


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
    result = normalize_documents(
        (stale,),
        approved_sources=approvals(),
        now=NOW,
        include_diagnostics=True,
    )
    assert result.events == ()
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].reason == "stale_document"

    unapproved = stale.model_copy(update={"source_id": "unknown-source"})
    with pytest.raises(ValueError, match="not approved"):
        normalize_documents((unapproved,), approved_sources=approvals(), now=NOW)


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
