"""Host-local Waterloo LEARN bridge.

The bridge is intentionally standalone: it does not import backend settings,
database, queue, model, or Docker-facing code. It exposes a loopback-only HTTP
API backed by a dedicated Playwright persistent profile that remains on the host.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict, cast
from urllib.parse import urljoin, urlparse

from pydantic import SecretStr

LEARN_BASE_URL = "https://learn.uwaterloo.ca"
DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765"
DEFAULT_MAX_REQUEST_BYTES = 16_384
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_TIMESTAMP_SKEW_SECONDS = 300
DEFAULT_NONCE_TTL_SECONDS = 600
DEFAULT_LOOKBACK_HOURS = 72
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_COURSE_LIMIT = 100
DEFAULT_ITEM_LIMIT = 500
DEFAULT_ANNOUNCEMENT_LIMIT = 250
DEFAULT_FRAGMENT_LIMIT = 80
DEFAULT_FRAGMENT_CHARS = 4_000
DEFAULT_ANNOUNCEMENT_BODY_CHARS = 60_000
_SIGNATURE_PREFIX = "sha256="
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{16,128}$")
_ORG_UNIT = re.compile(r"/d2l/(?:home|le/(?:content|news|calendar))/([0-9]+)")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_COURSE_CODE = re.compile(r"\b[A-Z]{2,8}\s*[0-9][A-Z0-9]{1,5}[A-Z]?\b")


HealthStatus = Literal["ready", "login_required", "browser_unavailable"]
DatePrecision = Literal["date", "datetime", "unknown"]


class LearnCourse(TypedDict):
    org_unit_id: str
    code: str
    name: str
    term: str | None
    active: bool
    url: str


class LearnScheduledItem(TypedDict):
    source_id: str
    course_org_unit_id: str
    course_code: str
    title: str
    start_at: str | None
    due_at: str | None
    end_at: str | None
    date_precision: Literal["date", "datetime"]
    completed: bool
    url: str
    fingerprint: str


class ScheduledItemDomRecord(TypedDict):
    source_key: str
    href: str | None
    text: str
    start_at: str | None
    due_at: str | None
    end_at: str | None
    completed: bool


class LearnAnnouncementEvidence(TypedDict):
    source_id: str
    course_org_unit_id: str
    course_code: str
    published_at: str
    updated_at: str | None
    body_fragments: list[str]
    attachments_present: bool
    oversized: bool
    url: str
    fingerprint: str


class AnnouncementDomRecord(TypedDict):
    source_key: str
    href: str | None
    text: str
    published_at: str | None
    updated_at: str | None
    attachments_present: bool


class LearnSnapshot(TypedDict):
    status: HealthStatus
    generated_at: str
    courses: list[LearnCourse]
    scheduled_items: list[LearnScheduledItem]
    announcements: list[LearnAnnouncementEvidence]


class SnapshotRequest(TypedDict, total=False):
    since: str
    until: str
    course_ids: list[str]
    course_limit: int
    item_limit: int
    announcement_limit: int
    include_announcements: bool
    include_scheduled_items: bool


class LearnBrowserAdapter(Protocol):
    def health(self) -> HealthStatus:
        """Return host browser/session readiness."""
        ...

    def snapshot(self, request: SnapshotRequest) -> LearnSnapshot:
        """Return bounded LEARN data visible to the authenticated session."""
        ...

    def login(self) -> HealthStatus:
        """Open headed browser login flow and verify session persistence."""
        ...

    def close(self) -> None:
        """Release browser resources."""
        ...


class LearnBridgeRejectedError(RuntimeError):
    """Safe bridge rejection code."""

    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LearnBridgeSettings:
    """Validated settings for the host-only LEARN bridge."""

    secret: SecretStr
    profile_dir: Path
    repository_root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    learn_base_url: str = LEARN_BASE_URL
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    timestamp_skew_seconds: int = DEFAULT_TIMESTAMP_SKEW_SECONDS
    nonce_ttl_seconds: int = DEFAULT_NONCE_TTL_SECONDS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS
    course_limit: int = DEFAULT_COURSE_LIMIT
    item_limit: int = DEFAULT_ITEM_LIMIT
    announcement_limit: int = DEFAULT_ANNOUNCEMENT_LIMIT
    fragment_limit: int = DEFAULT_FRAGMENT_LIMIT
    fragment_chars: int = DEFAULT_FRAGMENT_CHARS
    announcement_body_chars: int = DEFAULT_ANNOUNCEMENT_BODY_CHARS

    def __post_init__(self) -> None:
        _require_secret("LEARN_BRIDGE_HMAC_SECRET", self.secret)
        host_ip = ipaddress.ip_address(self.host)
        if not host_ip.is_loopback:
            raise ValueError("LEARN bridge host must be loopback-only")
        if self.port < 0 or self.port > 65_535:
            raise ValueError("LEARN bridge port must be between 0 and 65535")
        root = self.repository_root.expanduser().resolve()
        profile = self.profile_dir.expanduser().resolve()
        if _is_relative_to(profile, root):
            raise ValueError("LEARN Playwright profile must live outside the repository")
        if ".artifacts" in profile.parts or "docker" in {part.casefold() for part in profile.parts}:
            raise ValueError("LEARN Playwright profile must not live in artifacts or Docker paths")
        parsed = urlparse(self.learn_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("LEARN base URL must be an HTTPS URL")
        for name, value, upper in (
            ("max_request_bytes", self.max_request_bytes, 1_000_000),
            ("max_response_bytes", self.max_response_bytes, 5_000_000),
            ("timestamp_skew_seconds", self.timestamp_skew_seconds, 3_600),
            ("nonce_ttl_seconds", self.nonce_ttl_seconds, 7_200),
            ("timeout_seconds", self.timeout_seconds, 120),
            ("lookback_hours", self.lookback_hours, 24 * 14),
            ("course_limit", self.course_limit, 500),
            ("item_limit", self.item_limit, 1_000),
            ("announcement_limit", self.announcement_limit, 1_000),
            ("fragment_limit", self.fragment_limit, 80),
            ("fragment_chars", self.fragment_chars, 5_000),
            ("announcement_body_chars", self.announcement_body_chars, 250_000),
        ):
            if value <= 0 or value > upper:
                raise ValueError(f"{name} must be positive and <= {upper}")
        object.__setattr__(self, "repository_root", root)
        object.__setattr__(self, "profile_dir", profile)
        object.__setattr__(self, "learn_base_url", self.learn_base_url.rstrip("/"))

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        repository_root: Path | None = None,
    ) -> LearnBridgeSettings:
        env = environ or os.environ
        root = (repository_root or Path(env.get("LIFEAGENT_REPOSITORY_ROOT", "."))).resolve()
        return cls(
            secret=SecretStr(_required(env, "LEARN_BRIDGE_HMAC_SECRET")),
            profile_dir=Path(
                env.get("LEARN_BRIDGE_PROFILE_DIR")
                or default_profile_dir()
            ),
            repository_root=root,
            host=env.get("LEARN_BRIDGE_HOST", "127.0.0.1"),
            port=_env_int(env, "LEARN_BRIDGE_PORT", 8765),
            learn_base_url=env.get("LEARN_BASE_URL", LEARN_BASE_URL),
            max_request_bytes=_env_int(
                env,
                "LEARN_BRIDGE_MAX_REQUEST_BYTES",
                DEFAULT_MAX_REQUEST_BYTES,
            ),
            max_response_bytes=_env_int(
                env,
                "LEARN_BRIDGE_MAX_RESPONSE_BYTES",
                DEFAULT_MAX_RESPONSE_BYTES,
            ),
            timestamp_skew_seconds=_env_int(
                env,
                "LEARN_BRIDGE_MAX_CLOCK_SKEW_SECONDS",
                DEFAULT_TIMESTAMP_SKEW_SECONDS,
            ),
            nonce_ttl_seconds=_env_int(
                env,
                "LEARN_BRIDGE_NONCE_TTL_SECONDS",
                DEFAULT_NONCE_TTL_SECONDS,
            ),
            timeout_seconds=_env_int(env, "LEARN_BRIDGE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            lookback_hours=_env_int(
                env,
                "LEARN_ANNOUNCEMENT_LOOKBACK_HOURS",
                DEFAULT_LOOKBACK_HOURS,
            ),
            course_limit=_env_int(env, "LEARN_BRIDGE_MAX_COURSES", DEFAULT_COURSE_LIMIT),
            item_limit=_env_int(
                env,
                "LEARN_BRIDGE_MAX_SCHEDULED_ITEMS",
                DEFAULT_ITEM_LIMIT,
            ),
            announcement_limit=_env_int(
                env,
                "LEARN_BRIDGE_MAX_ANNOUNCEMENTS",
                DEFAULT_ANNOUNCEMENT_LIMIT,
            ),
            announcement_body_chars=_env_int(
                env,
                "LEARN_ANNOUNCEMENT_MAX_BODY_CHARS",
                DEFAULT_ANNOUNCEMENT_BODY_CHARS,
            ),
        )


class NonceStore:
    """Small in-memory replay cache for signed bridge requests."""

    def __init__(self, ttl_seconds: int, *, clock: Callable[[], float] = time.time) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._nonces: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def accept(self, nonce: str, timestamp: int) -> bool:
        now = self._clock()
        with self._lock:
            self._prune(now)
            if nonce in self._nonces:
                return False
            self._nonces[nonce] = float(timestamp)
            return True

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl_seconds
        while self._nonces:
            first_nonce, first_timestamp = next(iter(self._nonces.items()))
            if first_timestamp >= cutoff:
                break
            self._nonces.pop(first_nonce, None)


class LearnBridgeRequestVerifier:
    """Verify HMAC, timestamp, nonce, body size, and loopback origin."""

    def __init__(
        self,
        settings: LearnBridgeSettings,
        nonce_store: NonceStore,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._nonce_store = nonce_store
        self._clock = clock

    def verify(
        self,
        *,
        client_host: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> tuple[int, str]:
        if not is_loopback_host(client_host):
            raise LearnBridgeRejectedError("non_local", HTTPStatus.FORBIDDEN)
        if len(body) > self._settings.max_request_bytes:
            raise LearnBridgeRejectedError("request_too_large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        timestamp_header = _header(headers, "x-lifeagent-learn-timestamp")
        nonce = _header(headers, "x-lifeagent-learn-nonce")
        signature = _header(headers, "x-lifeagent-learn-signature")
        if not timestamp_header or not nonce or not signature:
            raise LearnBridgeRejectedError("unsigned", HTTPStatus.UNAUTHORIZED)
        if _NONCE_PATTERN.fullmatch(nonce) is None:
            raise LearnBridgeRejectedError("bad_nonce", HTTPStatus.UNAUTHORIZED)
        try:
            timestamp = int(timestamp_header)
        except ValueError as exc:
            raise LearnBridgeRejectedError("bad_timestamp", HTTPStatus.UNAUTHORIZED) from exc
        if abs(self._clock() - timestamp) > self._settings.timestamp_skew_seconds:
            raise LearnBridgeRejectedError("stale", HTTPStatus.UNAUTHORIZED)
        expected = sign_bridge_message(
            secret=self._settings.secret,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        if not hmac.compare_digest(expected, signature):
            raise LearnBridgeRejectedError("bad_signature", HTTPStatus.UNAUTHORIZED)
        if not self._nonce_store.accept(nonce, timestamp):
            raise LearnBridgeRejectedError("replay", HTTPStatus.CONFLICT)
        return timestamp, nonce


class PlaywrightLearnBrowser:
    """Minimal Brightspace browser adapter using only user-visible pages."""

    def __init__(self, settings: LearnBridgeSettings) -> None:
        self._settings = settings
        self._playwright: Any | None = None
        self._context: Any | None = None
        self._lock = threading.Lock()

    def health(self) -> HealthStatus:
        try:
            with self._page(headless=True) as page:
                page.goto(self._settings.learn_base_url, wait_until="domcontentloaded")
                return "login_required" if _is_login_page(page) else "ready"
        except _LoginRequiredError:
            return "login_required"
        except Exception:
            return "browser_unavailable"

    def snapshot(self, request: SnapshotRequest) -> LearnSnapshot:
        generated_at = datetime.now(UTC).isoformat()
        with self._page(headless=True) as page:
            page.goto(self._settings.learn_base_url, wait_until="domcontentloaded")
            if _is_login_page(page):
                return _empty_snapshot("login_required", generated_at)
            courses = self._courses(page)
            course_ids = set(request.get("course_ids") or [])
            if course_ids:
                courses = [course for course in courses if course["org_unit_id"] in course_ids]
            course_limit = _bounded_int(
                request.get("course_limit"),
                default=self._settings.course_limit,
                upper=self._settings.course_limit,
            )
            item_limit = _bounded_int(
                request.get("item_limit"),
                default=self._settings.item_limit,
                upper=self._settings.item_limit,
            )
            announcement_limit = _bounded_int(
                request.get("announcement_limit"),
                default=self._settings.announcement_limit,
                upper=self._settings.announcement_limit,
            )
            courses = courses[:course_limit]
            scheduled_items: list[LearnScheduledItem] = []
            announcements: list[LearnAnnouncementEvidence] = []
            since, until = _request_window(request, self._settings.lookback_hours)
            for course in courses:
                if (
                    request.get("include_scheduled_items", True)
                    and len(scheduled_items) < item_limit
                ):
                    scheduled_items.extend(
                        self._scheduled_items(page, course, since, until)[
                            : item_limit - len(scheduled_items)
                        ]
                    )
                if (
                    request.get("include_announcements", True)
                    and len(announcements) < announcement_limit
                ):
                    announcements.extend(
                        self._announcements(page, course, since, until)[
                            : announcement_limit - len(announcements)
                        ]
                    )
            return {
                "status": "ready",
                "generated_at": generated_at,
                "courses": courses,
                "scheduled_items": scheduled_items,
                "announcements": announcements,
            }

    def login(self) -> HealthStatus:
        ensure_private_profile_dir(self._settings.profile_dir)
        playwright = _sync_playwright().start()
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._settings.profile_dir),
            headless=False,
            viewport={"width": 1440, "height": 1000},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(self._settings.learn_base_url, wait_until="domcontentloaded")
        input("Complete Waterloo SSO/MFA in Chromium, then press Enter here to close it. ")
        visible_status: HealthStatus = "login_required" if _is_login_page(page) else "ready"
        print(f"learn_bridge_login_visible={visible_status}")
        context.close()
        playwright.stop()
        status = self.health()
        self.close()
        return status

    def close(self) -> None:
        with self._lock:
            if self._context is not None:
                self._context.close()
                self._context = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

    def _page(self, *, headless: bool) -> _PageLease:
        ensure_private_profile_dir(self._settings.profile_dir)
        with self._lock:
            if self._context is None:
                self._playwright = _sync_playwright().start()
                playwright = self._playwright
                if playwright is None:
                    raise RuntimeError("Playwright failed to start")
                self._context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self._settings.profile_dir),
                    headless=headless,
                    viewport={"width": 1440, "height": 1000},
                    timeout=self._settings.timeout_seconds * 1000,
                )
            context = self._context
            if context is None:
                raise RuntimeError("Playwright browser context failed to start")
            page = context.new_page()
            page.set_default_timeout(self._settings.timeout_seconds * 1000)
            return _PageLease(page)

    def _courses(self, page: Any) -> list[LearnCourse]:
        links = _link_records(page, self._settings.learn_base_url)
        courses: OrderedDict[str, LearnCourse] = OrderedDict()
        for link in links:
            match = re.search(r"/d2l/home/([0-9]+)", link["href"])
            if match is None:
                continue
            text = clean_text(link["text"], max_chars=240)
            if not text:
                continue
            org_unit_id = match.group(1)
            courses.setdefault(
                org_unit_id,
                {
                    "org_unit_id": org_unit_id,
                    "code": _course_code(text) or text[:80],
                    "name": text,
                    "term": _term_from_text(text),
                    "active": True,
                    "url": _same_origin_url(self._settings.learn_base_url, link["href"]),
                },
            )
        return list(courses.values())

    def _scheduled_items(
        self,
        page: Any,
        course: LearnCourse,
        since: datetime,
        until: datetime,
    ) -> list[LearnScheduledItem]:
        calendar_url = f"{self._settings.learn_base_url}/d2l/le/calendar/{course['org_unit_id']}"
        records: list[LearnScheduledItem] = []
        for url in _page_urls(page, calendar_url, self._settings.learn_base_url):
            page.goto(url, wait_until="domcontentloaded")
            if _is_login_page(page):
                raise _LoginRequiredError
            for item in _scheduled_item_records(page):
                title = clean_text(item["text"], max_chars=240)
                if not title:
                    continue
                start_value = _parse_dom_date_value(item["start_at"])
                due_value = _parse_dom_date_value(item["due_at"])
                end_value = _parse_dom_date_value(item["end_at"])
                values = tuple(
                    value
                    for value in (start_value, due_value, end_value)
                    if value is not None
                )
                if not values:
                    continue
                if not any(_date_value_in_window(value, since, until) for value in values):
                    continue
                source_url = _same_origin_url(
                    self._settings.learn_base_url,
                    item["href"] or url,
                )
                source_id = _stable_source_id(
                    course["org_unit_id"],
                    "scheduled",
                    source_url,
                    item["source_key"],
                )
                payload = {
                    "course": course["org_unit_id"],
                    "title": title,
                    "start_at": _dom_value_iso(start_value),
                    "due_at": _dom_value_iso(due_value),
                    "end_at": _dom_value_iso(end_value),
                    "completed": item["completed"],
                    "url": source_url,
                }
                records.append(
                    {
                        "source_id": source_id,
                        "course_org_unit_id": course["org_unit_id"],
                        "course_code": course["code"],
                        "title": title,
                        "start_at": _dom_value_iso(start_value),
                        "due_at": _dom_value_iso(due_value),
                        "end_at": _dom_value_iso(end_value),
                        "date_precision": (
                            "datetime"
                            if any(isinstance(value, datetime) for value in values)
                            else "date"
                        ),
                        "completed": item["completed"],
                        "url": source_url,
                        "fingerprint": _fingerprint(payload),
                    }
                )
                if len(records) >= self._settings.item_limit:
                    return records
        return records

    def _announcements(
        self,
        page: Any,
        course: LearnCourse,
        since: datetime,
        until: datetime,
    ) -> list[LearnAnnouncementEvidence]:
        news_url = f"{self._settings.learn_base_url}/d2l/le/news/{course['org_unit_id']}"
        records: list[LearnAnnouncementEvidence] = []
        for url in _page_urls(page, news_url, self._settings.learn_base_url):
            page.goto(url, wait_until="domcontentloaded")
            if _is_login_page(page):
                raise _LoginRequiredError
            for record in _announcement_records(page):
                text = record["text"]
                cleaned = _normalized_body(text)
                if not cleaned:
                    continue
                oversized = len(cleaned) > self._settings.announcement_body_chars
                fragments = (
                    ["Announcement exceeds the configured semantic body limit."]
                    if oversized
                    else _fragments(
                        cleaned,
                        max_fragments=self._settings.fragment_limit,
                        max_chars=self._settings.fragment_chars,
                    )
                )
                source_url = _same_origin_url(
                    self._settings.learn_base_url,
                    record["href"] or url,
                )
                published = _parse_dom_datetime(record["published_at"])
                updated = _parse_dom_datetime(record["updated_at"])
                if published is None:
                    continue
                effective = max(published, updated) if updated is not None else published
                if effective < since or effective > until:
                    continue
                source_id = _stable_source_id(
                    course["org_unit_id"],
                    "announcement",
                    source_url,
                    record["source_key"],
                )
                payload = {
                    "course": course["org_unit_id"],
                    "body_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "url": source_url,
                }
                records.append(
                    {
                        "source_id": source_id,
                        "course_org_unit_id": course["org_unit_id"],
                        "course_code": course["code"],
                        "published_at": published.isoformat(),
                        "updated_at": updated.isoformat() if updated is not None else None,
                        "body_fragments": fragments,
                        "attachments_present": record["attachments_present"],
                        "oversized": oversized,
                        "url": source_url,
                        "fingerprint": _fingerprint(payload),
                    }
                )
                if len(records) >= self._settings.announcement_limit:
                    return records
        return records


@dataclass(slots=True)
class LearnBridgeService:
    settings: LearnBridgeSettings
    adapter: LearnBrowserAdapter
    nonce_store: NonceStore = field(init=False)

    def __post_init__(self) -> None:
        self.nonce_store = NonceStore(self.settings.nonce_ttl_seconds)

    def serve_forever(self) -> None:
        ensure_private_profile_dir(self.settings.profile_dir)
        server = build_http_server(self.settings, self.adapter, self.nonce_store)
        try:
            server.serve_forever()
        finally:
            self.adapter.close()
            server.server_close()


class _PageLease:
    def __init__(self, page: Any) -> None:
        self._page = page

    def __enter__(self) -> Any:
        return self._page

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self._page.close()


class _LoginRequiredError(RuntimeError):
    pass


class _LearnBridgeHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        settings: LearnBridgeSettings,
        adapter: LearnBrowserAdapter,
        nonce_store: NonceStore,
    ) -> None:
        super().__init__(server_address, handler)
        self.settings = settings
        self.adapter = adapter
        self.nonce_store = nonce_store
        self.verifier = LearnBridgeRequestVerifier(settings, nonce_store)


class _LearnBridgeHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def _handle(self) -> None:
        path = urlparse(self.path).path
        body = b""
        bridge_server = self._bridge_server()
        try:
            headers = dict(self.headers.items())
            content_length = _content_length(headers)
            if content_length > bridge_server.settings.max_request_bytes:
                raise LearnBridgeRejectedError(
                    "request_too_large",
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
            body = self.rfile.read(content_length) if content_length else b""
            bridge_server.verifier.verify(
                client_host=self.client_address[0],
                method=self.command,
                path=path,
                headers=headers,
                body=body,
            )
            if self.command == "GET" and path == "/health":
                self._send_json(HTTPStatus.OK, {"status": bridge_server.adapter.health()})
                return
            if self.command == "POST" and path == "/v1/snapshot":
                request = _decode_snapshot_request(body)
                snapshot = bridge_server.adapter.snapshot(request)
                self._send_json(HTTPStatus.OK, snapshot)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except LearnBridgeRejectedError as exc:
            self._send_json(exc.status_code, {"error": exc.code})
        except _LoginRequiredError:
            self._send_json(
                HTTPStatus.OK,
                _empty_snapshot("login_required", datetime.now(UTC).isoformat()),
            )
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_json"})
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request"})
        except Exception:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "browser_unavailable"})

    def _send_json(self, status_code: int, payload: Mapping[str, object]) -> None:
        bridge_server = self._bridge_server()
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(body) > bridge_server.settings.max_response_bytes:
            status_code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            body = b'{"error":"response_too_large"}'
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(24)
        signature = sign_bridge_response(
            secret=bridge_server.settings.secret,
            status_code=int(status_code),
            path=urlparse(self.path).path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        self.send_response(int(status_code))
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("x-lifeagent-learn-timestamp", str(timestamp))
        self.send_header("x-lifeagent-learn-nonce", nonce)
        self.send_header("x-lifeagent-learn-signature", signature)
        self.end_headers()
        self.wfile.write(body)

    def _bridge_server(self) -> _LearnBridgeHTTPServer:
        return cast(_LearnBridgeHTTPServer, self.server)


def build_http_server(
    settings: LearnBridgeSettings,
    adapter: LearnBrowserAdapter,
    nonce_store: NonceStore | None = None,
) -> ThreadingHTTPServer:
    return _LearnBridgeHTTPServer(
        (settings.host, settings.port),
        _LearnBridgeHandler,
        settings=settings,
        adapter=adapter,
        nonce_store=nonce_store or NonceStore(settings.nonce_ttl_seconds),
    )


def sign_bridge_message(
    *,
    secret: SecretStr,
    method: str,
    path: str,
    timestamp: int,
    nonce: str,
    body: bytes,
) -> str:
    material = b"\n".join(
        (
            method.upper().encode("ascii"),
            path.encode("utf-8"),
            str(timestamp).encode("ascii"),
            nonce.encode("utf-8"),
            hashlib.sha256(body).hexdigest().encode("ascii"),
        )
    )
    digest = hmac.new(secret.get_secret_value().encode("utf-8"), material, hashlib.sha256)
    return f"{_SIGNATURE_PREFIX}{digest.hexdigest()}"


def sign_bridge_response(
    *,
    secret: SecretStr,
    status_code: int,
    path: str,
    timestamp: int,
    nonce: str,
    body: bytes,
) -> str:
    material = b"\n".join(
        (
            b"RESPONSE",
            str(status_code).encode("ascii"),
            path.encode("utf-8"),
            str(timestamp).encode("ascii"),
            nonce.encode("utf-8"),
            hashlib.sha256(body).hexdigest().encode("ascii"),
        )
    )
    digest = hmac.new(secret.get_secret_value().encode("utf-8"), material, hashlib.sha256)
    return f"{_SIGNATURE_PREFIX}{digest.hexdigest()}"


def verify_bridge_response(
    *,
    secret: SecretStr,
    status_code: int,
    path: str,
    timestamp: int,
    nonce: str,
    body: bytes,
    signature: str,
) -> bool:
    expected = sign_bridge_response(
        secret=secret,
        status_code=status_code,
        path=path,
        timestamp=timestamp,
        nonce=nonce,
        body=body,
    )
    return hmac.compare_digest(expected, signature)


def signed_headers(
    *,
    secret: SecretStr,
    method: str,
    path: str,
    body: bytes = b"",
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    actual_timestamp = timestamp if timestamp is not None else int(time.time())
    actual_nonce = nonce or secrets.token_urlsafe(24)
    signature = sign_bridge_message(
        secret=secret,
        method=method,
        path=path,
        timestamp=actual_timestamp,
        nonce=actual_nonce,
        body=body,
    )
    return {
        "x-lifeagent-learn-timestamp": str(actual_timestamp),
        "x-lifeagent-learn-nonce": actual_nonce,
        "x-lifeagent-learn-signature": signature,
    }


def is_loopback_host(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.casefold() == "localhost"


def ensure_private_profile_dir(profile_dir: Path) -> None:
    profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    current_mode = stat.S_IMODE(profile_dir.stat().st_mode)
    if current_mode != 0o700:
        profile_dir.chmod(0o700)


def default_profile_dir() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "LifeAgent"
        / "learn-bridge"
        / "profile"
    )


def _decode_snapshot_request(body: bytes) -> SnapshotRequest:
    if not body:
        return {}
    raw_object: object = json.loads(body.decode("utf-8"))
    if not isinstance(raw_object, dict):
        raise ValueError("snapshot request must be a JSON object")
    raw = cast(dict[str, object], raw_object)
    result: SnapshotRequest = {}
    if "since" in raw:
        result["since"] = _string_field(raw, "since", 40)
    if "until" in raw:
        result["until"] = _string_field(raw, "until", 40)
    if "course_ids" in raw:
        values_object = raw["course_ids"]
        if not isinstance(values_object, list):
            raise ValueError("course_ids must be a bounded list")
        values = cast(list[object], values_object)
        if len(values) > DEFAULT_COURSE_LIMIT:
            raise ValueError("course_ids must be a bounded list")
        result["course_ids"] = [_course_id_field(value) for value in values]
    for key in ("course_limit", "item_limit", "announcement_limit"):
        if key in raw:
            value = raw[key]
            if not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            if key == "course_limit":
                result["course_limit"] = value
            elif key == "item_limit":
                result["item_limit"] = value
            else:
                result["announcement_limit"] = value
    for key in ("include_announcements", "include_scheduled_items"):
        if key in raw:
            value = raw[key]
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            result[key] = value
    return result


def _request_window(request: SnapshotRequest, lookback_hours: int) -> tuple[datetime, datetime]:
    until = _parse_datetime(request.get("until")) or datetime.now(UTC)
    since = _parse_datetime(request.get("since")) or (until - timedelta(hours=lookback_hours))
    if since > until:
        raise ValueError("since must be before until")
    if until - since > timedelta(days=31):
        raise ValueError("snapshot window must be <= 31 days")
    return since, until


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_dom_datetime(value: str | None) -> datetime | None:
    """Parse machine-readable DOM timestamps without guessing locale or timezone."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def clean_text(value: str, *, max_chars: int) -> str:
    text = html.unescape(value)
    text = _CONTROL_CHARS.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _fragments(text: str, *, max_fragments: int, max_chars: int) -> list[str]:
    fragments: list[str] = []
    for paragraph in (part for part in text.split("\n\n") if part):
        for offset in range(0, len(paragraph), max_chars):
            fragments.append(paragraph[offset : offset + max_chars])
            if len(fragments) >= max_fragments:
                return fragments
    return fragments


def _normalized_body(value: str) -> str:
    text = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub(" ", text)
    paragraphs = [
        _WHITESPACE.sub(" ", paragraph).strip()
        for paragraph in re.split(r"\n\s*\n+", text)
    ]
    return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)


def _link_records(page: Any, base_url: str) -> list[dict[str, str]]:
    return cast(
        list[dict[str, str]],
        page.eval_on_selector_all(
            "a[href]",
            """(links) => links.map((link) => ({
                href: link.href || link.getAttribute('href') || '',
                text: link.innerText || link.textContent || ''
            }))""",
        ),
    )


def _scheduled_item_records(page: Any) -> list[ScheduledItemDomRecord]:
    return cast(
        list[ScheduledItemDomRecord],
        page.eval_on_selector_all(
            "article, d2l-card, li, tr, [role='listitem']",
            """(items) => items.map((item) => {
                const link = item.querySelector('a[href]');
                const times = Array.from(item.querySelectorAll('time[datetime]'))
                    .map((node) => node.getAttribute('datetime'))
                    .filter(Boolean);
                const stableId = item.getAttribute('data-event-id')
                    || item.getAttribute('data-calendar-event-id')
                    || item.id
                    || (link ? link.href : '');
                return {
                    source_key: stableId,
                    href: link ? link.href : null,
                    text: item.innerText || item.textContent || '',
                    start_at: item.getAttribute('data-start-date') || times[0] || null,
                    due_at: item.getAttribute('data-due-date') || null,
                    end_at: item.getAttribute('data-end-date') || times[1] || null,
                    completed: item.getAttribute('data-completed') === 'true'
                };
            }).filter((item) => item.source_key && item.text && item.text.trim().length > 0)""",
        ),
    )


def _announcement_records(page: Any) -> list[AnnouncementDomRecord]:
    return cast(
        list[AnnouncementDomRecord],
        page.eval_on_selector_all(
            "article, d2l-card, .d2l-le-news-item, li, [role='article']",
            """(items) => items.map((item) => {
                const link = item.querySelector('a[href]');
                const body = item.querySelector(
                    '.d2l-htmlblock, .d2l-le-news-posting-content, '
                    + '[data-testid="announcement-content"]'
                );
                const metadataTimes = Array.from(item.querySelectorAll('time[datetime]'))
                    .filter((node) => !body || !body.contains(node))
                    .map((node) => node.getAttribute('datetime'))
                    .filter(Boolean);
                const published = item.getAttribute('data-published-date')
                    || item.getAttribute('data-created-date')
                    || metadataTimes[0]
                    || null;
                const updated = item.getAttribute('data-updated-date')
                    || item.getAttribute('data-modified-date')
                    || metadataTimes[1]
                    || null;
                const stableId = item.getAttribute('data-announcement-id')
                    || item.getAttribute('data-news-id')
                    || item.id
                    || (link ? link.href : '');
                const attachment = item.querySelector(
                    'a[download], a[href*="/content/enforced/"], a[href*="/viewFile.d2l"]'
                );
                return {
                    source_key: stableId,
                    href: link ? link.href : null,
                    text: (body
                        ? body.innerText || body.textContent
                        : item.innerText || item.textContent) || '',
                    published_at: published,
                    updated_at: updated,
                    attachments_present: Boolean(attachment)
                };
            }).filter((item) => item.source_key && item.text && item.text.trim().length > 0)""",
        ),
    )


def _page_urls(page: Any, start_url: str, base_url: str, *, max_pages: int = 5) -> Iterable[str]:
    current_url = start_url
    seen: set[str] = set()
    for _ in range(max_pages):
        if current_url in seen:
            return
        seen.add(current_url)
        yield current_url
        try:
            links = _link_records(page, base_url)
        except Exception:
            return
        next_url = None
        for link in links:
            text = clean_text(link["text"], max_chars=80).casefold()
            if text in {"next", "next page", "older"}:
                next_url = _same_origin_url(base_url, link["href"])
                break
        if next_url is None:
            return
        current_url = next_url


def _is_login_page(page: Any) -> bool:
    parsed = urlparse(str(page.url))
    hostname = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    if hostname != "learn.uwaterloo.ca":
        return True
    return path.startswith("/d2l/login") or "/signin" in path or "/saml" in path


def _same_origin_url(base_url: str, href: str) -> str:
    joined = urljoin(base_url, href)
    base = urlparse(base_url)
    parsed = urlparse(joined)
    if parsed.scheme != base.scheme or parsed.netloc != base.netloc:
        return base_url
    return joined


def _parse_dom_date_value(value: str | None) -> date | datetime | None:
    if value is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return _parse_dom_datetime(value)


def _date_value_in_window(value: date | datetime, since: datetime, until: datetime) -> bool:
    if isinstance(value, datetime):
        return since <= value <= until
    return since.date() <= value <= until.date()


def _dom_value_iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _course_code(text: str) -> str | None:
    match = _COURSE_CODE.search(text.upper())
    if match is None:
        return None
    return _WHITESPACE.sub(" ", match.group(0)).strip()


def _term_from_text(text: str) -> str | None:
    lowered = text.casefold()
    for term in ("winter", "spring", "fall"):
        if term in lowered:
            year_match = re.search(r"\b20[0-9]{2}\b", text)
            return f"{term.title()} {year_match.group(0)}" if year_match else term.title()
    return None


def _stable_source_id(course_id: str, kind: str, url: str, text: str) -> str:
    digest = hashlib.sha256(f"{course_id}\n{kind}\n{url}\n{text}".encode()).hexdigest()
    return f"learn:{kind}:{course_id}:{digest[:24]}"


def _fingerprint(payload: object) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _empty_snapshot(status: HealthStatus, generated_at: str) -> LearnSnapshot:
    return {
        "status": status,
        "generated_at": generated_at,
        "courses": [],
        "scheduled_items": [],
        "announcements": [],
    }


def _bounded_int(value: object, *, default: int, upper: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value <= 0 or value > upper:
        raise ValueError("bounded integer is invalid")
    return value


def _content_length(headers: Mapping[str, str]) -> int:
    raw = _header(headers, "content-length")
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise LearnBridgeRejectedError("bad_content_length", HTTPStatus.BAD_REQUEST) from exc
    if value < 0:
        raise LearnBridgeRejectedError("bad_content_length", HTTPStatus.BAD_REQUEST)
    return value


def _header(headers: Mapping[str, str], key: str) -> str | None:
    for header_key, value in headers.items():
        if header_key.casefold() == key:
            return value
    return None


def _string_field(raw: Mapping[str, object], key: str, max_length: int) -> str:
    value = raw[key]
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{key} is invalid")
    return value


def _course_id_field(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,20}", value) is None:
        raise ValueError("course ID is invalid")
    return value


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        raise ValueError(f"{key} must be a positive integer")
    return int(raw)


def _require_secret(name: str, value: SecretStr) -> None:
    if len(value.get_secret_value()) < 32:
        raise ValueError(f"{name} must be at least 32 characters")


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True


def _sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed in the host runtime") from exc
    return sync_playwright()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the host-local Waterloo LEARN bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "login", "health", "verify"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--secret-file", type=Path)
        subparser.add_argument("--profile-dir", type=Path)
        subparser.add_argument("--host", default="127.0.0.1")
        subparser.add_argument("--port", type=int, default=8765)
        subparser.add_argument("--repository-root", type=Path, default=Path.cwd())
        subparser.add_argument("--learn-base-url", default=LEARN_BASE_URL)
    return parser


def _settings_from_args(args: argparse.Namespace) -> LearnBridgeSettings:
    env = dict(os.environ)
    if args.secret_file is not None:
        env["LEARN_BRIDGE_HMAC_SECRET"] = args.secret_file.read_text(encoding="utf-8").strip()
    if args.profile_dir is not None:
        env["LEARN_BRIDGE_PROFILE_DIR"] = str(args.profile_dir)
    env["LEARN_BRIDGE_HOST"] = str(args.host)
    env["LEARN_BRIDGE_PORT"] = str(args.port)
    env["LEARN_BASE_URL"] = str(args.learn_base_url)
    return LearnBridgeSettings.from_env(env, repository_root=args.repository_root)


def _http_health(settings: LearnBridgeSettings) -> int:
    import urllib.error
    import urllib.request

    path = "/health"
    headers = signed_headers(secret=settings.secret, method="GET", path=path)
    request = urllib.request.Request(
        f"http://{settings.host}:{settings.port}{path}",
        headers=headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:  # noqa: S310
            body = response.read()
            response_headers = {key.casefold(): value for key, value in response.headers.items()}
            signature = response_headers.get("x-lifeagent-learn-signature", "")
            timestamp = int(response_headers.get("x-lifeagent-learn-timestamp", "0"))
            nonce = response_headers.get("x-lifeagent-learn-nonce", "")
            if not verify_bridge_response(
                secret=settings.secret,
                status_code=response.status,
                path=path,
                timestamp=timestamp,
                nonce=nonce,
                body=body,
                signature=signature,
            ):
                print("learn_bridge_health=bad_response_signature")
                return 1
            payload = json.loads(body.decode("utf-8"))
    except urllib.error.URLError:
        print("learn_bridge_health=unreachable")
        return 1
    print(f"learn_bridge_health={payload.get('status', 'unknown')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    settings = _settings_from_args(args)
    adapter = PlaywrightLearnBrowser(settings)
    if args.command == "login":
        status = adapter.login()
        print(f"learn_bridge_login={status}")
        return 0 if status == "ready" else 1
    if args.command == "health":
        return _http_health(settings)
    if args.command == "verify":
        try:
            current = datetime.now(UTC)
            snapshot = adapter.snapshot(
                {
                    "since": (current - timedelta(days=7)).isoformat(),
                    "until": (current + timedelta(days=24)).isoformat(),
                    "include_announcements": True,
                    "include_scheduled_items": True,
                }
            )
            ready = (
                snapshot["status"] == "ready"
                and bool(snapshot["courses"])
                and bool(snapshot["scheduled_items"])
                and bool(snapshot["announcements"])
            )
            if ready:
                print(
                    "learn_bridge_feasibility=ready "
                    f"courses={len(snapshot['courses'])} "
                    f"scheduled_items={len(snapshot['scheduled_items'])} "
                    f"announcements={len(snapshot['announcements'])}"
                )
                return 0
            print("learn_bridge_feasibility=login_required")
            return 1
        except Exception:
            print("learn_bridge_feasibility=login_required")
            return 1
        finally:
            adapter.close()
    service = LearnBridgeService(settings=settings, adapter=adapter)
    service.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LearnAnnouncementEvidence",
    "LearnBridgeRejectedError",
    "LearnBridgeRequestVerifier",
    "LearnBridgeService",
    "LearnBridgeSettings",
    "LearnBrowserAdapter",
    "LearnCourse",
    "LearnScheduledItem",
    "LearnSnapshot",
    "NonceStore",
    "PlaywrightLearnBrowser",
    "SnapshotRequest",
    "build_http_server",
    "clean_text",
    "default_profile_dir",
    "ensure_private_profile_dir",
    "is_loopback_host",
    "main",
    "sign_bridge_message",
    "sign_bridge_response",
    "signed_headers",
    "verify_bridge_response",
]
