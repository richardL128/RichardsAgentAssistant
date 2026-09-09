"""Discord REST operations used by the native host wake daemon."""

from __future__ import annotations

import hashlib
import re
from typing import Literal

import httpx
from pydantic import SecretStr

HOST_WAKE_ACKNOWLEDGEMENT = (
    "I’m waking up LifeAgent and Qwen. Please give me a little time to respond."  # noqa: RUF001
)
HOST_COMMAND_ACKNOWLEDGEMENT = "I’m waking up LifeAgent to handle that command."  # noqa: RUF001

DiscordWakeFailure = Literal[
    "docker_timeout",
    "image_stale",
    "compose_unhealthy",
    "ollama_unavailable",
    "handoff_failed",
]

_DISCORD_ID = re.compile(r"^[0-9]{5,24}$")


def wake_ack_nonce(message_id: str) -> str:
    _require_id(message_id)
    return hashlib.sha256(f"lifeagent-wake-ack:{message_id}".encode()).hexdigest()[:25]


def safe_failure_content(code: DiscordWakeFailure) -> str:
    if code == "docker_timeout":
        return (
            "LifeAgent could not start Docker Desktop. Check Docker Desktop on the Mac "
            "and try again."
        )
    if code == "image_stale":
        return (
            "LifeAgent needs a local deployment refresh. Run the documented LifeAgent deploy "
            "command on the Mac, then try again."
        )
    if code == "compose_unhealthy":
        return (
            "LifeAgent started Docker but its services did not become healthy. Check the "
            "LifeAgent runtime status on the Mac."
        )
    if code == "ollama_unavailable":
        return (
            "Qwen is unavailable on this Mac. Check the LifeAgent Ollama LaunchAgent status "
            "command, then try again."
        )
    return (
        "LifeAgent woke up but could not safely queue this request. Please check runtime "
        "status and retry the mention."
    )


class DiscordWakeAckAdapter:
    """Send and edit the single wake acknowledgement through Discord REST."""

    def __init__(
        self,
        *,
        token: SecretStr,
        allowed_channel_id: str,
        base_url: str = "https://discord.com/api/v10",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        _require_id(allowed_channel_id)
        self._token = token
        self._allowed_channel_id = allowed_channel_id
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def send_acknowledgement(
        self,
        *,
        channel_id: str,
        root_message_id: str,
        content: str = HOST_WAKE_ACKNOWLEDGEMENT,
    ) -> str:
        self._require_allowed(channel_id)
        if not content or len(content) > 2_000:
            raise ValueError("Discord acknowledgement content is invalid")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(base_url=self._base_url, timeout=10.0)
        try:
            response = await client.post(
                f"/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
                json={
                    "content": content,
                    "nonce": wake_ack_nonce(root_message_id),
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                },
            )
            response.raise_for_status()
            payload = response.json()
            message_id = payload.get("id")
            if not isinstance(message_id, str) or _DISCORD_ID.fullmatch(message_id) is None:
                raise ValueError("Discord acknowledgement receipt is invalid")
            return message_id
        finally:
            if owns_client:
                await client.aclose()

    async def edit_acknowledgement(
        self,
        *,
        channel_id: str,
        acknowledgement_message_id: str,
        content: str,
    ) -> None:
        self._require_allowed(channel_id)
        _require_id(acknowledgement_message_id)
        if not content or len(content) > 2_000:
            raise ValueError("Discord acknowledgement edit content is invalid")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(base_url=self._base_url, timeout=10.0)
        try:
            response = await client.patch(
                f"/channels/{channel_id}/messages/{acknowledgement_message_id}",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
                json={"content": content, "allowed_mentions": {"parse": []}, "components": []},
            )
            response.raise_for_status()
        finally:
            if owns_client:
                await client.aclose()

    def _require_allowed(self, channel_id: str) -> None:
        _require_id(channel_id)
        if channel_id != self._allowed_channel_id:
            raise ValueError("Discord wake channel is not allowlisted")


def _require_id(value: str) -> None:
    if _DISCORD_ID.fullmatch(value) is None:
        raise ValueError("Discord ID is invalid")


__all__ = [
    "HOST_COMMAND_ACKNOWLEDGEMENT",
    "HOST_WAKE_ACKNOWLEDGEMENT",
    "DiscordWakeAckAdapter",
    "DiscordWakeFailure",
    "safe_failure_content",
    "wake_ack_nonce",
]
