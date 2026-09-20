"""Docker-side client for the host-local authenticated LEARN browser bridge."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from app.agents.learn.contracts import (
    LearnAnnouncementEvidence,
    LearnCourse,
    LearnScheduledItem,
)

MAX_LEARN_BRIDGE_RESPONSE_BYTES: Final[int] = 2_000_000
MAX_LEARN_BRIDGE_COURSES: Final[int] = 200
MAX_LEARN_BRIDGE_SCHEDULED_ITEMS: Final[int] = 1_000
MAX_LEARN_BRIDGE_ANNOUNCEMENTS: Final[int] = 500
LEARN_BRIDGE_SIGNATURE_VERSION: Final[str] = "v1"
_MAX_CLOCK_SKEW_SECONDS: Final[int] = 300
_NONCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]{16,128}$")
_SAFE_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "127.0.0.1",
        "::1",
        "localhost",
        "host.docker.internal",
    }
)
_FORBIDDEN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "cookie",
        "cookies",
        "set-cookie",
        "localstorage",
        "sessionstorage",
        "storagestate",
        "headers",
        "password",
    }
)


class LearnBridgeHealthStatus(StrEnum):
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    BROWSER_UNAVAILABLE = "browser_unavailable"


class LearnBridgeError(RuntimeError):
    """Raised when the host LEARN bridge cannot provide a safe snapshot."""


class LearnBridgeHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    status: LearnBridgeHealthStatus
    checked_at: datetime | None = None

    @field_validator("checked_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("LEARN bridge health timestamps must be timezone-aware")
        return value.astimezone(UTC)


class LearnBridgeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: LearnBridgeHealthStatus = LearnBridgeHealthStatus.READY
    courses: tuple[LearnCourse, ...] = Field(default=(), max_length=MAX_LEARN_BRIDGE_COURSES)
    scheduled_items: tuple[LearnScheduledItem, ...] = Field(
        default=(),
        max_length=MAX_LEARN_BRIDGE_SCHEDULED_ITEMS,
    )
    announcements: tuple[LearnAnnouncementEvidence, ...] = Field(
        default=(),
        max_length=MAX_LEARN_BRIDGE_ANNOUNCEMENTS,
    )
    generated_at: datetime

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("LEARN snapshot timestamps must be timezone-aware")
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _SignedRequest:
    nonce: str
    timestamp: str
    body: bytes
    signature: str


class LearnBridgeConnector:
    """Authenticated loopback-only client for read-only LEARN bridge snapshots."""

    def __init__(
        self,
        *,
        base_url: str,
        hmac_secret: SecretStr | str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = MAX_LEARN_BRIDGE_RESPONSE_BYTES,
        max_clock_skew_seconds: int = 60,
        max_courses: int = 100,
        max_scheduled_items: int = 500,
        max_announcements: int = 250,
        clock: Callable[[], datetime] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("LEARN bridge timeout must be finite and between 0 and 120 seconds")
        if max_response_bytes <= 0 or max_response_bytes > MAX_LEARN_BRIDGE_RESPONSE_BYTES:
            raise ValueError("LEARN bridge response limit is invalid")
        if max_clock_skew_seconds <= 0 or max_clock_skew_seconds > _MAX_CLOCK_SKEW_SECONDS:
            raise ValueError("LEARN bridge clock skew limit is invalid")
        if not 1 <= max_courses <= MAX_LEARN_BRIDGE_COURSES:
            raise ValueError("LEARN bridge course limit is invalid")
        if not 1 <= max_scheduled_items <= MAX_LEARN_BRIDGE_SCHEDULED_ITEMS:
            raise ValueError("LEARN bridge scheduled-item limit is invalid")
        if not 1 <= max_announcements <= MAX_LEARN_BRIDGE_ANNOUNCEMENTS:
            raise ValueError("LEARN bridge announcement limit is invalid")
        if len(_secret_value(hmac_secret)) < 32:
            raise ValueError("LEARN bridge HMAC secret must be at least 32 characters")
        self._base_url = _validate_base_url(base_url)
        self._secret = hmac_secret
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_clock_skew_seconds = max_clock_skew_seconds
        self._max_courses = max_courses
        self._max_scheduled_items = max_scheduled_items
        self._max_announcements = max_announcements
        self._clock = clock or (lambda: datetime.now(UTC))
        self._nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(24))
        self._seen_response_nonces: set[str] = set()

    async def health(self) -> LearnBridgeHealth:
        response_body = await self._send_signed("GET", "/health", b"")
        try:
            _reject_forbidden_keys(json.loads(response_body))
            return LearnBridgeHealth.model_validate_json(response_body)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            raise LearnBridgeError("LEARN bridge health payload was invalid") from None

    async def snapshot(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        course_ids: Sequence[str] = (),
        include_announcements: bool = True,
        include_scheduled_items: bool = True,
    ) -> LearnBridgeSnapshot:
        if start_at.tzinfo is None or start_at.utcoffset() is None:
            raise ValueError("LEARN snapshot start_at must be timezone-aware")
        if end_at.tzinfo is None or end_at.utcoffset() is None:
            raise ValueError("LEARN snapshot end_at must be timezone-aware")
        if end_at < start_at:
            raise ValueError("LEARN snapshot end_at must be after start_at")
        if len(course_ids) > 50:
            raise ValueError("LEARN snapshot course filter is too large")
        payload = {
            "since": start_at.astimezone(UTC).isoformat(),
            "until": end_at.astimezone(UTC).isoformat(),
            "course_ids": list(dict.fromkeys(course_ids)),
            "include_announcements": include_announcements,
            "include_scheduled_items": include_scheduled_items,
            "course_limit": self._max_courses,
            "item_limit": self._max_scheduled_items,
            "announcement_limit": self._max_announcements,
        }
        body = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()
        response_body = await self._send_signed("POST", "/v1/snapshot", body)
        try:
            parsed = json.loads(response_body)
            _reject_forbidden_keys(parsed)
            snapshot = LearnBridgeSnapshot.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            raise LearnBridgeError("LEARN bridge snapshot payload was invalid") from None
        return snapshot

    async def _send_signed(self, method: str, path: str, body: bytes) -> bytes:
        signed = self._sign(method, path, body)
        headers = {
            "content-type": "application/json",
            "x-lifeagent-learn-timestamp": signed.timestamp,
            "x-lifeagent-learn-nonce": signed.nonce,
            "x-lifeagent-learn-signature": signed.signature,
        }
        client = self._client
        if client is None:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as owned_client:
                return await self._stream_signed_response(
                    owned_client,
                    method=method,
                    path=path,
                    body=signed.body,
                    headers=headers,
                )
        return await self._stream_signed_response(
            client,
            method=method,
            path=path,
            body=signed.body,
            headers=headers,
        )

    async def _stream_signed_response(
        self,
        client: httpx.AsyncClient,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> bytes:
        async with client.stream(
                method,
                str(self._base_url.join(path)),
                content=body,
                headers=headers,
                timeout=self._timeout_seconds,
        ) as response:
            body_bytes = await _bounded_response_body(response, self._max_response_bytes)
            self._verify_response(path, response.status_code, response.headers, body_bytes)
            if response.status_code >= 400:
                raise LearnBridgeError("LEARN bridge request failed")
        return body_bytes

    def _sign(self, method: str, path: str, body: bytes) -> _SignedRequest:
        nonce = self._nonce_factory()
        timestamp = str(int(self._clock().timestamp()))
        signature = _signature(
            secret=_secret_value(self._secret),
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        return _SignedRequest(nonce=nonce, timestamp=timestamp, body=body, signature=signature)

    def _verify_response(
        self,
        path: str,
        status_code: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        signature = _header(headers, "x-lifeagent-learn-signature")
        timestamp = _header(headers, "x-lifeagent-learn-timestamp")
        nonce = _header(headers, "x-lifeagent-learn-nonce")
        if not signature or not timestamp or not nonce:
            raise LearnBridgeError("LEARN bridge response signature was missing")
        if _NONCE_PATTERN.fullmatch(nonce) is None:
            raise LearnBridgeError("LEARN bridge response nonce was invalid")
        if nonce in self._seen_response_nonces:
            raise LearnBridgeError("LEARN bridge response nonce was replayed")
        try:
            response_time = datetime.fromtimestamp(int(timestamp), tz=UTC)
        except (TypeError, ValueError, OSError):
            raise LearnBridgeError("LEARN bridge response timestamp was invalid") from None
        skew = abs((self._clock().astimezone(UTC) - response_time).total_seconds())
        if skew > self._max_clock_skew_seconds:
            raise LearnBridgeError("LEARN bridge response timestamp was stale")
        expected = _response_signature(
            secret=_secret_value(self._secret),
            status_code=status_code,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        if not hmac.compare_digest(signature, expected):
            raise LearnBridgeError("LEARN bridge response signature was invalid")
        self._seen_response_nonces.add(nonce)


async def _bounded_response_body(response: httpx.Response, limit: int) -> bytes:
    raw_length = response.headers.get("content-length")
    if raw_length is not None:
        try:
            declared_length = int(raw_length)
        except ValueError:
            raise LearnBridgeError("LEARN bridge response length was invalid") from None
        if declared_length < 0 or declared_length > limit:
            raise LearnBridgeError("LEARN bridge response exceeded the byte limit")
    total = 0
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise LearnBridgeError("LEARN bridge response exceeded the byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _signature(
    *,
    secret: str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    canonical = b"\n".join(
        (
            method.upper().encode("ascii"),
            path.encode("ascii"),
            timestamp.encode("ascii"),
            nonce.encode("ascii"),
            hashlib.sha256(body).hexdigest().encode("ascii"),
        )
    )
    digest = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _response_signature(
    *,
    secret: str,
    status_code: int,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    canonical = b"\n".join(
        (
            b"RESPONSE",
            str(status_code).encode("ascii"),
            path.encode("utf-8"),
            timestamp.encode("ascii"),
            nonce.encode("utf-8"),
            hashlib.sha256(body).hexdigest().encode("ascii"),
        )
    )
    digest = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _validate_base_url(value: str) -> httpx.URL:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.username is not None or parsed.password is not None:
        raise ValueError("LEARN bridge URL must be plain HTTP without credentials")
    host = parsed.hostname
    if host not in _SAFE_HOSTS:
        raise ValueError("LEARN bridge URL must target a loopback or host-local address")
    if parsed.query or parsed.fragment:
        raise ValueError("LEARN bridge URL must not contain query or fragment components")
    return httpx.URL(value.rstrip("/") + "/")


def _secret_value(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _reject_forbidden_keys(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key, child in mapping.items():
            if (
                isinstance(key, str)
                and key.replace("_", "").replace("-", "").casefold() in _FORBIDDEN_KEYS
            ):
                raise ValueError(f"LEARN bridge payload contained forbidden key at {path}")
            _reject_forbidden_keys(child, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = cast(Sequence[object], value)
        for index, child in enumerate(items):
            _reject_forbidden_keys(child, path=f"{path}[{index}]")


__all__ = [
    "LEARN_BRIDGE_SIGNATURE_VERSION",
    "LearnBridgeConnector",
    "LearnBridgeError",
    "LearnBridgeHealth",
    "LearnBridgeHealthStatus",
    "LearnBridgeSnapshot",
]
