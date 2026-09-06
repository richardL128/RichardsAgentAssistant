"""Local-friendly Discord Gateway listener for academic clarification buttons."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

DiscordClarificationAction = Literal["quiz", "assignment", "ignore"]
DiscordInteractionStatus = Literal[
    "handled",
    "ignored",
    "duplicate",
    "unauthorized",
    "invalid",
    "failed",
]

_CUSTOM_ID_PATTERN = re.compile(
    r"^academic_clarify:"
    r"(?P<clarification_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}):"
    r"(?P<action>quiz|assignment|ignore)$"
)
_DISCORD_MESSAGE_COMPONENT_TYPE = 3
_DISCORD_INTERACTION_CALLBACK_DEFERRED_UPDATE = 6


class DiscordClarificationInteraction(BaseModel):
    """Sanitized interaction facts passed to academic clarification handling."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interaction_id: str = Field(pattern=r"^[0-9]{5,24}$")
    channel_id: str = Field(pattern=r"^[0-9]{5,24}$")
    user_id: str = Field(pattern=r"^[0-9]{5,24}$")
    clarification_id: UUID
    action: DiscordClarificationAction


class DiscordClarificationCallbackResult(BaseModel):
    """Bounded callback result safe to log or persist by callers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DiscordInteractionStatus


class DiscordClarificationHandler(Protocol):
    async def __call__(
        self,
        interaction: DiscordClarificationInteraction,
    ) -> DiscordClarificationCallbackResult: ...


class DiscordGatewayHttpClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response: ...

    async def post(
        self,
        url: str,
        *,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response: ...


class DiscordGatewayWebSocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, data: str) -> None: ...

    async def close(self) -> None: ...


DiscordGatewayConnect = Callable[[str], Awaitable[DiscordGatewayWebSocket]]
DiscordGatewaySleep = Callable[[float], Awaitable[None]]


class DiscordGatewayReconnectError(RuntimeError):
    """Internal signal for a safe reconnect."""


DiscordGatewayReconnect = DiscordGatewayReconnectError


class DiscordGatewayListener:
    """Connect to Discord Gateway and dispatch academic clarification interactions."""

    def __init__(
        self,
        *,
        token: SecretStr,
        api_base_url: str,
        allowed_channel_ids: set[str],
        authorized_user_ids: set[str],
        handler: DiscordClarificationHandler,
        http_client: DiscordGatewayHttpClient | None = None,
        websocket_connect: DiscordGatewayConnect | None = None,
        sleep: DiscordGatewaySleep = asyncio.sleep,
        max_seen_interactions: int = 1_024,
    ) -> None:
        self._token = token
        self._api_base_url = api_base_url.rstrip("/")
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._authorized_user_ids = frozenset(authorized_user_ids)
        self._handler = handler
        self._http_client = http_client
        self._websocket_connect = websocket_connect or _default_websocket_connect
        self._sleep = sleep
        self._max_seen_interactions = max_seen_interactions
        self._seen_interactions: list[str] = []
        self._sequence: int | None = None
        self._session_id: str | None = None

    async def run_forever(self, *, max_attempts: int | None = None) -> None:
        attempts = 0
        while max_attempts is None or attempts < max_attempts:
            attempts += 1
            try:
                await self.run_once()
            except DiscordGatewayReconnectError:
                await self._sleep(1.0)
            except (ConnectionError, TimeoutError, httpx.HTTPError, ValueError):
                await self._sleep(1.0)

    async def run_once(self) -> None:
        gateway_url = await self._gateway_url()
        websocket = await self._websocket_connect(gateway_url)
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            while True:
                payload = _decode_gateway_payload(await websocket.recv())
                op = _int_value(payload.get("op"))
                if op == 10:
                    interval = _heartbeat_interval_seconds(payload)
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket, interval))
                    await self._send_identify_or_resume(websocket)
                elif op == 0:
                    self._sequence = _int_value(payload.get("s"))
                    await self.handle_gateway_payload(payload)
                    self._capture_ready_session(payload)
                elif op == 1:
                    await self._send_heartbeat(websocket)
                elif op in {7, 9}:
                    if op == 9 and payload.get("d") is False:
                        self._session_id = None
                        self._sequence = None
                    raise DiscordGatewayReconnectError()
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            await websocket.close()

    async def handle_gateway_payload(
        self,
        payload: Mapping[str, object],
    ) -> DiscordInteractionStatus:
        if payload.get("t") != "INTERACTION_CREATE":
            return "ignored"
        data = _mapping_value(payload.get("d"))
        if data is None or _int_value(data.get("type")) != _DISCORD_MESSAGE_COMPONENT_TYPE:
            return "ignored"

        interaction_id = _str_value(data.get("id"))
        token = _str_value(data.get("token"))
        if interaction_id is None or token is None:
            return "invalid"
        await self._acknowledge_interaction(interaction_id, token)

        parsed = normalize_clarification_interaction(
            data,
            allowed_channel_ids=self._allowed_channel_ids,
            authorized_user_ids=self._authorized_user_ids,
        )
        if parsed is None:
            return _rejection_status(data, self._allowed_channel_ids, self._authorized_user_ids)
        if parsed.interaction_id in self._seen_interactions:
            return "duplicate"
        self._remember_interaction(parsed.interaction_id)
        try:
            result = await self._handler(parsed)
        except Exception:
            return "failed"
        return result.status

    async def _gateway_url(self) -> str:
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        try:
            response = await client.get(
                f"{self._api_base_url}/gateway/bot",
                headers={"Authorization": f"Bot {self._token.get_secret_value()}"},
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns_client:
                await cast(httpx.AsyncClient, client).aclose()
        url = payload.get("url")
        if not isinstance(url, str) or not url.startswith(("ws://", "wss://")):
            raise ValueError("Discord gateway URL response was invalid")
        return f"{url.rstrip('/')}/?v=10&encoding=json"

    async def _send_identify_or_resume(self, websocket: DiscordGatewayWebSocket) -> None:
        token = self._token.get_secret_value()
        if self._session_id is not None and self._sequence is not None:
            await websocket.send(
                json.dumps(
                    {
                        "op": 6,
                        "d": {
                            "token": token,
                            "session_id": self._session_id,
                            "seq": self._sequence,
                        },
                    }
                )
            )
            return
        await websocket.send(
            json.dumps(
                {
                    "op": 2,
                    "d": {
                        "token": token,
                        "intents": 0,
                        "properties": {
                            "os": "linux",
                            "browser": "lifeagent",
                            "device": "lifeagent",
                        },
                    },
                }
            )
        )

    async def _heartbeat_loop(
        self,
        websocket: DiscordGatewayWebSocket,
        interval_seconds: float,
    ) -> None:
        while True:
            await self._sleep(interval_seconds)
            await self._send_heartbeat(websocket)

    async def _send_heartbeat(self, websocket: DiscordGatewayWebSocket) -> None:
        await websocket.send(json.dumps({"op": 1, "d": self._sequence}))

    async def _acknowledge_interaction(self, interaction_id: str, token: str) -> None:
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            base_url=self._api_base_url,
            timeout=httpx.Timeout(10.0),
        )
        try:
            response = await client.post(
                f"/interactions/{interaction_id}/{token}/callback",
                json={"type": _DISCORD_INTERACTION_CALLBACK_DEFERRED_UPDATE},
            )
            response.raise_for_status()
        finally:
            if owns_client:
                await cast(httpx.AsyncClient, client).aclose()

    def _capture_ready_session(self, payload: Mapping[str, object]) -> None:
        if payload.get("t") != "READY":
            return
        data = _mapping_value(payload.get("d"))
        if data is None:
            return
        session_id = _str_value(data.get("session_id"))
        if session_id is not None:
            self._session_id = session_id

    def _remember_interaction(self, interaction_id: str) -> None:
        self._seen_interactions.append(interaction_id)
        if len(self._seen_interactions) > self._max_seen_interactions:
            del self._seen_interactions[0]


def parse_clarification_custom_id(
    custom_id: str,
) -> tuple[UUID, DiscordClarificationAction] | None:
    match = _CUSTOM_ID_PATTERN.fullmatch(custom_id)
    if match is None:
        return None
    return (
        UUID(match.group("clarification_id")),
        cast(DiscordClarificationAction, match.group("action")),
    )


def normalize_clarification_interaction(
    data: Mapping[str, object],
    *,
    allowed_channel_ids: frozenset[str] | set[str],
    authorized_user_ids: frozenset[str] | set[str],
) -> DiscordClarificationInteraction | None:
    custom_id = _custom_id(data)
    if custom_id is None:
        return None
    parsed = parse_clarification_custom_id(custom_id)
    if parsed is None:
        return None
    channel_id = _str_value(data.get("channel_id"))
    user_id = _interaction_user_id(data)
    interaction_id = _str_value(data.get("id"))
    if interaction_id is None or channel_id is None or user_id is None:
        return None
    if channel_id not in allowed_channel_ids or user_id not in authorized_user_ids:
        return None
    clarification_id, action = parsed
    return DiscordClarificationInteraction(
        interaction_id=interaction_id,
        channel_id=channel_id,
        user_id=user_id,
        clarification_id=clarification_id,
        action=action,
    )


async def _default_websocket_connect(url: str) -> DiscordGatewayWebSocket:
    websockets: Any = importlib.import_module("websockets")
    return cast(DiscordGatewayWebSocket, await websockets.connect(url))


def _decode_gateway_payload(message: str | bytes) -> Mapping[str, object]:
    loaded = json.loads(message.decode() if isinstance(message, bytes) else message)
    if not isinstance(loaded, dict):
        raise ValueError("Discord gateway payload was invalid")
    return cast(Mapping[str, object], loaded)


def _heartbeat_interval_seconds(payload: Mapping[str, object]) -> float:
    data = _mapping_value(payload.get("d"))
    if data is None:
        raise ValueError("Discord gateway HELLO payload was invalid")
    interval_ms = _int_value(data.get("heartbeat_interval"))
    if interval_ms is None or interval_ms <= 0:
        raise ValueError("Discord gateway heartbeat interval was invalid")
    return interval_ms / 1_000


def _rejection_status(
    data: Mapping[str, object],
    allowed_channel_ids: frozenset[str],
    authorized_user_ids: frozenset[str],
) -> DiscordInteractionStatus:
    channel_id = _str_value(data.get("channel_id"))
    user_id = _interaction_user_id(data)
    custom_id = _custom_id(data)
    if (
        custom_id is not None
        and custom_id.startswith("academic_clarify:")
        and (channel_id not in allowed_channel_ids or user_id not in authorized_user_ids)
    ):
        return "unauthorized"
    return "invalid"


def _custom_id(data: Mapping[str, object]) -> str | None:
    interaction_data = _mapping_value(data.get("data"))
    if interaction_data is None:
        return None
    return _str_value(interaction_data.get("custom_id"))


def _interaction_user_id(data: Mapping[str, object]) -> str | None:
    member = _mapping_value(data.get("member"))
    if member is not None:
        member_user = _mapping_value(member.get("user"))
        if member_user is not None:
            return _str_value(member_user.get("id"))
    user = _mapping_value(data.get("user"))
    if user is not None:
        return _str_value(user.get("id"))
    return None


def _mapping_value(value: object) -> Mapping[str, object] | None:
    if isinstance(value, dict):
        return cast(Mapping[str, object], value)
    return None


def _str_value(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_value(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None


__all__ = [
    "DiscordClarificationAction",
    "DiscordClarificationCallbackResult",
    "DiscordClarificationHandler",
    "DiscordClarificationInteraction",
    "DiscordGatewayConnect",
    "DiscordGatewayHttpClient",
    "DiscordGatewayListener",
    "DiscordGatewayReconnect",
    "DiscordGatewayReconnectError",
    "DiscordGatewaySleep",
    "DiscordGatewayWebSocket",
    "DiscordInteractionStatus",
    "normalize_clarification_interaction",
    "parse_clarification_custom_id",
]
