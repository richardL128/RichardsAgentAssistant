"""Official issuer investor-relations feed registry and parsers."""

from __future__ import annotations

from datetime import datetime

from app.agents.finance.contracts import SourceClassification, SourceDocument, SourceQuery

from .common import ProviderDiagnostic
from .feeds import FeedParseOptions, parse_feed_documents
from .registries import COMPANY_IR_REGISTRY, COMPANY_IR_REGISTRY_FEEDS, COMPANY_IR_SOURCE_VERSION


def company_ir_missing_mapping_diagnostics(
    tickers: tuple[str, ...],
) -> tuple[ProviderDiagnostic, ...]:
    return tuple(
        ProviderDiagnostic(
            code="company_ir_mapping_missing",
            diagnostic=f"No reviewed official IR feed is configured for ticker {ticker.upper()}",
            ticker=ticker.upper(),
        )
        for ticker in tickers
        if ticker.upper() not in COMPANY_IR_REGISTRY_FEEDS
    )


def parse_company_ir_feed(
    payload: bytes | str,
    query: SourceQuery,
    retrieved_at: datetime,
    *,
    ticker: str,
) -> tuple[SourceDocument, ...]:
    mapping = COMPANY_IR_REGISTRY_FEEDS.get(ticker.upper())
    if mapping is None:
        raise ValueError(f"unsupported company IR ticker: {ticker.upper()}")
    return parse_feed_documents(
        payload,
        query,
        retrieved_at,
        FeedParseOptions(
            source_id=COMPANY_IR_REGISTRY,
            source_version=COMPANY_IR_SOURCE_VERSION,
            classification=SourceClassification.PRIMARY,
            endpoint_url=mapping.feed_url,
            endpoint_id=mapping.endpoint_id,
            tickers=(mapping.ticker,),
            themes=("issuer-ir",),
            issuer=mapping.issuer,
            excerpt_allowed=False,
            require_canonical_host=mapping.allowed_host,
        ),
    )
