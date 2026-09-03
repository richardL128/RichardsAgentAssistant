"""Discord adapter restricted to deterministic operational failure alerts."""

from __future__ import annotations

from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.core.errors import ErrorCode, authorization_error, permanent_error, transient_error
from app.health.checks import HealthState

DISCORD_API_BASE_URL = "https://discord.com/api/v10"


class FailureAlert(BaseModel):
    """Allowlisted facts used to construct an alert without private source text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: UUID
    run_id: UUID
    channel_id: str = Field(pattern=r"^[0-9]{5,24}$")
    component: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    state: HealthState
    error_code: ErrorCode
    attempt: int = Field(ge=0)
    attempt_limit: int = Field(ge=0)

    @field_validator("state")
    @classmethod
    def state_requires_attention(cls, value: HealthState) -> HealthState:
        if value is HealthState.HEALTHY:
            raise ValueError("normal health must not generate Discord alerts")
        return value


class DiscordDeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    permalink: str


class DiscordFailureAlertAdapter:
    """Send only failure/attention alerts to explicitly configured channels."""

    def __init__(
        self,
        *,
        token: SecretStr,
        allowed_channel_ids: set[str],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._client = client

    async def send(self, alert: FailureAlert) -> DiscordDeliveryReceipt:
        if alert.channel_id not in self._allowed_channel_ids:
            raise ValueError("Discord alert target is not allowlisted")

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            base_url=DISCORD_API_BASE_URL,
            timeout=httpx.Timeout(10.0),
        )
        content = (
            f"LifeAgent {alert.state.value}: {alert.component} "
            f"({alert.error_code.value}); retry {alert.attempt} of {alert.attempt_limit}; "
            f"run {alert.run_id}"
        )
        try:
            response = await client.post(
                f"/channels/{alert.channel_id}/messages",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
                json={
                    "content": content,
                    "nonce": str(alert.delivery_id),
                    "enforce_nonce": True,
                    "allowed_mentions": {"parse": []},
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {401, 403}:
                    raise authorization_error("Discord alert authorization is invalid") from None
                if exc.response.status_code == 429 or exc.response.status_code >= 500:
                    raise transient_error(
                        ErrorCode.CONNECTOR_TRANSIENT,
                        "Discord alert endpoint is temporarily unavailable",
                    ) from None
                raise permanent_error(
                    ErrorCode.INPUT_INVALID,
                    "Discord rejected the failure alert request",
                ) from None
            try:
                payload = response.json()
            except ValueError:
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid alert receipt",
                ) from None
            external_id = payload.get("id")
            if not isinstance(external_id, str) or not external_id.isdigit():
                raise transient_error(
                    ErrorCode.CONNECTOR_TRANSIENT,
                    "Discord returned an invalid alert receipt",
                )
            guild_id = payload.get("guild_id")
            server = guild_id if isinstance(guild_id, str) and guild_id.isdigit() else "@me"
            return DiscordDeliveryReceipt(
                external_id=external_id,
                permalink=f"https://discord.com/channels/{server}/{alert.channel_id}/{external_id}",
            )
        except httpx.TransportError:
            raise transient_error(
                ErrorCode.CONNECTOR_TRANSIENT,
                "Discord alert transport is unavailable",
            ) from None
        finally:
            if owns_client:
                await client.aclose()


__all__ = [
    "DiscordDeliveryReceipt",
    "DiscordFailureAlertAdapter",
    "FailureAlert",
]
