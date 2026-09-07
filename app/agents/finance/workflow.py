"""Phase 6 finance briefing orchestration with a hard source approval gate."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from app.agents.finance.contracts import (
    BriefingPayload,
    ETFExposure,
    EventCard,
    ImpactLabel,
    PortfolioSnapshot,
    SourceApproval,
    SourceFailure,
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
from app.connectors.finance_sources.transport import EndpointRequestAudit

_FEED_LATENCY_SOURCE_IDS = {
    "defense_gov_rss",
    "breaking_defense_public",
    "sec_edgar",
    "company_ir_registry",
    "technology_official_feeds",
}
_FEED_LATENCY_TARGET_SECONDS = 15 * 60


class FinanceStore(Protocol):
    def load_approved_sources(self, *, allowlist_version: str) -> Sequence[SourceApproval]: ...

    def load_portfolio_snapshot(self) -> PortfolioSnapshot: ...

    def save_briefing_payload(self, payload: BriefingPayload) -> None: ...

    def append_thesis_events(self, entries: Sequence[ThesisJournalEntry]) -> None: ...

    def upsert_etf_exposures(self, exposures: Sequence[ETFExposure]) -> None: ...

    def record_source_request_audit(
        self,
        *,
        source_id: str,
        endpoint_id: str,
        requested_at: datetime,
        status_code: int | None,
        error_code: str | None,
        not_modified: bool,
    ) -> None: ...

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
    issuer_tickers: Sequence[str] = (),
    etf_tickers: Sequence[str] = (),
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
        issuer_tickers=issuer_tickers,
        etf_tickers=etf_tickers,
    )
    approval_by_id = {approval.source_id: approval for approval in approvals}
    logical_failures = {
        result.source_id: _result_failure(result)
        for result in results
    }
    for result in results:
        approval = approval_by_id[result.source_id]
        logical_failure = logical_failures[result.source_id]
        store.record_source_health(
            source_id=result.source_id,
            source_version=approval.source_version,
            status=_source_health_status(result),
            checked_at=current,
            diagnostic=logical_failure.diagnostic if logical_failure is not None else None,
        )
    failures = tuple(failure for failure in logical_failures.values() if failure is not None)
    etf_exposures = tuple(exposure for result in results for exposure in result.etf_exposures)
    if etf_exposures:
        store.upsert_etf_exposures(etf_exposures)
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
        "source_request_count": sum(
            result.metadata.request_count for result in results if result.metadata is not None
        ),
        "source_failure_count": len(failures),
        "card_count": len(cards),
    }


async def run_finance(run_id: str, idempotency_key: str) -> dict[str, object]:
    """Worker entry point with lazily constructed host integrations."""

    parsed_run_id = uuid.UUID(run_id)
    runtime = _runtime or await _load_default_runtime(parsed_run_id)
    try:
        snapshot = runtime.store.load_portfolio_snapshot()
        tickers, themes, issuer_tickers, etf_tickers = _portfolio_scope(snapshot)
        result = await run_finance_briefing(
            run_id=parsed_run_id,
            store=runtime.store,
            adapters=runtime.adapters,
            allowlist_version=runtime.allowlist_version,
            tickers=tickers,
            themes=themes,
            issuer_tickers=issuer_tickers,
            etf_tickers=etf_tickers,
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
    from app.artifacts.store import ArtifactStore
    from app.connectors.finance_sources.cache import (
        EndpointStateStore,
        EndpointWatermark,
        PersistentEndpointStateStore,
    )
    from app.connectors.finance_sources.definitions import RawSourcePayload
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
        def record_request(source_id: str, audit: EndpointRequestAudit) -> None:
            store.record_source_request_audit(
                source_id=source_id,
                endpoint_id=audit.endpoint_id,
                requested_at=audit.requested_at,
                status_code=audit.status_code,
                error_code=audit.error_code,
                not_modified=audit.not_modified,
            )

        def state_store_for(source_id: str) -> EndpointStateStore:
            def load(endpoint_id: str) -> EndpointWatermark | None:
                record = store.load_source_cache_state(
                    source_id=source_id,
                    endpoint_id=endpoint_id,
                )
                if record is None:
                    return None
                seen: frozenset[str] = (
                    frozenset({record.watermark_external_id})
                    if record.watermark_external_id is not None
                    else frozenset[str]()
                )
                return EndpointWatermark(
                    endpoint_id=endpoint_id,
                    etag=record.etag,
                    last_modified=record.last_modified,
                    high_watermark=record.watermark_published_at,
                    seen_external_ids=seen,
                    artifact_ref=record.cached_artifact_key,
                    retrieved_at=record.last_retrieved_at,
                    not_modified_at=record.last_not_modified_at,
                )

            def save(watermark: EndpointWatermark) -> None:
                last_external_id = (
                    sorted(watermark.seen_external_ids)[-1]
                    if watermark.seen_external_ids
                    else None
                )
                store.save_source_cache_state(
                    source_id=source_id,
                    endpoint_id=watermark.endpoint_id,
                    etag=watermark.etag,
                    last_modified=watermark.last_modified,
                    watermark_external_id=last_external_id,
                    watermark_published_at=watermark.high_watermark,
                    cached_artifact_key=watermark.artifact_ref,
                    payload_sha256=watermark.artifact_ref,
                    last_retrieved_at=watermark.retrieved_at,
                    last_not_modified_at=watermark.not_modified_at,
                )

            return PersistentEndpointStateStore(loader=load, saver=save)

        artifact_store = ArtifactStore(
            settings.artifact_root,
            default_retention_days=settings.artifact_retention_days,
        )

        def persist_bulk_artifact(
            _source_id: str, payload: RawSourcePayload
        ) -> str:
            metadata = artifact_store.put(
                payload.body,
                media_type=payload.content_type or "application/octet-stream",
                data_class="finance-source-cache",
                already_redacted=True,
            )
            return metadata.key

        adapters = build_finance_adapter_registry(
            settings,
            approvals,
            client=client,
            request_audit=record_request,
            state_store_factory=state_store_for,
            bulk_artifact=persist_bulk_artifact,
        )
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


def _portfolio_scope(
    snapshot: PortfolioSnapshot,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
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
    issuer_tickers = tuple(
        dict.fromkeys(
            symbol.upper()
            for symbol in (
                *(holding.symbol for holding in snapshot.holdings),
                *(item.symbol for item in snapshot.watchlist),
                *(exposure.underlying_symbol for exposure in snapshot.etf_exposures),
            )
        )
    )[:100]
    etf_tickers = tuple(
        dict.fromkeys(exposure.etf_symbol.upper() for exposure in snapshot.etf_exposures)
    )[:100]
    return tickers, themes, issuer_tickers, etf_tickers


def _source_health_status(result: SourceFetchResult) -> str:
    if result.failure is None:
        if result.metadata is not None and (
            result.metadata.endpoint_failures
            or result.metadata.stale
            or _latency_target_missed(result)
        ):
            return "attention"
        return "healthy"
    if result.failure.error_code in {"connector_auth", "input_invalid", "request_invalid"}:
        return "failed"
    return "attention"


def _result_failure(result: SourceFetchResult) -> SourceFailure | None:
    if result.failure is not None:
        return result.failure
    metadata = result.metadata
    if metadata is None:
        return None
    if metadata.endpoint_failures:
        return SourceFailure(
            source_id=result.source_id,
            error_code="partial_registry_failure",
            diagnostic=(
                f"{len(metadata.endpoint_failures)} of {metadata.endpoint_count} "
                "reviewed finance endpoints failed"
            ),
        )
    if metadata.stale:
        return SourceFailure(
            source_id=result.source_id,
            error_code="source_stale",
            diagnostic="finance source data is stale against its reviewed schedule",
        )
    if _latency_target_missed(result):
        return SourceFailure(
            source_id=result.source_id,
            error_code="ingestion_latency_target_missed",
            diagnostic="finance feed ingestion p95 exceeded the 15-minute target",
        )
    return None


def _latency_target_missed(result: SourceFetchResult) -> bool:
    metadata = result.metadata
    return (
        result.source_id in _FEED_LATENCY_SOURCE_IDS
        and metadata is not None
        and metadata.ingestion_latency_seconds is not None
        and metadata.ingestion_latency_seconds > _FEED_LATENCY_TARGET_SECONDS
    )


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
