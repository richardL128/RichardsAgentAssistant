"""Typed HMAC handoff contract between the host daemon and localhost API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

_DISCORD_ID = r"^[0-9]{5,24}$"
_NONCE = r"^[0-9a-f]{32,64}$"
_MAX_BODY_BYTES = 4_096


class DiscordHostHandoffEvent(BaseModel):
    """Reference-only handoff body; no raw Discord content is permitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["discord-academic-message-v1"] = "discord-academic-message-v1"
    message_id: str = Field(pattern=_DISCORD_ID)
    channel_id: str = Field(pattern=_DISCORD_ID)
    author_id: str = Field(pattern=_DISCORD_ID)
    event_timestamp: datetime
    acknowledgement_message_id: str | None = Field(default=None, pattern=_DISCORD_ID)
    handoff_timestamp: datetime
    nonce: str = Field(pattern=_NONCE)

    @field_validator("event_timestamp", "handoff_timestamp")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("handoff timestamps must be timezone-aware")
        return value.astimezone(UTC)


class DiscordHostInteractionHandoffEvent(BaseModel):
    """Reference-only component handoff; the ephemeral token is never included."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["discord-academic-interaction-v1"] = "discord-academic-interaction-v1"
    interaction_id: str = Field(pattern=_DISCORD_ID)
    channel_id: str = Field(pattern=_DISCORD_ID)
    user_id: str = Field(pattern=_DISCORD_ID)
    clarification_id: UUID
    action: Literal["quiz", "assignment", "tutorial", "lab", "studying_block", "ignore"]
    event_timestamp: datetime
    handoff_timestamp: datetime
    nonce: str = Field(pattern=_NONCE)

    @field_validator("event_timestamp", "handoff_timestamp")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("handoff timestamps must be timezone-aware")
        return value.astimezone(UTC)


type DiscordHostHandoff = DiscordHostHandoffEvent | DiscordHostInteractionHandoffEvent


class DiscordHostHandoffAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "duplicate"]


class HandoffRejectedError(RuntimeError):
    """Safe host-side handoff failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def handoff_nonce(message_id: str) -> str:
    if re.fullmatch(_DISCORD_ID, message_id) is None:
        raise ValueError("message_id is invalid")
    return hashlib.sha256(f"lifeagent-host-handoff:{message_id}".encode()).hexdigest()


def canonical_handoff_body(event: DiscordHostHandoff) -> bytes:
    body = json.dumps(
        event.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > _MAX_BODY_BYTES:
        raise ValueError("handoff body is too large")
    return body


def sign_handoff_body(body: bytes, secret: SecretStr) -> str:
    key = secret.get_secret_value().encode("utf-8")
    digest = hmac.new(key, body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_handoff_signature(body: bytes, signature: str, secret: SecretStr) -> bool:
    if len(body) > _MAX_BODY_BYTES:
        return False
    expected = sign_handoff_body(body, secret)
    return hmac.compare_digest(expected, signature)


class DiscordHostHandoffClient:
    """Submit signed reference handoffs to a loopback-only backend endpoint."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        secret: SecretStr,
        timeout_seconds: int = 10,
        max_attempts: int = 3,
        client: httpx.AsyncClient | None = None,
        retry_sleep_seconds: float = 0.1,
    ) -> None:
        if not endpoint_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("handoff endpoint must be localhost-only")
        if timeout_seconds <= 0 or max_attempts <= 0:
            raise ValueError("handoff timeouts and attempts must be positive")
        self._endpoint_url = endpoint_url
        self._secret = secret
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._client = client
        self._retry_sleep_seconds = retry_sleep_seconds

    async def submit(self, event: DiscordHostHandoff) -> DiscordHostHandoffAccepted:
        body = canonical_handoff_body(event)
        signature = sign_handoff_body(body, self._secret)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            last_error: HandoffRejectedError | None = None
            for attempt in range(1, self._max_attempts + 1):
                try:
                    response = await client.post(
                        self._endpoint_url,
                        content=body,
                        headers={
                            "content-type": "application/json",
                            "x-lifeagent-handoff-signature": signature,
                        },
                    )
                except httpx.HTTPError as exc:
                    last_error = HandoffRejectedError("handoff_transport")
                    if attempt >= self._max_attempts:
                        raise last_error from exc
                    await asyncio.sleep(self._retry_sleep_seconds)
                    continue
                if response.status_code == 202:
                    return DiscordHostHandoffAccepted(status="accepted")
                if response.status_code in {200, 208}:
                    return DiscordHostHandoffAccepted(status="duplicate")
                if response.status_code == 401:
                    raise HandoffRejectedError("handoff_auth")
                if response.status_code in {408, 429} or response.status_code >= 500:
                    last_error = HandoffRejectedError("handoff_transient")
                    if attempt >= self._max_attempts:
                        raise last_error
                    await asyncio.sleep(self._retry_sleep_seconds)
                    continue
                raise HandoffRejectedError("handoff_rejected")
            raise last_error or HandoffRejectedError("handoff_rejected")
        finally:
            if owns_client:
                await client.aclose()


__all__ = [
    "DiscordHostHandoff",
    "DiscordHostHandoffAccepted",
    "DiscordHostHandoffClient",
    "DiscordHostHandoffEvent",
    "DiscordHostInteractionHandoffEvent",
    "HandoffRejectedError",
    "canonical_handoff_body",
    "handoff_nonce",
    "sign_handoff_body",
    "verify_handoff_signature",
]
