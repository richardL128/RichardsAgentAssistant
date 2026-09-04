"""Phase 6 finance briefing orchestration with a hard source approval gate."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from app.agents.finance.contracts import (
    BriefingPayload,
    EventCard,
    ImpactLabel,
    PortfolioSnapshot,
    SourceApproval,
    SourceFetchResult,
    ThesisJournalEntry,
)
from app.agents.finance.exposure import map_events_to_exposure
from app.agents.finance.normalization import normalize_documents
from app.connectors.finance_sources import (
    FinanceSourceAdapter,
    check_source_gate,
    fetch_exactly_eight_sources,
)


class FinanceStore(Protocol):
    def load_approved_sources(self, *, allowlist_version: str) -> Sequence[SourceApproval]: ...

    def load_portfolio_snapshot(self) -> PortfolioSnapshot: ...

    def save_briefing_payload(self, payload: BriefingPayload) -> None: ...

    def append_thesis_events(self, entries: Sequence[ThesisJournalEntry]) -> None: ...

    def record_source_health(
        self,
        *,
        source_id: str,
        source_version: str,
        status: str,
        checked_at: datetime,
        diagnostic: str | None = None,
    ) -> None: ...


class FinanceModelGateway(Protocol):
    async def event_cards(
        self,
        *,
        events: Sequence[object],
        exposures: Sequence[object],
    ) -> Sequence[EventCard]: ...


class FinanceDelivery(Protocol):
    async def send_briefing(self, payload: BriefingPayload, *, idempotency_key: str) -> object: ...


async def run_finance_briefing(
    *,
    run_id: uuid.UUID,
    store: FinanceStore,
    adapters: Mapping[str, FinanceSourceAdapter],
    allowlist_version: str,
    tickers: Sequence[str],
    themes: Sequence[str],
    delivery: FinanceDelivery | None = None,
    model: FinanceModelGateway | None = None,
    now: datetime | None = None,
    window: timedelta = timedelta(hours=24),
) -> dict[str, object]:
    """Run the finance briefing only when exactly eight approved sources exist."""

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    approvals = tuple(store.load_approved_sources(allowlist_version=allowlist_version))
    gate = check_source_gate(approvals, allowlist_version=allowlist_version)
    if not gate.enabled:
        return {
            "status": "approval_required",
            "run_id": str(run_id),
            "delivered": False,
            "source_call_count": 0,
            "diagnostic": gate.diagnostic,
        }

    results = await fetch_exactly_eight_sources(
        adapters=adapters,
        approved_sources=approvals,
        allowlist_version=allowlist_version,
        window_start=current - window,
        window_end=current,
        tickers=tickers,
        themes=themes,
    )
    approval_by_id = {approval.source_id: approval for approval in approvals}
    for result in results:
        approval = approval_by_id[result.source_id]
        store.record_source_health(
            source_id=result.source_id,
            source_version=approval.source_version,
            status=_source_health_status(result),
            checked_at=current,
            diagnostic=result.failure.diagnostic if result.failure is not None else None,
        )
    failures = tuple(result.failure for result in results if result.failure is not None)
    documents = tuple(document for result in results for document in result.documents)
    events = normalize_documents(documents, approved_sources=approvals, now=current)
    exposures = map_events_to_exposure(events, store.load_portfolio_snapshot())
    cards = (
        tuple(
            await model.event_cards(
                events=events,
                exposures=exposures,
            )
        )
        if model is not None
        else tuple(
            _deterministic_card(event_index=index, event=event, exposure=exposures[index])
            for index, event in enumerate(events)
        )
    )
    payload = BriefingPayload(
        run_id=run_id,
        source_allowlist_version=allowlist_version,
        generated_at=current,
        status="attention" if failures else "succeeded",
        cards=cards,
        source_failures=failures,
        tickers=tuple(tickers),
        themes=tuple(themes),
    )
    store.save_briefing_payload(payload)
    store.append_thesis_events(_journal_entries(payload))
    delivered = False
    if delivery is not None:
        await delivery.send_briefing(
            payload,
            idempotency_key=f"finance:{current.date().isoformat()}:market-open:v1",
        )
        delivered = True
    return {
        "status": payload.status,
        "run_id": str(run_id),
        "delivered": delivered,
        "source_call_count": len(results),
        "source_failure_count": len(failures),
        "card_count": len(cards),
    }


async def run_finance(run_id: str, idempotency_key: str) -> dict[str, object]:
    """Worker entry point with lazily constructed host integrations."""

    parsed_run_id = uuid.UUID(run_id)
    runtime = _runtime or await _load_default_runtime(parsed_run_id)
    try:
        snapshot = runtime.store.load_portfolio_snapshot()
        tickers, themes = _portfolio_scope(snapshot)
        result = await run_finance_briefing(
            run_id=parsed_run_id,
            store=runtime.store,
            adapters=runtime.adapters,
            allowlist_version=runtime.allowlist_version,
            tickers=tickers,
            themes=themes,
            delivery=runtime.delivery,
            model=runtime.model,
        )
        result.update({"idempotency_key": idempotency_key})
        return result
    finally:
        if runtime.owned_client is not None:
            await runtime.owned_client.aclose()
        if runtime.dispose_database is not None:
            runtime.dispose_database()


class _Runtime:
    def __init__(
        self,
        store: FinanceStore,
        adapters: Mapping[str, FinanceSourceAdapter],
        allowlist_version: str,
        delivery: FinanceDelivery | None,
        model: FinanceModelGateway | None,
        *,
        owned_client: httpx.AsyncClient | None = None,
        dispose_database: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.adapters = adapters
        self.allowlist_version = allowlist_version
        self.delivery = delivery
        self.model = model
        self.owned_client = owned_client
        self.dispose_database = dispose_database


_runtime: _Runtime | None = None


async def _load_default_runtime(run_id: uuid.UUID) -> _Runtime:
    """Load the SQL, source, and optional Discord runtime for one worker run."""

    from app.agents.finance.sources import build_finance_adapter_registry
    from app.core.config import get_settings
    from app.db.finance import SQLAlchemyFinanceStore
    from app.db.session import Database

    settings = get_settings()
    database = Database(settings)
    engine = database.engine
    store = SQLAlchemyFinanceStore(
        engine,
        allowlist_version=settings.finance_source_allowlist_version,
    )
    approvals = store.load_approved_sources(
        allowlist_version=settings.finance_source_allowlist_version
    )
    client = httpx.AsyncClient()
    try:
        adapters = build_finance_adapter_registry(settings, approvals, client=client)
        delivery = None
        token = settings.discord_bot_token
        channel_id = settings.discord_finance_channel_id
        if token is not None and channel_id is not None:
            from app.connectors.discord import (
                DiscordFinanceBriefingAdapter,
                DiscordFinanceBriefingDelivery,
            )

            adapter = DiscordFinanceBriefingAdapter(
                token=token,
                allowed_channel_ids={channel_id},
            )
            delivery = DiscordFinanceBriefingDelivery(
                engine=engine,
                run_id=run_id,
                channel_id=channel_id,
                adapter=adapter,
            )
    except Exception:
        await client.aclose()
        database.dispose()
        raise
    return _Runtime(
        store,
        adapters,
        settings.finance_source_allowlist_version,
        delivery,
        None,
        owned_client=client,
        dispose_database=database.dispose,
    )


def configure_finance_runtime(
    store: FinanceStore,
    adapters: Mapping[str, FinanceSourceAdapter],
    *,
    allowlist_version: str,
    delivery: FinanceDelivery | None = None,
    model: FinanceModelGateway | None = None,
) -> None:
    """Inject host integrations for a finance worker process or test."""

    if not allowlist_version.strip():
        raise ValueError("allowlist_version must not be empty")
    global _runtime
    _runtime = _Runtime(store, adapters, allowlist_version, delivery, model)


def _portfolio_scope(snapshot: PortfolioSnapshot) -> tuple[tuple[str, ...], tuple[str, ...]]:
    tickers = tuple(
        dict.fromkeys(
            symbol.upper()
            for symbol in (
                *(holding.symbol for holding in snapshot.holdings),
                *(item.symbol for item in snapshot.watchlist),
                *(exposure.etf_symbol for exposure in snapshot.etf_exposures),
                *(exposure.underlying_symbol for exposure in snapshot.etf_exposures),
            )
        )
    )[:100]
    themes = tuple(
        dict.fromkeys(
            theme.casefold()
            for theme in (
                *(theme for holding in snapshot.holdings for theme in holding.tags),
                *(theme for item in snapshot.watchlist for theme in item.themes),
            )
        )
    )[:50]
    return tickers, themes


def _source_health_status(result: SourceFetchResult) -> str:
    if result.failure is None:
        return "healthy"
    if result.failure.error_code in {"connector_auth", "input_invalid", "request_invalid"}:
        return "failed"
    return "attention"


def _deterministic_card(
    *,
    event_index: int,
    event: object,
    exposure: object,
) -> EventCard:
    from app.agents.finance.contracts import ExposureMapping, NormalizedEvent

    typed_event = (
        event if isinstance(event, NormalizedEvent) else NormalizedEvent.model_validate(event)
    )
    typed_exposure = (
        exposure
        if isinstance(exposure, ExposureMapping)
        else ExposureMapping.model_validate(exposure)
    )
    return EventCard(
        event_id=typed_event.event_id,
        title=typed_event.title,
        verified_facts=typed_event.verified_facts,
        uncertainty="Uncertainty remains until follow-up source coverage confirms durability.",
        counter_case="The event may be temporary or already reflected in market expectations.",
        impact_label=ImpactLabel.MONITOR if event_index < 10 else ImpactLabel.NO_ACTION,
        exposure=typed_exposure,
        citations=typed_event.source_ids,
        numbers=(*typed_event.numbers, *typed_exposure.derived_numbers),
    )


def _journal_entries(payload: BriefingPayload) -> tuple[ThesisJournalEntry, ...]:
    entries: list[ThesisJournalEntry] = []
    for card in payload.cards:
        if card.impact_label is not ImpactLabel.NO_ACTION:
            entries.extend(
                ThesisJournalEntry(
                    thesis_id=thesis_id,
                    event_id=card.event_id,
                    impact_label=card.impact_label,
                    rationale="Finance briefing event matched thesis exposure.",
                    counter_case=card.counter_case,
                    created_at=payload.generated_at,
                )
                for thesis_id in card.exposure.thesis_ids
            )
    return tuple(entries)


__all__ = [
    "FinanceDelivery",
    "FinanceModelGateway",
    "FinanceStore",
    "configure_finance_runtime",
    "run_finance",
    "run_finance_briefing",
]
