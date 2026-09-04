"""Map normalized finance events to manual holdings, watchlist, and ETF exposure."""

from __future__ import annotations

from app.agents.finance.contracts import (
    ExposureMapping,
    NormalizedEvent,
    PortfolioSnapshot,
    QuantValue,
)


def map_event_exposure(event: NormalizedEvent, snapshot: PortfolioSnapshot) -> ExposureMapping:
    symbols = {symbol.casefold() for symbol in event.tickers}
    holding_symbols = tuple(
        holding.symbol for holding in snapshot.holdings if holding.symbol.casefold() in symbols
    )
    watchlist_matches = tuple(
        item for item in snapshot.watchlist if item.symbol.casefold() in symbols
    )
    etf_matches = tuple(
        exposure
        for exposure in snapshot.etf_exposures
        if exposure.underlying_symbol.casefold() in symbols
        or exposure.etf_symbol.casefold() in symbols
    )
    thesis_ids = tuple(item.thesis_id for item in watchlist_matches if item.thesis_id is not None)
    derived_numbers = tuple(
        QuantValue(
            label=f"{exposure.etf_symbol} look-through weight in {exposure.underlying_symbol}",
            value=exposure.weight_percent,
            unit="percent",
            as_of=exposure.as_of,
            source_ids=(exposure.source_id,),
            formula="ETF underlying weight percent from approved holdings disclosure",
        )
        for exposure in etf_matches
    )
    notes: list[str] = []
    if holding_symbols:
        notes.append("Matched manually managed holding symbols.")
    if watchlist_matches:
        notes.append("Matched watchlist research leads.")
    if etf_matches:
        notes.append("Matched ETF look-through exposure.")
    return ExposureMapping(
        event_id=event.event_id,
        holding_symbols=holding_symbols,
        watchlist_symbols=tuple(item.symbol for item in watchlist_matches),
        etf_symbols=tuple(dict.fromkeys(item.etf_symbol for item in etf_matches)),
        thesis_ids=thesis_ids,
        exposure_notes=tuple(notes),
        derived_numbers=derived_numbers,
    )


def map_events_to_exposure(
    events: tuple[NormalizedEvent, ...], snapshot: PortfolioSnapshot
) -> tuple[ExposureMapping, ...]:
    return tuple(map_event_exposure(event, snapshot) for event in events)
