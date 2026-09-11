"""Constrained public-web research primitives for interview preparation."""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import re
import socket
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import ClassVar, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
}
_TEXTUAL_CONTENT_TYPES = {
    "application/xhtml+xml",
    "text/html",
    "text/plain",
}
_BLOCKED_HOSTS = {
    "metadata",
    "metadata.google.internal",
}


class CompanyResearchIntent(StrEnum):
    COMPANY_OVERVIEW = "company_overview"
    PRODUCTS = "products"
    ROLE_REQUIREMENTS = "role_requirements"
    RECENT_OFFICIAL_ANNOUNCEMENTS = "recent_official_announcements"
    ENGINEERING_PRACTICES = "engineering_practices"
    INTERVIEW_CONTEXT = "interview_context"


_QUERY_PHRASES: Mapping[CompanyResearchIntent, str] = {
    CompanyResearchIntent.COMPANY_OVERVIEW: "company overview",
    CompanyResearchIntent.PRODUCTS: "products",
    CompanyResearchIntent.ROLE_REQUIREMENTS: "role requirements",
    CompanyResearchIntent.RECENT_OFFICIAL_ANNOUNCEMENTS: "recent official announcements",
    CompanyResearchIntent.ENGINEERING_PRACTICES: "engineering practices",
    CompanyResearchIntent.INTERVIEW_CONTEXT: "interview relevant context",
}


class CompanyResearchSourceClass(StrEnum):
    POSTING = "posting"
    OFFICIAL = "official"
    THIRD_PARTY_PUBLIC = "third_party_public"


class CompanyResearchFailureCode(StrEnum):
    CONNECTOR_TIMEOUT = "connector_timeout"
    CONNECTOR_TRANSIENT = "connector_transient"
    CONNECTOR_HTTP_STATUS = "connector_http_status"
    CONTENT_TYPE_REJECTED = "content_type_rejected"
    DNS_RESOLUTION_FAILED = "dns_resolution_failed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    PROVIDER_UNCONFIGURED = "provider_unconfigured"
    REDIRECT_DISALLOWED = "redirect_disallowed"
    REQUEST_BUDGET_EXCEEDED = "request_budget_exceeded"
    REQUEST_INVALID = "request_invalid"
    URL_UNSAFE = "url_unsafe"


@dataclass(frozen=True, slots=True)
class CompanyResearchFailure:
    code: CompanyResearchFailureCode
    diagnostic: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class CompanyResearchSearchQuery:
    query: str
    company: str
    role: str | None
    intent: CompanyResearchIntent
    max_results: int


@dataclass(frozen=True, slots=True)
class CompanyResearchSearchResultItem:
    title: str
    url: str
    snippet: str = ""
    source_class: CompanyResearchSourceClass = CompanyResearchSourceClass.THIRD_PARTY_PUBLIC


@dataclass(frozen=True, slots=True)
class CompanyResearchSearchResult:
    items: tuple[CompanyResearchSearchResultItem, ...]
    failure: CompanyResearchFailure | None = None


class CompanyResearchSearchProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    async def search(self, query: CompanyResearchSearchQuery) -> CompanyResearchSearchResult: ...


@dataclass(frozen=True, slots=True)
class UnconfiguredCompanyResearchSearchProvider:
    @property
    def configured(self) -> bool:
        return False

    async def search(self, query: CompanyResearchSearchQuery) -> CompanyResearchSearchResult:
        return CompanyResearchSearchResult(
            items=(),
            failure=CompanyResearchFailure(
                code=CompanyResearchFailureCode.PROVIDER_UNCONFIGURED,
                diagnostic="company research search provider is not configured",
            ),
        )


@dataclass(frozen=True, slots=True)
class CompanyResearchRequest:
    posting_url: str
    company: str
    role: str | None = None
    intents: tuple[CompanyResearchIntent, ...] = (CompanyResearchIntent.COMPANY_OVERVIEW,)
    interview_source_id: str | None = None
    application_source_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompanyResearchSourceSnapshot:
    source_id: str
    source_url: str
    canonical_url: str
    title: str | None
    source_class: CompanyResearchSourceClass
    retrieved_at: datetime
    content_fingerprint: str
    excerpt: str
    excerpt_truncated: bool
    content_type: str | None
    status_code: int
    bytes_read: int


@dataclass(frozen=True, slots=True)
class CompanyResearchResult:
    posting_url: str
    canonical_posting_url: str | None
    company: str
    role: str | None
    sources: tuple[CompanyResearchSourceSnapshot, ...]
    failures: tuple[CompanyResearchFailure, ...]
    search_queries: tuple[CompanyResearchSearchQuery, ...]
    retrieved_at: datetime
    research_fingerprint: str

    @property
    def ok(self) -> bool:
        return bool(self.sources)


class CompanyResearchDnsResolver(Protocol):
    async def resolve(self, hostname: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class SocketCompanyResearchDnsResolver:
    async def resolve(self, hostname: str) -> tuple[str, ...]:
        return await asyncio.to_thread(_resolve_hostname, hostname)


@dataclass(slots=True)
class CompanyResearchClient:
    client: httpx.AsyncClient
    search_provider: CompanyResearchSearchProvider = field(
        default_factory=UnconfiguredCompanyResearchSearchProvider
    )
    resolver: CompanyResearchDnsResolver = field(default_factory=SocketCompanyResearchDnsResolver)
    max_pages: int = 4
    max_search_results: int = 3
    max_response_bytes: int = 250_000
    max_excerpt_chars: int = 4_000
    timeout_seconds: float = 8.0
    max_redirects: int = 3
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)

    async def research(self, request: CompanyResearchRequest) -> CompanyResearchResult:
        retrieved_at = _aware(self.clock())
        failures: list[CompanyResearchFailure] = []
        sources: list[CompanyResearchSourceSnapshot] = []
        search_queries: list[CompanyResearchSearchQuery] = []
        requests_remaining = self.max_pages

        posting = await self._fetch_source(
            request.posting_url,
            source_class=CompanyResearchSourceClass.POSTING,
            requested_at=retrieved_at,
        )
        if isinstance(posting, CompanyResearchFailure):
            failures.append(posting)
            canonical_posting_url = None
        else:
            sources.append(posting)
            canonical_posting_url = posting.canonical_url
            requests_remaining -= 1

        if request.intents:
            if not self.search_provider.configured:
                failures.append(
                    CompanyResearchFailure(
                        code=CompanyResearchFailureCode.PROVIDER_UNCONFIGURED,
                        diagnostic="company research search provider is not configured",
                    )
                )
            else:
                for intent in request.intents:
                    if requests_remaining <= 0:
                        failures.append(
                            CompanyResearchFailure(
                                code=CompanyResearchFailureCode.REQUEST_BUDGET_EXCEEDED,
                                diagnostic="company research request budget was exhausted",
                            )
                        )
                        break
                    query = build_company_research_query(
                        company=request.company,
                        role=request.role,
                        intent=intent,
                        max_results=min(self.max_search_results, requests_remaining),
                    )
                    search_queries.append(query)
                    search_result = await self.search_provider.search(query)
                    if search_result.failure is not None:
                        failures.append(search_result.failure)
                        continue
                    for item in search_result.items[: query.max_results]:
                        if requests_remaining <= 0:
                            break
                        fetched = await self._fetch_source(
                            item.url,
                            source_class=item.source_class,
                            requested_at=retrieved_at,
                        )
                        if isinstance(fetched, CompanyResearchFailure):
                            failures.append(fetched)
                            continue
                        sources.append(fetched)
                        requests_remaining -= 1

        return CompanyResearchResult(
            posting_url=request.posting_url,
            canonical_posting_url=canonical_posting_url,
            company=request.company,
            role=request.role,
            sources=tuple(sources),
            failures=tuple(failures),
            search_queries=tuple(search_queries),
            retrieved_at=retrieved_at,
            research_fingerprint=_research_fingerprint(sources),
        )

    async def _fetch_source(
        self,
        url: str,
        *,
        source_class: CompanyResearchSourceClass,
        requested_at: datetime,
    ) -> CompanyResearchSourceSnapshot | CompanyResearchFailure:
        current_url = url
        redirects_seen = 0
        while True:
            canonical = await self._validate_public_https_url(current_url)
            if isinstance(canonical, CompanyResearchFailure):
                return canonical
            try:
                async with self.client.stream(
                    "GET",
                    canonical,
                    headers={
                        "Accept": "text/html,text/plain,application/xhtml+xml",
                        "User-Agent": "LifeAgent job-interview-research",
                    },
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("location")
                        if not location or redirects_seen >= self.max_redirects:
                            return CompanyResearchFailure(
                                code=CompanyResearchFailureCode.REDIRECT_DISALLOWED,
                                diagnostic=(
                                    "company research redirect was missing or exceeded limit"
                                ),
                                url=canonical,
                            )
                        current_url = urljoin(canonical, location)
                        redirects_seen += 1
                        continue
                    if response.status_code >= 400:
                        return CompanyResearchFailure(
                            code=CompanyResearchFailureCode.CONNECTOR_HTTP_STATUS,
                            diagnostic=f"company research returned HTTP {response.status_code}",
                            url=canonical,
                        )
                    content_type = _base_content_type(response.headers.get("content-type"))
                    if content_type not in _TEXTUAL_CONTENT_TYPES:
                        return CompanyResearchFailure(
                            code=CompanyResearchFailureCode.CONTENT_TYPE_REJECTED,
                            diagnostic="company research content type is not textual",
                            url=canonical,
                        )
                    body = await _bounded_body(response, self.max_response_bytes)
            except httpx.TimeoutException:
                return CompanyResearchFailure(
                    code=CompanyResearchFailureCode.CONNECTOR_TIMEOUT,
                    diagnostic="company research request timed out",
                    url=current_url,
                )
            except PayloadTooLargeError:
                return CompanyResearchFailure(
                    code=CompanyResearchFailureCode.PAYLOAD_TOO_LARGE,
                    diagnostic="company research response exceeded the byte ceiling",
                    url=current_url,
                )
            except httpx.HTTPError:
                return CompanyResearchFailure(
                    code=CompanyResearchFailureCode.CONNECTOR_TRANSIENT,
                    diagnostic="company research request failed",
                    url=current_url,
                )

            text = _decode_text(body, response.headers.get("content-type"))
            title, visible_text = extract_visible_text(text, content_type=content_type)
            excerpt, truncated = _bounded_excerpt(visible_text, self.max_excerpt_chars)
            fingerprint = hashlib.sha256(visible_text.encode("utf-8")).hexdigest()
            return CompanyResearchSourceSnapshot(
                source_id=_source_id(canonical, fingerprint),
                source_url=str(response.url),
                canonical_url=canonical,
                title=title,
                source_class=source_class,
                retrieved_at=requested_at,
                content_fingerprint=fingerprint,
                excerpt=excerpt,
                excerpt_truncated=truncated,
                content_type=response.headers.get("content-type"),
                status_code=response.status_code,
                bytes_read=len(body),
            )

    async def _validate_public_https_url(self, url: str) -> str | CompanyResearchFailure:
        try:
            canonical = canonicalize_public_https_url(url)
        except ValueError as exc:
            return CompanyResearchFailure(
                code=CompanyResearchFailureCode.URL_UNSAFE,
                diagnostic=str(exc),
                url=url,
            )
        host = urlsplit(canonical).hostname
        if host is None:
            return CompanyResearchFailure(
                code=CompanyResearchFailureCode.URL_UNSAFE,
                diagnostic="company research URL is missing a hostname",
                url=url,
            )
        try:
            addresses = await self.resolver.resolve(host)
        except OSError:
            return CompanyResearchFailure(
                code=CompanyResearchFailureCode.DNS_RESOLUTION_FAILED,
                diagnostic="company research hostname could not be resolved",
                url=canonical,
            )
        try:
            validate_public_resolved_addresses(host, addresses)
        except ValueError as exc:
            return CompanyResearchFailure(
                code=CompanyResearchFailureCode.URL_UNSAFE,
                diagnostic=str(exc),
                url=canonical,
            )
        return canonical


class PayloadTooLargeError(ValueError):
    """Raised when a bounded response body exceeds the configured ceiling."""


def build_company_research_query(
    *,
    company: str,
    role: str | None,
    intent: CompanyResearchIntent,
    max_results: int,
) -> CompanyResearchSearchQuery:
    clean_company = _squash_whitespace(company)
    clean_role = _squash_whitespace(role or "")
    if not clean_company:
        raise ValueError("company research query requires a company name")
    phrase = _QUERY_PHRASES[intent]
    query_parts = [clean_company]
    if (
        intent
        in {
            CompanyResearchIntent.ROLE_REQUIREMENTS,
            CompanyResearchIntent.ENGINEERING_PRACTICES,
            CompanyResearchIntent.INTERVIEW_CONTEXT,
        }
        and clean_role
    ):
        query_parts.append(clean_role)
    query_parts.append(phrase)
    return CompanyResearchSearchQuery(
        query=" ".join(query_parts),
        company=clean_company,
        role=clean_role or None,
        intent=intent,
        max_results=max(0, max_results),
    )


def canonicalize_public_https_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https":
        raise ValueError("company research URL must use HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("company research URL must not include credentials")
    if parsed.hostname is None:
        raise ValueError("company research URL is missing a hostname")
    host = parsed.hostname.rstrip(".").lower()
    if not host or host in _BLOCKED_HOSTS or host.endswith(".local"):
        raise ValueError("company research URL host is not public")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("company research URL host must not be localhost")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("company research URL must use a public hostname, not an IP literal")

    port = parsed.port
    netloc = host if port in (None, 443) else f"{host}:{port}"
    query = _canonical_query(parsed.query)
    path = parsed.path or "/"
    return urlunsplit(("https", netloc, path, query, ""))


def validate_public_resolved_addresses(hostname: str, addresses: Iterable[str]) -> None:
    address_tuple = tuple(addresses)
    if not address_tuple:
        raise ValueError("company research hostname did not resolve")
    for address in address_tuple:
        ip_address = ipaddress.ip_address(address)
        if not ip_address.is_global:
            raise ValueError("company research hostname resolved to a non-public address")
        if ip_address.is_loopback or ip_address.is_link_local or ip_address.is_private:
            raise ValueError("company research hostname resolved to a blocked address")
    if hostname in _BLOCKED_HOSTS:
        raise ValueError("company research URL host is not public")


def extract_visible_text(
    raw_text: str,
    *,
    content_type: str | None = None,
) -> tuple[str | None, str]:
    if content_type == "text/plain":
        return None, _squash_whitespace(raw_text)
    parser = _VisibleTextParser()
    parser.feed(raw_text)
    parser.close()
    return parser.title, _squash_whitespace(" ".join(parser.text_parts))


async def _bounded_body(response: httpx.Response, max_response_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_response_bytes:
            raise PayloadTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


def _resolve_hostname(hostname: str) -> tuple[str, ...]:
    results = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    return tuple({str(result[4][0]) for result in results})


def _canonical_query(query: str) -> str:
    params = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    return urlencode(sorted(params), doseq=True)


def _base_content_type(value: str | None) -> str | None:
    if value is None:
        return None
    return value.split(";", 1)[0].strip().lower()


def _decode_text(body: bytes, content_type: str | None) -> str:
    charset = "utf-8"
    if content_type:
        match = re.search(r"charset=([\w.-]+)", content_type, flags=re.IGNORECASE)
        if match is not None:
            charset = match.group(1)
    return body.decode(charset, errors="replace")


def _bounded_excerpt(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip(), True


def _source_id(canonical_url: str, fingerprint: str) -> str:
    digest = hashlib.sha256(f"{canonical_url}\n{fingerprint}".encode()).hexdigest()
    return f"job-research:{digest[:24]}"


def _research_fingerprint(sources: Sequence[CompanyResearchSourceSnapshot]) -> str:
    hasher = hashlib.sha256()
    for source in sorted(sources, key=lambda item: item.source_id):
        hasher.update(source.canonical_url.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(source.content_fingerprint.encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def _squash_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("company research timestamps must be timezone-aware")
    return value.astimezone(UTC)


class _VisibleTextParser(HTMLParser):
    _SKIP_TAGS: ClassVar[frozenset[str]] = frozenset(
        {"form", "head", "noscript", "script", "style", "svg", "template"}
    )
    _BLOCK_TAGS: ClassVar[frozenset[str]] = frozenset(
        {
            "article",
            "br",
            "dd",
            "div",
            "dt",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "p",
            "section",
            "td",
            "th",
            "tr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self._title_parts: list[str] = []
        self._skip_stack: list[str] = []
        self._in_title = False

    @property
    def title(self) -> str | None:
        title = _squash_whitespace(" ".join(self._title_parts))
        return title or None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value for key, value in attrs}
        lower_tag = tag.lower()
        if lower_tag == "title":
            self._in_title = True
        if lower_tag in self._SKIP_TAGS or _is_hidden(attrs_map):
            self._skip_stack.append(lower_tag)
        elif lower_tag in self._BLOCK_TAGS and not self._skip_stack:
            self.text_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lower_tag = tag.lower()
        if lower_tag == "title":
            self._in_title = False
        if self._skip_stack and lower_tag == self._skip_stack[-1]:
            self._skip_stack.pop()
        elif lower_tag in self._BLOCK_TAGS and not self._skip_stack:
            self.text_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if not self._skip_stack and not self._in_title:
            self.text_parts.append(data)


def _is_hidden(attrs: Mapping[str, str | None]) -> bool:
    if "hidden" in attrs:
        return True
    aria_hidden = attrs.get("aria-hidden") or ""
    if aria_hidden.lower() == "true":
        return True
    style = (attrs.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


__all__ = [
    "CompanyResearchClient",
    "CompanyResearchDnsResolver",
    "CompanyResearchFailure",
    "CompanyResearchFailureCode",
    "CompanyResearchIntent",
    "CompanyResearchRequest",
    "CompanyResearchResult",
    "CompanyResearchSearchProvider",
    "CompanyResearchSearchQuery",
    "CompanyResearchSearchResult",
    "CompanyResearchSearchResultItem",
    "CompanyResearchSourceClass",
    "CompanyResearchSourceSnapshot",
    "SocketCompanyResearchDnsResolver",
    "UnconfiguredCompanyResearchSearchProvider",
    "canonicalize_public_https_url",
    "extract_visible_text",
    "validate_public_resolved_addresses",
]
