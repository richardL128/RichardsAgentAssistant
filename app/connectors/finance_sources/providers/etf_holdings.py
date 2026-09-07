"""Reviewed issuer ETF holdings parsers."""

from __future__ import annotations

import csv
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import StringIO

from app.agents.finance.contracts import ETFExposure

from .common import ProviderDiagnostic, ProviderParseError, parse_period_date, text
from .registries import ETF_HOLDINGS_REGISTRY, ISSUER_ETF_HOLDINGS


@dataclass(frozen=True, slots=True)
class EtfHoldingsParseResult:
    etf_symbol: str
    as_of: date
    source_url: str
    retrieved_at: datetime
    exposures: tuple[ETFExposure, ...]

    @property
    def stale(self) -> bool:
        return self.as_of < self.retrieved_at.date()


def etf_missing_mapping_diagnostics(tickers: tuple[str, ...]) -> tuple[ProviderDiagnostic, ...]:
    return tuple(
        ProviderDiagnostic(
            code="etf_holdings_mapping_missing",
            diagnostic=(
                "No reviewed issuer holdings endpoint is configured "
                f"for ETF {ticker.upper()}"
            ),
            ticker=ticker.upper(),
        )
        for ticker in tickers
        if ticker.upper() not in ETF_HOLDINGS_REGISTRY
    )


def parse_ishares_holdings_csv(
    payload: bytes | str,
    *,
    etf_symbol: str,
    retrieved_at: datetime,
) -> EtfHoldingsParseResult:
    endpoint = ETF_HOLDINGS_REGISTRY.get(etf_symbol.upper())
    if endpoint is None or endpoint.parser != "ishares_csv":
        raise ValueError(f"unsupported ETF holdings ticker: {etf_symbol.upper()}")
    raw = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
    rows = tuple(csv.reader(StringIO(raw)))
    as_of = _ishares_as_of(rows)
    header_index = _header_index(rows)
    holdings = rows[header_index + 1 :]
    exposures: list[ETFExposure] = []
    for row in holdings:
        record = _row_mapping(rows[header_index], row)
        symbol = text(record.get("Ticker")).upper()
        if not symbol or symbol in {"-", "CASH"}:
            continue
        weight = _weight(record)
        if weight is None:
            continue
        exposures.append(
            ETFExposure.model_validate(
                {
                    "etf_symbol": endpoint.ticker,
                    "underlying_symbol": symbol,
                    "weight_percent": weight,
                    "source_id": ISSUER_ETF_HOLDINGS,
                    "as_of": as_of,
                    "source_url": endpoint.holdings_url,
                    "retrieved_at": retrieved_at.astimezone(UTC),
                }
            )
        )
    return EtfHoldingsParseResult(
        etf_symbol=endpoint.ticker,
        as_of=as_of,
        source_url=endpoint.holdings_url,
        retrieved_at=retrieved_at.astimezone(UTC),
        exposures=tuple(exposures),
    )


def _ishares_as_of(rows: tuple[list[str], ...]) -> date:
    for row in rows[:20]:
        joined = ",".join(cell.strip() for cell in row if cell.strip())
        if "as of" not in joined.casefold():
            continue
        candidate = joined.casefold().split("as of", maxsplit=1)[1].strip(" ,")
        parsed_candidate = _month_day_year(candidate)
        if parsed_candidate is not None:
            return parsed_candidate
        for cell in row:
            if not _looks_like_complete_date(cell):
                continue
            parsed = parse_period_date(cell)
            if parsed is not None:
                return parsed
    raise ProviderParseError("iShares holdings CSV is missing upstream as-of date")


def _header_index(rows: tuple[list[str], ...]) -> int:
    for index, row in enumerate(rows):
        normalized = {cell.strip().casefold() for cell in row}
        if "ticker" in normalized and "weight (%)" in normalized:
            return index
    raise ProviderParseError("iShares holdings CSV is missing holdings header")


def _row_mapping(header: list[str], row: list[str]) -> Mapping[str, str]:
    return {
        name.strip(): row[index].strip()
        for index, name in enumerate(header)
        if index < len(row)
    }


def _weight(record: Mapping[str, str]) -> float | None:
    value = text(record.get("Weight (%)")).replace("%", "").replace(",", "")
    if not value:
        return None
    try:
        weight = float(value)
    except ValueError:
        return None
    return weight if 0 <= weight <= 100 else None


_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _month_day_year(value: str) -> date | None:
    match = re.search(r"([a-z]+)\s+(\d{1,2}),?\s*(\d{4})", value.casefold())
    if match is None:
        return None
    month = _MONTHS.get(match.group(1))
    if month is None:
        return None
    day = int(match.group(2))
    year = int(match.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _looks_like_complete_date(value: str) -> bool:
    normalized = value.strip().casefold()
    return bool(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized)
        or re.search(r"[a-z]+\s+\d{1,2},?\s*\d{4}", normalized)
    )
