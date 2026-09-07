"""SEC EDGAR submissions registry helpers and parser."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from app.agents.finance.contracts import SourceClassification, SourceDocument, SourceQuery

from .common import (
    JsonObject,
    ProviderDiagnostic,
    ProviderParseError,
    in_query_window,
    parse_json_object,
    parse_period_date,
    text,
    unique_lower,
    unique_upper,
)
from .registries import SEC_EDGAR, SEC_SOURCE_VERSION, SEC_TICKER_REGISTRY, SecCompany


def sec_missing_mapping_diagnostics(tickers: tuple[str, ...]) -> tuple[ProviderDiagnostic, ...]:
    return tuple(
        ProviderDiagnostic(
            code="sec_cik_mapping_missing",
            diagnostic=f"No audited SEC CIK mapping is configured for ticker {ticker.upper()}",
            ticker=ticker.upper(),
        )
        for ticker in tickers
        if ticker.upper() not in SEC_TICKER_REGISTRY
    )


def sec_companies_for_tickers(tickers: tuple[str, ...]) -> tuple[SecCompany, ...]:
    return tuple(
        company
        for ticker in unique_upper(tickers)
        if (company := SEC_TICKER_REGISTRY.get(ticker)) is not None
    )


def parse_sec_submissions(
    payload: bytes | str | JsonObject,
    query: SourceQuery,
    retrieved_at: datetime,
    *,
    ticker: str,
) -> tuple[SourceDocument, ...]:
    company = SEC_TICKER_REGISTRY.get(ticker.upper())
    if company is None:
        raise ValueError(f"unsupported SEC ticker: {ticker.upper()}")
    root = parse_json_object(payload)
    recent = _recent_filings(root)
    documents: list[SourceDocument] = []
    for filing in _filings(recent):
        if filing["form"] not in company.forms:
            continue
        filing_date = parse_period_date(filing["filing_date"])
        if filing_date is None:
            continue
        published_at = datetime(
            filing_date.year,
            filing_date.month,
            filing_date.day,
            tzinfo=retrieved_at.tzinfo,
        )
        if not in_query_window(published_at, query, retrieved_at):
            continue
        accession = filing["accession_number"]
        try:
            documents.append(
                SourceDocument.model_validate(
                    {
                        "source_id": SEC_EDGAR,
                        "source_version": SEC_SOURCE_VERSION,
                        "external_id": f"{company.cik}:{accession}",
                        "title": f"{company.issuer} {filing['form']} filed {filing['filing_date']}",
                        "url": _filing_url(company.cik, accession, filing["primary_document"]),
                        "published_at": published_at,
                        "retrieved_at": retrieved_at,
                        "classification": SourceClassification.PRIMARY,
                        "license_allows_excerpt": False,
                        "tickers": unique_upper((*query.tickers, company.ticker)),
                        "themes": unique_lower((*query.themes, "sec-filing")),
                    }
                )
            )
        except ValidationError:
            continue
    return tuple(documents)


def _recent_filings(root: JsonObject) -> JsonObject:
    filings = root.get("filings")
    if not isinstance(filings, Mapping):
        raise ProviderParseError("SEC submissions payload is missing filings")
    filings_object = cast(JsonObject, filings)
    recent = filings_object.get("recent")
    if not isinstance(recent, Mapping):
        raise ProviderParseError("SEC submissions payload is missing recent filings")
    return cast(JsonObject, recent)


def _filings(recent: JsonObject) -> tuple[dict[str, str], ...]:
    accessions = _string_column(recent, "accessionNumber")
    forms = _string_column(recent, "form")
    filing_dates = _string_column(recent, "filingDate")
    primary_documents = _string_column(recent, "primaryDocument")
    count = min(len(accessions), len(forms), len(filing_dates), len(primary_documents))
    return tuple(
        {
            "accession_number": accessions[index],
            "form": forms[index],
            "filing_date": filing_dates[index],
            "primary_document": primary_documents[index],
        }
        for index in range(count)
    )


def _string_column(root: JsonObject, key: str) -> tuple[str, ...]:
    value = root.get(key)
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    return tuple(text(item) for item in cast(Sequence[object], value) if text(item))


def _filing_url(cik: str, accession: str, primary_document: str) -> str:
    numeric_cik = str(int(cik))
    accession_path = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{numeric_cik}/{accession_path}/{primary_document}"
