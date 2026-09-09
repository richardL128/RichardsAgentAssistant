"""Local-friendly Discord Gateway listener for academic Discord events."""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

DiscordClarificationAction = Literal[
    "quiz",
    "assignment",
    "tutorial",
    "lab",
    "studying_block",
    "ignore",
]
DiscordInteractionStatus = Literal[
    "handled",
    "queued",
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
    r"(?P<action>quiz|assignment|tutorial|lab|studying_block|ignore)$"
)
_DISCORD_ID_PATTERN = re.compile(r"^[0-9]{5,24}$")
_DISCORD_MESSAGE_COMPONENT_TYPE = 3
_DISCORD_INTERACTION_CALLBACK_CHANNEL_MESSAGE = 4
_DISCORD_INTERACTION_CALLBACK_UPDATE_MESSAGE = 7
_DISCORD_INTENT_GUILD_MESSAGES = 1 << 9
_DISCORD_INTENT_MESSAGE_CONTENT = 1 << 15
_DISCORD_MESSAGE_CONTENT_INTENTS = _DISCORD_INTENT_GUILD_MESSAGES | _DISCORD_INTENT_MESSAGE_CONTENT
_DISCORD_MESSAGE_CONTENT_LIMIT = 2_000
_DISCORD_INTENT_CLOSE_CODES = {4013, 4014}


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


class DiscordAcademicMessageCreate(BaseModel):
    """Sanitized authorized Discord message facts for academic check-in handling."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(pattern=r"^[0-9]{5,24}$")
    channel_id: str = Field(pattern=r"^[0-9]{5,24}$")
    author_id: str = Field(pattern=r"^[0-9]{5,24}$")
    timestamp: datetime
    content: SecretStr = Field(repr=False)
    mentioned_user_ids: tuple[str, ...] = Field(default=(), max_length=20)
    progress_message_id: str | None = Field(default=None, pattern=r"^[0-9]{5,24}$")

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Discord message timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("content")
    @classmethod
    def content_is_bounded(cls, value: SecretStr) -> SecretStr:
        content = value.get_secret_value()
        if not content or len(content) > _DISCORD_MESSAGE_CONTENT_LIMIT:
            raise ValueError("Discord message content must be present and bounded")
        return value

    def has_verified_mention(self, application_id: str) -> bool:
        """Require both Discord mention metadata and canonical mention text."""

        if _DISCORD_ID_PATTERN.fullmatch(application_id) is None:
            return False
        return (
            application_id in self.mentioned_user_ids
            and re.search(
                rf"<@!?{re.escape(application_id)}>",
                self.content.get_secret_value(),
            )
            is not None
        )


class DiscordMessageCallbackResult(BaseModel):
    """Bounded callback result safe to log or persist by callers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DiscordInteractionStatus


class DiscordClarificationEnqueuer(Protocol):
    async def __call__(
        self,
        interaction: DiscordClarificationInteraction,
    ) -> DiscordClarificationCallbackResult: ...


class DiscordMessageHandler(Protocol):
    async def __call__(
        self,
        message: DiscordAcademicMessageCreate,
    ) -> DiscordMessageCallbackResult: ...


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

    async def patch(
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


class DiscordGatewayConfigurationError(RuntimeError):
    """Safe terminal diagnostic for a Gateway configuration mismatch."""

    def __init__(self, diagnostic: str) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic)


class DiscordGatewayListener:
    """Connect to Discord Gateway and dispatch academic clarification interactions."""

    def __init__(
        self,
        *,
        token: SecretStr,
        api_base_url: str,
        allowed_channel_ids: set[str],
        authorized_user_ids: set[str],
        clarification_enqueuer: DiscordClarificationEnqueuer,
        message_content_enabled: bool = False,
        message_handler: DiscordMessageHandler | None = None,
        http_client: DiscordGatewayHttpClient | None = None,
        websocket_connect: DiscordGatewayConnect | None = None,
        sleep: DiscordGatewaySleep = asyncio.sleep,
        max_seen_interactions: int = 1_024,
        max_seen_messages: int = 1_024,
    ) -> None:
        self._token = token
        self._api_base_url = api_base_url.rstrip("/")
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._authorized_user_ids = frozenset(authorized_user_ids)
        self._clarification_enqueuer = clarification_enqueuer
        self._message_handler = message_handler
        self._message_content_enabled = message_content_enabled
        self._http_client = http_client
        self._websocket_connect = websocket_connect or _default_websocket_connect
        self._sleep = sleep
        self._max_seen_interactions = max_seen_interactions
        self._max_seen_messages = max_seen_messages
        self._seen_interactions: list[str] = []
        self._seen_messages: list[str] = []
        self._inflight_messages: set[str] = set()
        self._message_tasks: set[asyncio.Task[DiscordMessageCallbackResult]] = set()
        self._sequence: int | None = None
        self._session_id: str | None = None
        self._last_diagnostic: str | None = None

    @property
    def identity_intents(self) -> int:
        """Return the exact Gateway intents this listener will request."""

        if not self._message_content_enabled:
            return 0
        return _DISCORD_MESSAGE_CONTENT_INTENTS

    @property
    def last_diagnostic(self) -> str | None:
        """Return the latest non-secret Gateway diagnostic, if any."""

        return self._last_diagnostic

    async def drain_message_tasks(self) -> None:
        """Wait for scheduled message callbacks and consume their bounded results."""

        if not self._message_tasks:
            return
        await asyncio.gather(*tuple(self._message_tasks), return_exceptions=True)

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
                try:
                    raw_message = await websocket.recv()
                except Exception as exc:
                    diagnostic = self._close_diagnostic(exc)
                    if diagnostic is not None:
                        self._last_diagnostic = diagnostic
                        raise DiscordGatewayConfigurationError(diagnostic) from None
                    raise
                payload = _decode_gateway_payload(raw_message)
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
        event_type = payload.get("t")
        if event_type == "MESSAGE_CREATE":
            return await self._handle_message_create(payload)
        if event_type != "INTERACTION_CREATE":
            return "ignored"
        data = _mapping_value(payload.get("d"))
        if data is None or _int_value(data.get("type")) != _DISCORD_MESSAGE_COMPONENT_TYPE:
            return "ignored"

        parsed_custom_id = parse_clarification_custom_id(_custom_id(data) or "")
        if parsed_custom_id is None:
            return "ignored"
        _, action = parsed_custom_id
        interaction_id = _str_value(data.get("id"))
        token = _str_value(data.get("token"))
        application_id = _str_value(data.get("application_id"))
        if (
            interaction_id is None
            or _DISCORD_ID_PATTERN.fullmatch(interaction_id) is None
            or token is None
            or not token
            or application_id is None
            or _DISCORD_ID_PATTERN.fullmatch(application_id) is None
        ):
            return "invalid"
        if interaction_id in self._seen_interactions:
            return "duplicate"

        parsed = normalize_clarification_interaction(
            data,
            allowed_channel_ids=self._allowed_channel_ids,
            authorized_user_ids=self._authorized_user_ids,
        )
        if parsed is None:
            status = _rejection_status(
                data,
                self._allowed_channel_ids,
                self._authorized_user_ids,
            )
            self._remember_interaction(interaction_id)
            return status

        try:
            result = await self._clarification_enqueuer(parsed)
        except Exception:
            result = DiscordClarificationCallbackResult(status="failed")

        try:
            if result.status == "failed":
                await self._acknowledge_interaction(
                    interaction_id,
                    token,
                    content=_interaction_confirmation_content(action, result.status),
                )
            else:
                await self._update_source_message_interaction(
                    interaction_id,
                    token,
                    content=_interaction_confirmation_content(action, result.status),
                )
        except httpx.HTTPError:
            self._remember_interaction(parsed.interaction_id)
            return "failed"
        self._remember_interaction(parsed.interaction_id)
        return result.status

    async def _handle_message_create(
        self,
        payload: Mapping[str, object],
    ) -> DiscordInteractionStatus:
        if not self._message_content_enabled or self._message_handler is None:
            return "ignored"
        data = _mapping_value(payload.get("d"))
        if data is None:
            return "invalid"
        parsed = normalize_academic_message(
            data,
            allowed_channel_ids=self._allowed_channel_ids,
            authorized_user_ids=self._authorized_user_ids,
        )
        if parsed is None:
            return _message_rejection_status(
                data,
                self._allowed_channel_ids,
                self._authorized_user_ids,
            )
        if parsed.message_id in self._seen_messages or parsed.message_id in self._inflight_messages:
            return "duplicate"
        self._inflight_messages.add(parsed.message_id)
        self._schedule_message(parsed)
        return "handled"

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
                        "intents": self.identity_intents,
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

    async def _acknowledge_interaction(
        self,
        interaction_id: str,
        token: str,
        *,
        content: str,
    ) -> None:
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            base_url=self._api_base_url,
            timeout=httpx.Timeout(10.0),
        )
        try:
            response = await client.post(
                f"/interactions/{interaction_id}/{token}/callback",
                json={
                    "type": _DISCORD_INTERACTION_CALLBACK_CHANNEL_MESSAGE,
                    "data": {
                        "content": content,
                        "allowed_mentions": {"parse": []},
                    },
                },
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

    def _remember_message(self, message_id: str) -> None:
        self._seen_messages.append(message_id)
        if len(self._seen_messages) > self._max_seen_messages:
            del self._seen_messages[0]

    def _close_diagnostic(self, exc: Exception) -> str | None:
        code = getattr(exc, "code", None)
        if code not in _DISCORD_INTENT_CLOSE_CODES:
            return None
        if self._message_content_enabled:
            return (
                "Discord Gateway rejected the configured message intents; enable the "
                "Message Content Intent in the Discord Developer Portal or disable "
                "academic free-text Discord check-ins."
            )
        return "Discord Gateway rejected the configured intents; check the Discord Gateway setup."

    def _schedule_message(self, message: DiscordAcademicMessageCreate) -> None:
        handler = self._message_handler
        if handler is None:
            return
        task = asyncio.create_task(
            self._run_message_handler(handler, message),
            name=f"discord-academic-message-{message.message_id}",
        )
        self._message_tasks.add(task)
        task.add_done_callback(
            lambda completed: self._message_task_done(message.message_id, completed)
        )

    def _message_task_done(
        self,
        message_id: str,
        task: asyncio.Task[DiscordMessageCallbackResult],
    ) -> None:
        self._message_tasks.discard(task)
        self._inflight_messages.discard(message_id)
        if task.cancelled():
            return
        try:
            result = task.result()
        except Exception:
            return
        if result.status in {"handled", "duplicate"}:
            self._remember_message(message_id)

    @staticmethod
    async def _run_message_handler(
        handler: DiscordMessageHandler,
        message: DiscordAcademicMessageCreate,
    ) -> DiscordMessageCallbackResult:
        try:
            return await handler(message)
        except Exception:
            return DiscordMessageCallbackResult(status="failed")

    async def _update_source_message_interaction(
        self,
        interaction_id: str,
        token: str,
        *,
        content: str,
    ) -> None:
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            base_url=self._api_base_url,
            timeout=httpx.Timeout(10.0),
        )
        try:
            response = await client.post(
                f"/interactions/{interaction_id}/{token}/callback",
                json={
                    "type": _DISCORD_INTERACTION_CALLBACK_UPDATE_MESSAGE,
                    "data": {
                        "content": content,
                        "allowed_mentions": {"parse": []},
                        "components": [],
                    },
                },
            )
            response.raise_for_status()
        finally:
            if owns_client:
                await cast(httpx.AsyncClient, client).aclose()


def _interaction_confirmation_content(
    action: DiscordClarificationAction,
    status: DiscordInteractionStatus,
    *,
    initial: bool = False,
) -> str:
    label = _clarification_action_label(action)
    if initial:
        if status == "unauthorized":
            return f"Choice not accepted: {label}. This Discord user is not authorized."
        return f"Choice received: {label}. Queueing this decision now."
    if status in {"handled", "queued"}:
        return f"Choice queued: {label}. I will update this message when Notion finishes."
    if status == "ignored":
        return "Confirmed choice: Ignore. No Notion change was made."
    if status == "duplicate":
        return (
            f"Choice received: {label}. This clarification was already resolved; "
            "no duplicate Notion change was made."
        )
    if status == "unauthorized":
        return f"Choice not accepted: {label}. This Discord user is not authorized."
    if status == "invalid":
        return (
            f"Choice not accepted: {label}. The clarification is invalid or expired; "
            "no Notion change was made."
        )
    return (
        f"Choice not queued: {label}. LifeAgent could not queue this update; "
        "no Notion change was made."
    )


def _clarification_action_label(action: DiscordClarificationAction) -> str:
    return {
        "quiz": "Quiz",
        "assignment": "Assignment",
        "tutorial": "Tutorial",
        "lab": "Lab",
        "studying_block": "Studying Block",
        "ignore": "Ignore",
    }[action]


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


def normalize_academic_message(
    data: Mapping[str, object],
    *,
    allowed_channel_ids: frozenset[str] | set[str],
    authorized_user_ids: frozenset[str] | set[str],
) -> DiscordAcademicMessageCreate | None:
    channel_id = _str_value(data.get("channel_id"))
    author = _mapping_value(data.get("author"))
    author_id = _str_value(author.get("id")) if author is not None else None
    author_is_bot = bool(author.get("bot")) if author is not None else False
    message_id = _str_value(data.get("id"))
    if channel_id is None or author_id is None or message_id is None:
        return None
    if (
        author_is_bot
        or channel_id not in allowed_channel_ids
        or author_id not in authorized_user_ids
    ):
        return None

    timestamp = _parse_discord_timestamp(_str_value(data.get("timestamp")))
    content = _str_value(data.get("content"))
    if timestamp is None or content is None:
        return None
    if not content or len(content) > _DISCORD_MESSAGE_CONTENT_LIMIT:
        return None
    raw_mentions = data.get("mentions")
    mentioned_user_ids: list[str] = []
    if isinstance(raw_mentions, list):
        for item in cast(list[Any], raw_mentions)[:20]:
            mention = _mapping_value(item)
            mention_id = _str_value(mention.get("id")) if mention is not None else None
            if mention_id is not None and _DISCORD_ID_PATTERN.fullmatch(mention_id) is not None:
                mentioned_user_ids.append(mention_id)
    return DiscordAcademicMessageCreate(
        message_id=message_id,
        channel_id=channel_id,
        author_id=author_id,
        timestamp=timestamp,
        content=SecretStr(content),
        mentioned_user_ids=tuple(mentioned_user_ids),
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


def _message_rejection_status(
    data: Mapping[str, object],
    allowed_channel_ids: frozenset[str],
    authorized_user_ids: frozenset[str],
) -> DiscordInteractionStatus:
    channel_id = _str_value(data.get("channel_id"))
    author = _mapping_value(data.get("author"))
    author_id = _str_value(author.get("id")) if author is not None else None
    author_is_bot = bool(author.get("bot")) if author is not None else False
    if author_is_bot:
        return "ignored"
    if channel_id not in allowed_channel_ids or author_id not in authorized_user_ids:
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


def _parse_discord_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


__all__ = [
    "DiscordAcademicMessageCreate",
    "DiscordClarificationAction",
    "DiscordClarificationCallbackResult",
    "DiscordClarificationEnqueuer",
    "DiscordClarificationInteraction",
    "DiscordGatewayConfigurationError",
    "DiscordGatewayConnect",
    "DiscordGatewayHttpClient",
    "DiscordGatewayListener",
    "DiscordGatewayReconnect",
    "DiscordGatewayReconnectError",
    "DiscordGatewaySleep",
    "DiscordGatewayWebSocket",
    "DiscordInteractionStatus",
    "DiscordMessageCallbackResult",
    "DiscordMessageHandler",
    "normalize_academic_message",
    "normalize_clarification_interaction",
    "parse_clarification_custom_id",
]
