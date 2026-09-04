"""HTTP adapters for the approved Phase 6 finance source allowlist."""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import cast
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.agents.finance.contracts import (
    QuantValue,
    SourceApproval,
    SourceDocument,
    SourceFailure,
    SourceFetchResult,
    SourceQuery,
)

JsonValue = Mapping[str, object] | Sequence[object]
MAX_CONTRACT_EXCERPT_CHARS = 500
SourceParser = Callable[
    [JsonValue, SourceQuery, SourceApproval, datetime],
    tuple[SourceDocument, ...],
]


class AuthStyle(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    OAUTH_BEARER = "oauth_bearer"
    QUERY = "query"
    QUERY_PARAM = "query_param"
    HEADER = "header"


@dataclass(frozen=True, slots=True)
class HttpAuthConfig:
    style: AuthStyle
    secret: str | None = field(default=None, repr=False)
    parameter: str | None = None
    header: str | None = None
    header_prefix: str = ""


@dataclass(frozen=True, slots=True)
class HttpRequestConfig:
    method: str = "GET"
    static_params: Mapping[str, str] | None = None
    window_start_param: str | None = "window_start"
    window_end_param: str | None = "window_end"
    tickers_param: str | None = "tickers"
    themes_param: str | None = "themes"
    source_version_param: str | None = None
    search_param: str | None = None
    window_format: str | None = None
    tickers_limit: int | None = None
    ticker_in_path: bool = False
    sequence_separator: str = ","


@dataclass(slots=True)
class HttpFinanceSourceAdapter:
    source_id: str
    approval: SourceApproval
    client: httpx.AsyncClient
    parser: SourceParser
    auth: HttpAuthConfig = HttpAuthConfig(style=AuthStyle.NONE)
    request: HttpRequestConfig = HttpRequestConfig()
    timeout_seconds: float = 10.0
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    async def fetch(self, query: SourceQuery) -> SourceFetchResult:
        if (
            query.source_id != self.source_id
            or query.source_version != self.approval.source_version
        ):
            return _failure(
                self.source_id,
                "input_invalid",
                "finance source query does not match the adapter approval record",
            )
        auth_failure = _auth_failure(self.source_id, self.auth)
        if auth_failure is not None:
            return auth_failure
        try:
            response = await self.client.request(
                self.request.method,
                _request_url(self.approval, query, self.request),
                params=_request_params(query, self.approval, self.auth, self.request),
                headers=_request_headers(self.auth),
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            return _failure(self.source_id, "connector_timeout", "finance source request timed out")
        except httpx.HTTPStatusError as exc:
            return _failure(
                self.source_id,
                "connector_http_status",
                f"finance source returned HTTP {exc.response.status_code}",
            )
        except httpx.HTTPError:
            return _failure(self.source_id, "connector_transient", "finance source request failed")
        except (TypeError, ValueError):
            return _failure(
                self.source_id,
                "request_invalid",
                "finance source request configuration is invalid",
            )

        try:
            payload = cast(JsonValue, response.json())
        except ValueError:
            return _failure(
                self.source_id,
                "malformed_json",
                "finance source returned invalid JSON",
            )

        try:
            documents = self.parser(
                payload,
                query,
                self.approval,
                _aware(self.clock()),
            )
        except (TypeError, ValueError, ValidationError):
            return _failure(
                self.source_id, "parser_failed", "finance source response could not be parsed"
            )
        return SourceFetchResult(source_id=self.source_id, documents=documents)


def parser_for_source(source_id: str) -> SourceParser:
    try:
        return _PARSERS[source_id]
    except KeyError as exc:
        raise ValueError(f"unsupported finance source parser: {source_id}") from exc


def dvids_parser(
    payload: JsonValue,
    query: SourceQuery,
    approval: SourceApproval,
    retrieved_at: datetime,
) -> tuple[SourceDocument, ...]:
    return _parse_documents(
        payload,
        query,
        approval,
        retrieved_at,
        excerpt_fields=("short_description", "description"),
    )


def breaking_defense_parser(
    payload: JsonValue,
    query: SourceQuery,
    approval: SourceApproval,
    retrieved_at: datetime,
) -> tuple[SourceDocument, ...]:
    return _parse_documents(
        payload,
        query,
        approval,
        retrieved_at,
        excerpt_fields=("excerpt", "summary"),
    )


def eia_open_data_parser(
    payload: JsonValue,
    query: SourceQuery,
    approval: SourceApproval,
    retrieved_at: datetime,
) -> tuple[SourceDocument, ...]:
    return _parse_documents(
        payload,
        query,
        approval,
        retrieved_at,
        excerpt_fields=("description", "summary"),
        number_fields=("value",),
    )


def federal_register_energy_parser(
    payload: JsonValue,
    query: SourceQuery,
    approval: SourceApproval,
    retrieved_at: datetime,
) -> tuple[SourceDocument, ...]:
    return _parse_documents(
        payload,
        query,
        approval,
        retrieved_at,
        excerpt_fields=("abstract",),
    )


def alpha_vantage_news_parser(
    payload: JsonValue,
    query: SourceQuery,
    approval: SourceApproval,
    retrieved_at: datetime,
) -> tuple[SourceDocument, ...]:
    return _parse_documents(payload, query, approval, retrieved_at, excerpt_fields=("summary",))


def benzinga_news_parser(
    payload: JsonValue,
    query: SourceQuery,
    approval: SourceApproval,
    retrieved_at: datetime,
) -> tuple[SourceDocument, ...]:
    return _parse_documents(
        payload,
        query,
        approval,
        retrieved_at,
        excerpt_fields=("teaser", "summary", "body"),
    )


def fmp_etf_parser(
    payload: JsonValue,
    query: SourceQuery,
    approval: SourceApproval,
    retrieved_at: datetime,
) -> tuple[SourceDocument, ...]:
    return _parse_documents(
        payload,
        query,
        approval,
        retrieved_at,
        excerpt_fields=(),
        number_fields=("weightPercentage", "weight_percent", "marketValue"),
    )


def alpha_vantage_etf_parser(
    payload: JsonValue,
    query: SourceQuery,
    approval: SourceApproval,
    retrieved_at: datetime,
) -> tuple[SourceDocument, ...]:
    return _parse_documents(
        payload,
        query,
        approval,
        retrieved_at,
        excerpt_fields=(),
        number_fields=(
            "weight",
            "weightPercentage",
            "net_assets",
            "netAssets",
            "expense_ratio",
            "expenseRatio",
        ),
    )


def _auth_failure(source_id: str, auth: HttpAuthConfig) -> SourceFetchResult | None:
    if auth.style == AuthStyle.NONE:
        return None
    if not auth.secret:
        return _failure(source_id, "connector_auth", "finance source credential is not configured")
    if auth.style in {AuthStyle.QUERY, AuthStyle.QUERY_PARAM} and not auth.parameter:
        return _failure(
            source_id,
            "connector_auth",
            "finance source query credential parameter is not configured",
        )
    if auth.style == AuthStyle.HEADER and not auth.header:
        return _failure(
            source_id,
            "connector_auth",
            "finance source credential header is not configured",
        )
    return None


def _request_params(
    query: SourceQuery,
    approval: SourceApproval,
    auth: HttpAuthConfig,
    request: HttpRequestConfig,
) -> dict[str, str]:
    params = dict(httpx.URL(str(approval.base_url)).params.multi_items())
    params.update(request.static_params or {})
    if request.window_start_param is not None:
        params[request.window_start_param] = _format_window(
            query.window_start,
            request.window_format,
        )
    if request.window_end_param is not None:
        params[request.window_end_param] = _format_window(
            query.window_end,
            request.window_format,
        )
    tickers = query.tickers[: request.tickers_limit] if request.tickers_limit else query.tickers
    if request.tickers_param is not None and query.tickers:
        params[request.tickers_param] = request.sequence_separator.join(tickers)
    if request.themes_param is not None and query.themes:
        params[request.themes_param] = request.sequence_separator.join(query.themes)
    if request.search_param is not None:
        search_terms = (*tickers, *query.themes)
        if search_terms:
            params[request.search_param] = " ".join(search_terms)
    if request.source_version_param is not None:
        params[request.source_version_param] = query.source_version
    if auth.style in {AuthStyle.QUERY, AuthStyle.QUERY_PARAM} and auth.secret and auth.parameter:
        params[auth.parameter] = auth.secret
    return params


def _request_url(
    approval: SourceApproval,
    query: SourceQuery,
    request: HttpRequestConfig,
) -> str:
    base_url = str(approval.base_url)
    if not request.ticker_in_path:
        return base_url
    if not query.tickers:
        raise ValueError("finance source request requires an ETF ticker")
    return f"{base_url.rstrip('/')}/{quote(query.tickers[0], safe='')}"


def _format_window(value: datetime, window_format: str | None) -> str:
    return value.strftime(window_format) if window_format else value.isoformat()


def _request_headers(auth: HttpAuthConfig) -> dict[str, str]:
    if auth.style == AuthStyle.NONE:
        return {}
    if not auth.secret:
        return {}
    if auth.style in {AuthStyle.BEARER, AuthStyle.OAUTH_BEARER}:
        return {"Authorization": f"Bearer {auth.secret}"}
    if auth.style == AuthStyle.HEADER and auth.header:
        return {auth.header: f"{auth.header_prefix}{auth.secret}"}
    return {}


def _failure(source_id: str, error_code: str, diagnostic: str) -> SourceFetchResult:
    return SourceFetchResult(
        source_id=source_id,
        failure=SourceFailure(
            source_id=source_id,
            error_code=error_code,
            diagnostic=diagnostic,
        ),
    )


def _parse_documents(
    payload: JsonValue,
    query: SourceQuery,
    approval: SourceApproval,
    retrieved_at: datetime,
    *,
    excerpt_fields: Sequence[str],
    number_fields: Sequence[str] = (),
) -> tuple[SourceDocument, ...]:
    documents: list[SourceDocument] = []
    for item in _items(payload):
        if _item_version(item) not in {None, query.source_version}:
            continue
        published_at = _published_at(item)
        effective_time = published_at or retrieved_at
        if not _inside_window(effective_time, query):
            continue
        try:
            document = SourceDocument.model_validate(
                {
                    "source_id": query.source_id,
                    "source_version": query.source_version,
                    "external_id": _external_id(item),
                    "title": _title(item, approval),
                    "url": _url(item, approval),
                    "published_at": published_at,
                    "retrieved_at": retrieved_at,
                    "language": _language(item),
                    "classification": _approval_classification(approval),
                    "license_allows_excerpt": _license_allows_excerpt(approval),
                    "excerpt": _licensed_excerpt(item, approval, excerpt_fields),
                    "tickers": _tickers(item, query),
                    "themes": _themes(item, query),
                    "numbers": _numbers(item, query, effective_time.date(), number_fields),
                }
            )
        except (TypeError, ValueError, ValidationError):
            continue
        documents.append(document)
    return tuple(documents)


def _items(payload: JsonValue) -> tuple[Mapping[str, object], ...]:
    if isinstance(payload, Sequence) and not isinstance(payload, str | bytes | bytearray):
        return _mapping_items(payload)
    if not isinstance(payload, Mapping):
        return ()
    for key in ("feed", "items", "articles", "results", "data", "posts", "news", "holdings"):
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return _mapping_items(cast(Sequence[object], value))
    response = payload.get("response")
    if isinstance(response, Mapping):
        return _items(cast(Mapping[str, object], response))
    return (payload,)


def _mapping_items(values: Sequence[object]) -> tuple[Mapping[str, object], ...]:
    return tuple(cast(Mapping[str, object], item) for item in values if isinstance(item, Mapping))


def _item_version(item: Mapping[str, object]) -> str | None:
    value = item.get("source_version") or item.get("version")
    return value if isinstance(value, str) and value else None


def _external_id(item: Mapping[str, object]) -> str:
    for key in ("external_id", "id", "uuid", "slug", "document_number", "symbol"):
        value = item.get(key)
        if isinstance(value, str | int) and str(value):
            return str(value)[:255]
    title = _raw_text(item.get("title") or item.get("headline"))
    url = _raw_text(item.get("url") or item.get("link"))
    key = f"{title}:{url}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _title(item: Mapping[str, object], approval: SourceApproval) -> str:
    for key in (
        "title",
        "headline",
        "name",
        "description",
        "seriesDescription",
        "series_name",
        "symbol",
    ):
        value = _raw_text(item.get(key))
        if value:
            return value[:500]
    return f"{approval.name} update"


def _url(item: Mapping[str, object], approval: SourceApproval) -> str:
    for key in (
        "url",
        "link",
        "canonical_url",
        "article_url",
        "html_url",
        "pdf_url",
    ):
        value = _raw_text(item.get(key))
        if value:
            return value
    return str(approval.base_url)


def _published_at(item: Mapping[str, object]) -> datetime | None:
    for key in (
        "published_at",
        "published",
        "date",
        "date_gmt",
        "created",
        "created_at",
        "time_published",
        "date_published",
        "period",
    ):
        value = item.get(key)
        parsed = _parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip()
    for fmt in ("%Y%m%dT%H%M%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _inside_window(value: datetime, query: SourceQuery) -> bool:
    timestamp = value.astimezone(UTC)
    return query.window_start <= timestamp <= query.window_end


def _language(item: Mapping[str, object]) -> str:
    value = item.get("language") or item.get("lang")
    return value if isinstance(value, str) and value else "en"


def _licensed_excerpt(
    item: Mapping[str, object],
    approval: SourceApproval,
    excerpt_fields: Sequence[str],
) -> str | None:
    if not _license_allows_excerpt(approval):
        return None
    for key in excerpt_fields:
        value = _raw_text(item.get(key))
        if value:
            return _clamp_excerpt(value, approval)
    return None


def _clamp_excerpt(value: str, approval: SourceApproval) -> str:
    excerpt = value
    max_words = _positive_int_attr(approval, "excerpt_max_words")
    if max_words is not None:
        words = excerpt.split()
        if len(words) > max_words:
            excerpt = " ".join(words[:max_words])
    max_chars = min(_positive_int_attr(approval, "excerpt_max_chars") or 500, 500)
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rstrip()
    if len(excerpt) > MAX_CONTRACT_EXCERPT_CHARS:
        excerpt = excerpt[:MAX_CONTRACT_EXCERPT_CHARS].rstrip()
    return excerpt


def _tickers(item: Mapping[str, object], query: SourceQuery) -> tuple[str, ...]:
    values = _string_tuple(item.get("tickers") or item.get("symbols") or item.get("ticker"))
    stocks = item.get("stocks")
    if isinstance(stocks, Sequence) and not isinstance(stocks, str | bytes | bytearray):
        values = (*values, *(_stock_symbol(stock) for stock in cast(Sequence[object], stocks)))
    return _unique(value.upper() for value in (*values, *query.tickers))


def _themes(item: Mapping[str, object], query: SourceQuery) -> tuple[str, ...]:
    values = _string_tuple(item.get("themes") or item.get("categories") or item.get("sector"))
    return _unique(value.casefold() for value in (*values, *query.themes))


def _numbers(
    item: Mapping[str, object],
    query: SourceQuery,
    as_of: date,
    number_fields: Sequence[str],
) -> tuple[QuantValue, ...]:
    numbers: list[QuantValue] = []
    for key in number_fields:
        value = item.get(key)
        if isinstance(value, str):
            try:
                value = float(value.rstrip("%"))
            except ValueError:
                continue
        if not isinstance(value, int | float):
            continue
        unit = "percent" if "weight" in key.casefold() else _raw_text(item.get("unit")) or "value"
        numbers.append(
            QuantValue(
                label=key.replace("_", " ").replace("Percentage", " percentage"),
                value=float(value),
                unit=unit,
                as_of=as_of,
                source_ids=(query.source_id,),
            )
        )
    return tuple(numbers)


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part.strip() for part in re.split(r"[,;]", value) if part.strip())
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        items = cast(Sequence[object], value)
        return tuple(_raw_text(item) for item in items if _raw_text(item))
    return ()


def _stock_symbol(value: object) -> str:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, object], value)
        for key in ("symbol", "ticker", "name"):
            text = _raw_text(mapping.get(key))
            if text:
                return text
        return ""
    return _raw_text(value)


def _raw_text(value: object) -> str:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[str, object], value)
        for key in ("rendered", "text", "raw"):
            rendered = _raw_text(mapping.get(key))
            if rendered:
                return rendered
        return ""
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _license_allows_excerpt(approval: SourceApproval) -> bool:
    raw = getattr(approval, "license_allows_excerpt", False)
    return raw if isinstance(raw, bool) else False


def _approval_classification(approval: SourceApproval) -> str:
    return approval.classification.value


def _positive_int_attr(approval: SourceApproval, attr: str) -> int | None:
    raw = getattr(approval, attr, None)
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


_PARSERS: Mapping[str, SourceParser] = {
    "dvids": dvids_parser,
    "breaking_defense": breaking_defense_parser,
    "eia_open_data": eia_open_data_parser,
    "federal_register_energy": federal_register_energy_parser,
    "alpha_vantage_news": alpha_vantage_news_parser,
    "benzinga_news": benzinga_news_parser,
    "fmp_etf": fmp_etf_parser,
    "alpha_vantage_etf": alpha_vantage_etf_parser,
}


__all__ = [
    "AuthStyle",
    "HttpAuthConfig",
    "HttpFinanceSourceAdapter",
    "HttpRequestConfig",
    "SourceParser",
    "alpha_vantage_etf_parser",
    "alpha_vantage_news_parser",
    "benzinga_news_parser",
    "breaking_defense_parser",
    "dvids_parser",
    "eia_open_data_parser",
    "federal_register_energy_parser",
    "fmp_etf_parser",
    "parser_for_source",
]
