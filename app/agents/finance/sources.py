"""Approved finance source registry for the Phase 6 allowlist."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.agents.finance.contracts import (
    SourceApproval,
    SourceDocument,
    SourceEndpointFailure,
    SourceQuery,
)
from app.connectors.finance_sources.adapters import (
    EXACT_SOURCE_COUNT,
    BulkFileSourceAdapter,
    EndpointSelection,
    EndpointSelector,
    FinanceSourceAdapter,
    JsonHttpSourceAdapter,
    ParsedSourcePayload,
    PayloadParser,
    PublicSourceAdapter,
    RegistrySourceAdapter,
    RequestHeaders,
    RequestParameters,
    RssAtomSourceAdapter,
    SourceGateError,
)
from app.connectors.finance_sources.cache import EndpointStateStore, InMemoryEndpointStateStore
from app.connectors.finance_sources.definitions import (
    ParserKind,
    RawSourcePayload,
    SourceDefinition,
    SourceEndpointDefinition,
    TransportKind,
)
from app.connectors.finance_sources.http import (
    AuthStyle,
    HttpAuthConfig,
    HttpFinanceSourceAdapter,
    HttpRequestConfig,
    breaking_defense_parser,
    federal_register_energy_parser,
    parser_for_source,
)
from app.connectors.finance_sources.providers import (
    EiaMode,
    company_ir_definition,
    defense_gov_definition,
    eia_public_definition,
    issuer_etf_holdings_definition,
    parse_cisa_kev,
    parse_company_ir_feed,
    parse_defense_gov_rss,
    parse_eia_api_response,
    parse_eia_bulk_zip,
    parse_ishares_holdings_csv,
    parse_sec_submissions,
    sec_edgar_definition,
    technology_official_definition,
)
from app.connectors.finance_sources.transport import ConditionalHttpTransport, EndpointRequestAudit
from app.core.config import Settings

FINANCE_SOURCE_ALLOWLIST_VERSION_V1 = "finance-sources-2026.09"
FINANCE_SOURCE_ALLOWLIST_VERSION_V2 = "finance-sources-2026.09-v2"
FINANCE_SOURCE_ALLOWLIST_VERSION = FINANCE_SOURCE_ALLOWLIST_VERSION_V2

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

PUBLIC_FINANCE_SOURCE_IDS: tuple[str, ...] = (
    "defense_gov_rss",
    "breaking_defense_public",
    "eia_public_data",
    "federal_register_energy",
    "sec_edgar",
    "company_ir_registry",
    "issuer_etf_holdings",
    "technology_official_feeds",
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
    request_audit: Callable[[str, EndpointRequestAudit], None] | None = None,
    state_store_factory: Callable[[str], EndpointStateStore] | None = None,
    bulk_artifact: Callable[[str, RawSourcePayload], str] | None = None,
) -> Mapping[str, FinanceSourceAdapter]:
    if settings.finance_source_allowlist_version == FINANCE_SOURCE_ALLOWLIST_VERSION_V2:
        return _build_public_adapter_registry(
            settings,
            approvals,
            client=client,
            request_audit=request_audit,
            state_store_factory=state_store_factory,
            bulk_artifact=bulk_artifact,
        )
    if settings.finance_source_allowlist_version != FINANCE_SOURCE_ALLOWLIST_VERSION_V1:
        raise SourceGateError("unsupported finance source allowlist version")
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


def _build_public_adapter_registry(
    settings: Settings,
    approvals: Sequence[SourceApproval],
    *,
    client: httpx.AsyncClient,
    request_audit: Callable[[str, EndpointRequestAudit], None] | None,
    state_store_factory: Callable[[str], EndpointStateStore] | None,
    bulk_artifact: Callable[[str, RawSourcePayload], str] | None,
) -> Mapping[str, FinanceSourceAdapter]:
    source_map = {source.source_id: source for source in approvals}
    if len(source_map) != EXACT_SOURCE_COUNT or set(source_map) != set(PUBLIC_FINANCE_SOURCE_IDS):
        raise SourceGateError("finance v2 adapter registry requires the public eight source IDs")
    if any(
        approval.allowlist_version != FINANCE_SOURCE_ALLOWLIST_VERSION_V2
        for approval in source_map.values()
    ):
        raise SourceGateError("finance source allowlist version mismatch")

    mode = EiaMode(settings.finance_eia_mode)
    definitions = {
        definition.source_id: definition
        for definition in (
            defense_gov_definition(),
            _breaking_defense_definition(),
            eia_public_definition(mode),
            _federal_register_definition(),
            sec_edgar_definition(),
            company_ir_definition(),
            issuer_etf_holdings_definition(),
            technology_official_definition(),
        )
    }
    adapters: dict[str, FinanceSourceAdapter] = {}
    for source_id in PUBLIC_FINANCE_SOURCE_IDS:
        definition = replace(
            definitions[source_id],
            max_fanout=min(
                definitions[source_id].max_fanout,
                settings.finance_registry_max_fanout,
            ),
        )
        state_store = (
            state_store_factory(source_id)
            if state_store_factory is not None
            else InMemoryEndpointStateStore()
        )
        transport = ConditionalHttpTransport(
            client=client,
            state_store=state_store,
            max_payload_bytes=(
                settings.finance_bulk_max_payload_bytes
                if any(
                    endpoint.transport_kind is TransportKind.BULK_FILE
                    for endpoint in definition.endpoints
                )
                else 2_000_000
            ),
            timeout_seconds=settings.connector_timeout_seconds,
            bulk_artifact_sink=(
                _source_artifact_sink(bulk_artifact, source_id)
                if bulk_artifact is not None
                else None
            ),
        )
        adapter_type: type[PublicSourceAdapter]
        if source_id in {"sec_edgar", "company_ir_registry", "issuer_etf_holdings"}:
            adapter_type = RegistrySourceAdapter
        elif source_id == "defense_gov_rss":
            adapter_type = RssAtomSourceAdapter
        elif source_id == "eia_public_data" and mode is EiaMode.BULK:
            adapter_type = BulkFileSourceAdapter
        else:
            adapter_type = JsonHttpSourceAdapter
        adapters[source_id] = adapter_type(
            source_id=source_id,
            approval=source_map[source_id],
            definition=definition,
            transport=transport,
            parsers=_public_parsers(source_id, definition, source_map[source_id], mode),
            selector=_public_selector(source_id),
            request_parameters=_public_request_parameters(settings, source_id, mode),
            request_headers=_public_request_headers(settings, source_id),
            request_audit_sink=(
                _source_audit_sink(request_audit, source_id)
                if request_audit is not None
                else None
            ),
        )
    return adapters


def _source_audit_sink(
    sink: Callable[[str, EndpointRequestAudit], None], source_id: str
) -> Callable[[EndpointRequestAudit], None]:
    def record(audit: EndpointRequestAudit) -> None:
        sink(source_id, audit)

    return record


def _source_artifact_sink(
    sink: Callable[[str, RawSourcePayload], str], source_id: str
) -> Callable[[RawSourcePayload], str]:
    def persist(payload: RawSourcePayload) -> str:
        return sink(source_id, payload)

    return persist


def _breaking_defense_definition() -> SourceDefinition:
    return SourceDefinition(
        source_id="breaking_defense_public",
        source_version="wp-rest-v2-public",
        allowlist_version=FINANCE_SOURCE_ALLOWLIST_VERSION_V2,
        max_fanout=1,
        endpoints=(
            SourceEndpointDefinition(
                endpoint_id="breaking-defense-wp-json",
                url="https://breakingdefense.com/wp-json/wp/v2/posts",
                allowed_host="breakingdefense.com",
                transport_kind=TransportKind.JSON_HTTP,
                parser_kind=ParserKind.JSON,
                registry_version="wp-rest-v2-public",
                expected_freshness_seconds=600,
                request_ceiling=1,
                license_note="Headline, short excerpt, attribution and canonical link only.",
                excerpt_allowed=True,
                excerpt_max_chars=200,
            ),
        ),
    )


def _federal_register_definition() -> SourceDefinition:
    return SourceDefinition(
        source_id="federal_register_energy",
        source_version="federal-register-api-v1",
        allowlist_version=FINANCE_SOURCE_ALLOWLIST_VERSION_V2,
        max_fanout=1,
        endpoints=(
            SourceEndpointDefinition(
                endpoint_id="federal-register-energy-json",
                url="https://www.federalregister.gov/api/v1/documents.json",
                allowed_host="www.federalregister.gov",
                transport_kind=TransportKind.JSON_HTTP,
                parser_kind=ParserKind.JSON,
                registry_version="federal-register-api-v1",
                expected_freshness_seconds=21_600,
                request_ceiling=1,
                license_note="Official Federal Register public document metadata.",
                excerpt_allowed=True,
                excerpt_max_chars=500,
            ),
        ),
    )


def _public_selector(source_id: str) -> EndpointSelector:
    if source_id not in {"sec_edgar", "company_ir_registry", "issuer_etf_holdings"}:
        def select_all(
            _query: SourceQuery, definition: SourceDefinition
        ) -> EndpointSelection:
            return EndpointSelection(definition.endpoints)

        return select_all

    def select(query: SourceQuery, definition: SourceDefinition) -> EndpointSelection:
        scoped_tickers = (
            query.etf_tickers if source_id == "issuer_etf_holdings" else query.issuer_tickers
        )
        requested = tuple(dict.fromkeys(ticker.upper() for ticker in scoped_tickers))
        endpoints = tuple(
            endpoint
            for endpoint in definition.endpoints
            if set(endpoint.ticker_scope).intersection(requested)
        )
        mapped = {ticker for endpoint in endpoints for ticker in endpoint.ticker_scope}
        diagnostics = tuple(
            SourceEndpointFailure(
                endpoint_id=f"unmapped-{ticker.casefold()}",
                error_code=(
                    "etf_holdings_mapping_missing"
                    if source_id == "issuer_etf_holdings"
                    else "sec_cik_mapping_missing"
                    if source_id == "sec_edgar"
                    else "company_ir_mapping_missing"
                ),
                diagnostic=f"No reviewed {source_id} endpoint is configured for ticker {ticker}",
            )
            for ticker in requested
            if ticker not in mapped
        )
        return EndpointSelection(endpoints=endpoints, diagnostics=diagnostics)

    return select


def _public_parsers(
    source_id: str,
    definition: SourceDefinition,
    approval: SourceApproval,
    mode: EiaMode,
) -> Mapping[str, PayloadParser]:
    parsers: dict[str, PayloadParser] = {}
    for endpoint in definition.endpoints:
        if source_id == "defense_gov_rss":
            def parse_defense(
                payload: RawSourcePayload, query: SourceQuery
            ) -> ParsedSourcePayload:
                return ParsedSourcePayload(
                    documents=parse_defense_gov_rss(
                        payload.body, query, payload.retrieved_at
                    )
                )

            parsers[endpoint.endpoint_id] = parse_defense
        elif source_id == "breaking_defense_public":
            def parse_breaking(
                payload: RawSourcePayload, query: SourceQuery
            ) -> ParsedSourcePayload:
                return ParsedSourcePayload(
                    documents=_require_document_hosts(
                        breaking_defense_parser(
                            _json_payload(payload), query, approval, payload.retrieved_at
                        ),
                        {"breakingdefense.com"},
                    )
                )

            parsers[endpoint.endpoint_id] = parse_breaking
        elif source_id == "eia_public_data":
            if mode is EiaMode.API:
                def parse_eia_api(
                    payload: RawSourcePayload, query: SourceQuery
                ) -> ParsedSourcePayload:
                    return ParsedSourcePayload(
                        documents=parse_eia_api_response(
                            payload.body,
                            query,
                            payload.retrieved_at,
                            source_url=payload.source_url,
                        )
                    )

                parsers[endpoint.endpoint_id] = parse_eia_api
            else:
                def parse_eia_bulk(
                    payload: RawSourcePayload, query: SourceQuery
                ) -> ParsedSourcePayload:
                    return ParsedSourcePayload(
                        documents=parse_eia_bulk_zip(
                            payload.body,
                            query,
                            payload.retrieved_at,
                            source_url=payload.source_url,
                        )
                    )

                parsers[endpoint.endpoint_id] = parse_eia_bulk
        elif source_id == "federal_register_energy":
            def parse_federal(
                payload: RawSourcePayload, query: SourceQuery
            ) -> ParsedSourcePayload:
                return ParsedSourcePayload(
                    documents=_require_document_hosts(
                        federal_register_energy_parser(
                            _json_payload(payload), query, approval, payload.retrieved_at
                        ),
                        {"www.federalregister.gov"},
                    )
                )

            parsers[endpoint.endpoint_id] = parse_federal
        elif source_id == "sec_edgar":
            ticker = endpoint.ticker_scope[0]

            def parse_sec(
                payload: RawSourcePayload,
                query: SourceQuery,
                *,
                scoped_ticker: str = ticker,
            ) -> ParsedSourcePayload:
                return ParsedSourcePayload(
                    documents=parse_sec_submissions(
                        payload.body,
                        query.model_copy(update={"tickers": (scoped_ticker,)}),
                        payload.retrieved_at,
                        ticker=scoped_ticker,
                    )
                )

            parsers[endpoint.endpoint_id] = parse_sec
        elif source_id == "company_ir_registry":
            ticker = endpoint.ticker_scope[0]

            def parse_ir(
                payload: RawSourcePayload,
                query: SourceQuery,
                *,
                scoped_ticker: str = ticker,
            ) -> ParsedSourcePayload:
                return ParsedSourcePayload(
                    documents=parse_company_ir_feed(
                        payload.body,
                        query.model_copy(update={"tickers": (scoped_ticker,)}),
                        payload.retrieved_at,
                        ticker=scoped_ticker,
                    )
                )

            parsers[endpoint.endpoint_id] = parse_ir
        elif source_id == "issuer_etf_holdings":
            ticker = endpoint.ticker_scope[0]

            def parse_etf(
                payload: RawSourcePayload,
                _query: SourceQuery,
                *,
                scoped_ticker: str = ticker,
            ) -> ParsedSourcePayload:
                result = parse_ishares_holdings_csv(
                    payload.body,
                    etf_symbol=scoped_ticker,
                    retrieved_at=payload.retrieved_at,
                )
                return ParsedSourcePayload(
                    etf_exposures=result.exposures,
                    stale=result.stale,
                )

            parsers[endpoint.endpoint_id] = parse_etf
        elif source_id == "technology_official_feeds":
            def parse_technology(
                payload: RawSourcePayload, query: SourceQuery
            ) -> ParsedSourcePayload:
                return ParsedSourcePayload(
                    documents=parse_cisa_kev(
                        payload.body, query, payload.retrieved_at
                    )
                )

            parsers[endpoint.endpoint_id] = parse_technology
    return parsers


def _public_request_parameters(
    settings: Settings, source_id: str, mode: EiaMode
) -> RequestParameters:
    def parameters(_endpoint: SourceEndpointDefinition, query: SourceQuery) -> Mapping[str, str]:
        if source_id == "breaking_defense_public":
            values = {
                "per_page": "100",
                "after": query.window_start.isoformat(),
                "before": query.window_end.isoformat(),
            }
            if terms := (*query.tickers, *query.themes):
                values["search"] = " ".join(terms)
            return values
        if source_id == "federal_register_energy":
            return {
                "conditions[agencies][]": "energy-department",
                "conditions[publication_date][gte]": query.window_start.date().isoformat(),
                "conditions[publication_date][lte]": query.window_end.date().isoformat(),
                "order": "newest",
                "per_page": "100",
            }
        if source_id == "eia_public_data" and mode is EiaMode.API:
            api_key = _secret(settings, "eia_api_key")
            if api_key is None:
                raise SourceGateError("FINANCE_EIA_MODE=api requires EIA_API_KEY")
            return {
                "api_key": api_key,
                "frequency": "weekly",
                "data[0]": "value",
                "facets[product][]": "EPCWTI",
                "start": query.window_start.date().isoformat(),
                "end": query.window_end.date().isoformat(),
                "length": "100",
            }
        return {}

    return parameters


def _public_request_headers(settings: Settings, source_id: str) -> RequestHeaders:
    def headers(_endpoint: SourceEndpointDefinition) -> Mapping[str, str]:
        if source_id == "sec_edgar":
            return {
                "User-Agent": settings.sec_user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        return {}

    return headers


def _json_payload(
    payload: RawSourcePayload,
) -> Mapping[str, object] | Sequence[object]:
    value = json.loads(payload.body)
    if not isinstance(value, Mapping | Sequence) or isinstance(value, str | bytes | bytearray):
        raise ValueError("finance JSON payload has an unsupported root")
    return cast(Mapping[str, object] | Sequence[object], value)


def _require_document_hosts(
    documents: Sequence[SourceDocument], allowed_hosts: set[str]
) -> tuple[SourceDocument, ...]:
    for document in documents:
        parsed = urlsplit(str(document.url))
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("finance source returned a canonical URL outside its reviewed hosts")
    return tuple(documents)


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
    "FINANCE_SOURCE_ALLOWLIST_VERSION_V1",
    "FINANCE_SOURCE_ALLOWLIST_VERSION_V2",
    "PUBLIC_FINANCE_SOURCE_IDS",
    "SourceCredentialConfig",
    "build_finance_adapter_registry",
    "source_auth_style",
]
