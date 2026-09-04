"""Approved finance source registry for the Phase 6 allowlist."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import httpx
from pydantic import SecretStr

from app.agents.finance.contracts import SourceApproval
from app.connectors.finance_sources.adapters import (
    EXACT_SOURCE_COUNT,
    FinanceSourceAdapter,
    SourceGateError,
)
from app.connectors.finance_sources.http import (
    AuthStyle,
    HttpAuthConfig,
    HttpFinanceSourceAdapter,
    HttpRequestConfig,
    parser_for_source,
)
from app.core.config import Settings

FINANCE_SOURCE_ALLOWLIST_VERSION = "finance-sources-2026.09"

APPROVED_FINANCE_SOURCE_IDS: tuple[str, ...] = (
    "dvids",
    "breaking_defense",
    "eia_open_data",
    "federal_register_energy",
    "alpha_vantage_news",
    "benzinga_news",
    "fmp_etf",
    "alpha_vantage_etf",
)


@dataclass(frozen=True, slots=True)
class SourceCredentialConfig:
    field_name: str | None
    auth_style: AuthStyle
    parameter: str | None = None
    header: str | None = None
    header_prefix: str = ""
    request: HttpRequestConfig = field(default_factory=HttpRequestConfig)


_CREDENTIALS: Mapping[str, SourceCredentialConfig] = {
    "dvids": SourceCredentialConfig(
        "dvids_api_key",
        AuthStyle.QUERY,
        parameter="api_key",
        request=HttpRequestConfig(
            static_params={
                "type": "news",
                "max_results": "50",
                "short_description_length": "300",
            },
            window_start_param="from_publishdate",
            window_end_param="to_publishdate",
            tickers_param=None,
            themes_param=None,
            search_param="q",
        ),
    ),
    "breaking_defense": SourceCredentialConfig(
        None,
        AuthStyle.NONE,
        request=HttpRequestConfig(
            static_params={"per_page": "100"},
            window_start_param="after",
            window_end_param="before",
            tickers_param=None,
            themes_param=None,
            search_param="search",
        ),
    ),
    "eia_open_data": SourceCredentialConfig(
        "eia_api_key",
        AuthStyle.QUERY,
        parameter="api_key",
        request=HttpRequestConfig(
            window_start_param="start",
            window_end_param="end",
            window_format="%Y-%m-%d",
        ),
    ),
    "federal_register_energy": SourceCredentialConfig(
        None,
        AuthStyle.NONE,
        request=HttpRequestConfig(
            static_params={
                "conditions[agencies][]": "energy-department",
                "order": "newest",
                "per_page": "100",
            },
            window_start_param="conditions[publication_date][gte]",
            window_end_param="conditions[publication_date][lte]",
            tickers_param=None,
            themes_param=None,
            search_param="conditions[term]",
            window_format="%Y-%m-%d",
        ),
    ),
    "alpha_vantage_news": SourceCredentialConfig(
        "alpha_vantage_api_key",
        AuthStyle.QUERY,
        parameter="apikey",
        request=HttpRequestConfig(
            static_params={"sort": "LATEST", "limit": "200"},
            window_start_param="time_from",
            window_end_param="time_to",
            tickers_param="tickers",
            themes_param="topics",
            window_format="%Y%m%dT%H%M",
        ),
    ),
    "benzinga_news": SourceCredentialConfig(
        "benzinga_api_token",
        AuthStyle.HEADER,
        header="token",
        request=HttpRequestConfig(
            window_start_param="dateFrom",
            window_end_param="dateTo",
            tickers_param="tickers",
            themes_param="channels",
            window_format="%Y-%m-%d",
        ),
    ),
    "fmp_etf": SourceCredentialConfig(
        "fmp_api_key",
        AuthStyle.QUERY,
        parameter="apikey",
        request=HttpRequestConfig(
            window_start_param=None,
            window_end_param=None,
            tickers_param=None,
            themes_param=None,
            tickers_limit=1,
            ticker_in_path=True,
        ),
    ),
    "alpha_vantage_etf": SourceCredentialConfig(
        "alpha_vantage_api_key",
        AuthStyle.QUERY,
        parameter="apikey",
        request=HttpRequestConfig(
            window_start_param=None,
            window_end_param=None,
            tickers_param="symbol",
            themes_param=None,
            tickers_limit=1,
        ),
    ),
}


def build_finance_adapter_registry(
    settings: Settings,
    approvals: Sequence[SourceApproval],
    *,
    client: httpx.AsyncClient,
) -> Mapping[str, FinanceSourceAdapter]:
    source_map = {source.source_id: source for source in approvals}
    expected_ids = set(APPROVED_FINANCE_SOURCE_IDS)
    if len(source_map) != EXACT_SOURCE_COUNT or set(source_map) != expected_ids:
        raise SourceGateError("finance adapter registry requires the approved eight source IDs")

    adapters: dict[str, FinanceSourceAdapter] = {}
    for source_id in APPROVED_FINANCE_SOURCE_IDS:
        approval = source_map[source_id]
        if approval.allowlist_version != settings.finance_source_allowlist_version:
            raise SourceGateError("finance source allowlist version mismatch")
        credential = _CREDENTIALS[source_id]
        secret = _secret(settings, credential.field_name)
        if approval.enabled and credential.field_name is not None and secret is None:
            raise SourceGateError(f"finance source credential missing for {source_id}")
        try:
            parser = parser_for_source(source_id)
        except ValueError as exc:
            raise SourceGateError(f"finance source parser missing for {source_id}") from exc
        adapters[source_id] = HttpFinanceSourceAdapter(
            source_id=source_id,
            approval=approval,
            client=client,
            parser=parser,
            auth=HttpAuthConfig(
                style=credential.auth_style,
                secret=secret,
                parameter=credential.parameter,
                header=credential.header,
                header_prefix=credential.header_prefix,
            ),
            request=credential.request,
            timeout_seconds=settings.connector_timeout_seconds,
        )
    return adapters


def _secret(settings: Settings, field_name: str | None) -> str | None:
    if field_name is None:
        return None
    value = getattr(settings, field_name)
    if isinstance(value, SecretStr):
        secret = value.get_secret_value()
        return secret or None
    if isinstance(value, str):
        return value or None
    return None


def source_auth_style(source_id: str) -> str:
    return _CREDENTIALS[source_id].auth_style.value


__all__ = [
    "APPROVED_FINANCE_SOURCE_IDS",
    "FINANCE_SOURCE_ALLOWLIST_VERSION",
    "SourceCredentialConfig",
    "build_finance_adapter_registry",
    "source_auth_style",
]
