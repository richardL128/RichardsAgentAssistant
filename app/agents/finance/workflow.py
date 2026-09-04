"""Phase 6 finance briefing orchestration with a hard source approval gate."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.agents.finance.contracts import (
    BriefingPayload,
    EventCard,
    ImpactLabel,
    PortfolioSnapshot,
    SourceApproval,
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
    "run_finance_briefing",
]
