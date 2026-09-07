from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.connectors.discord_gateway import (
    DiscordAcademicMessageCreate,
    DiscordClarificationCallbackResult,
    DiscordClarificationInteraction,
    DiscordGatewayConfigurationError,
    DiscordGatewayListener,
    DiscordGatewayReconnect,
    DiscordMessageCallbackResult,
    normalize_academic_message,
)

CHANNEL = "987654321012345678"
OTHER_CHANNEL = "111112222233333"
USER = "222223333344444"
OTHER_USER = "555556666677777"
MESSAGE = "333334444455555"
TOKEN = "never-print-this-discord-token"
PRIVATE_CONTENT = "completed assessment-secret"
MESSAGE_CONTENT_INTENTS = 33_280


class _GatewayHttp:
    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        assert url == "https://discord.com/api/v10/gateway/bot"
        assert headers == {"Authorization": f"Bot {TOKEN}"}
        return httpx.Response(
            200,
            json={"url": "wss://gateway.discord.test"},
            request=httpx.Request("GET", url),
        )

    async def post(
        self,
        url: str,
        *,
        json: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        raise AssertionError("message gateway tests should not POST interaction callbacks")


class _FakeWebSocket:
    def __init__(self, messages: list[Mapping[str, object]]) -> None:
        self._messages = [json.dumps(message) for message in messages]
        self.sent: list[Mapping[str, object]] = []
        self.closed = False

    async def recv(self) -> str:
        if not self._messages:
            raise ConnectionError("fake gateway exhausted")
        return self._messages.pop(0)

    async def send(self, data: str) -> None:
        loaded = json.loads(data)
        assert isinstance(loaded, dict)
        self.sent.append(loaded)

    async def close(self) -> None:
        self.closed = True


class _ClosedByDiscordError(Exception):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__("safe synthetic close")


class _ClosingWebSocket:
    closed = False

    async def recv(self) -> str:
        raise _ClosedByDiscordError(4014)

    async def send(self, data: str) -> None:
        raise AssertionError("closing websocket should not send")

    async def close(self) -> None:
        self.closed = True


class _ClarificationHandler:
    async def __call__(
        self,
        interaction: DiscordClarificationInteraction,
    ) -> DiscordClarificationCallbackResult:
        raise AssertionError("message tests should not call clarification handler")


class _MessageHandler:
    def __init__(self) -> None:
        self.messages: list[DiscordAcademicMessageCreate] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, message: DiscordAcademicMessageCreate) -> DiscordMessageCallbackResult:
        self.messages.append(message)
        self.started.set()
        await self.release.wait()
        return DiscordMessageCallbackResult(status="handled")


class _FailOnceMessageHandler(_MessageHandler):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def __call__(self, message: DiscordAcademicMessageCreate) -> DiscordMessageCallbackResult:
        self.messages.append(message)
        self.calls += 1
        if self.calls == 1:
            return DiscordMessageCallbackResult(status="failed")
        return DiscordMessageCallbackResult(status="handled")


class _ContentGuard(dict[str, object]):
    def get(self, key: str, default: object = None) -> object:
        if key == "content":
            raise AssertionError("unauthorized Discord content was accessed")
        return super().get(key, default)


def _listener(
    *,
    message_content_enabled: bool = True,
    message_handler: _MessageHandler | None = None,
) -> DiscordGatewayListener:
    return DiscordGatewayListener(
        token=SecretStr(TOKEN),
        api_base_url="https://discord.com/api/v10",
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
        handler=_ClarificationHandler(),
        message_content_enabled=message_content_enabled,
        message_handler=message_handler,
        http_client=_GatewayHttp(),
        websocket_connect=lambda _: _never_connect(),
    )


def _message_payload(
    *,
    message_id: str = MESSAGE,
    channel_id: str = CHANNEL,
    author_id: str = USER,
    content: str = PRIVATE_CONTENT,
    bot: bool = False,
) -> dict[str, object]:
    return {
        "op": 0,
        "s": 2,
        "t": "MESSAGE_CREATE",
        "d": {
            "id": message_id,
            "channel_id": channel_id,
            "author": {"id": author_id, "bot": bot},
            "timestamp": "2026-09-03T21:00:00.000000+00:00",
            "content": content,
        },
    }


@pytest.mark.asyncio
async def test_message_content_flag_controls_gateway_intents() -> None:
    disabled_ws = _FakeWebSocket(
        [
            {"op": 10, "d": {"heartbeat_interval": 60_000}},
            {"op": 7, "d": None},
        ]
    )

    async def connect_disabled(url: str) -> _FakeWebSocket:
        assert url == "wss://gateway.discord.test/?v=10&encoding=json"
        return disabled_ws

    disabled = DiscordGatewayListener(
        token=SecretStr(TOKEN),
        api_base_url="https://discord.com/api/v10",
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
        handler=_ClarificationHandler(),
        http_client=_GatewayHttp(),
        websocket_connect=connect_disabled,
    )
    with pytest.raises(DiscordGatewayReconnect):
        await disabled.run_once()

    enabled_ws = _FakeWebSocket(
        [
            {"op": 10, "d": {"heartbeat_interval": 60_000}},
            {"op": 7, "d": None},
        ]
    )

    async def connect_enabled(url: str) -> _FakeWebSocket:
        assert url == "wss://gateway.discord.test/?v=10&encoding=json"
        return enabled_ws

    enabled = DiscordGatewayListener(
        token=SecretStr(TOKEN),
        api_base_url="https://discord.com/api/v10",
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
        handler=_ClarificationHandler(),
        message_content_enabled=True,
        http_client=_GatewayHttp(),
        websocket_connect=connect_enabled,
    )
    with pytest.raises(DiscordGatewayReconnect):
        await enabled.run_once()

    assert disabled.identity_intents == 0
    assert enabled.identity_intents == MESSAGE_CONTENT_INTENTS
    assert disabled_ws.sent[0]["d"]["intents"] == 0
    assert enabled_ws.sent[0]["d"]["intents"] == MESSAGE_CONTENT_INTENTS


@pytest.mark.asyncio
async def test_authorized_message_create_is_scheduled_once_and_redacted() -> None:
    handler = _MessageHandler()
    listener = _listener(message_handler=handler)

    assert await listener.handle_gateway_payload(_message_payload()) == "handled"
    assert await listener.handle_gateway_payload(_message_payload()) == "duplicate"
    await asyncio.wait_for(handler.started.wait(), timeout=1)
    assert handler.messages == [
        DiscordAcademicMessageCreate(
            message_id=MESSAGE,
            channel_id=CHANNEL,
            author_id=USER,
            timestamp=datetime(2026, 9, 3, 21, tzinfo=UTC),
            content=SecretStr(PRIVATE_CONTENT),
        )
    ]
    assert PRIVATE_CONTENT not in repr(handler.messages[0])
    assert PRIVATE_CONTENT not in str(handler.messages[0])

    handler.release.set()
    await listener.drain_message_tasks()


@pytest.mark.asyncio
async def test_message_callback_is_not_awaited_in_receive_loop() -> None:
    handler = _MessageHandler()
    listener = _listener(message_handler=handler)

    status = await asyncio.wait_for(
        listener.handle_gateway_payload(_message_payload()),
        timeout=0.1,
    )

    assert status == "handled"
    assert handler.release.is_set() is False
    handler.release.set()
    await listener.drain_message_tasks()


@pytest.mark.asyncio
async def test_failed_callback_can_be_retried_before_message_is_remembered() -> None:
    handler = _FailOnceMessageHandler()
    listener = _listener(message_handler=handler)

    assert await listener.handle_gateway_payload(_message_payload()) == "handled"
    await listener.drain_message_tasks()
    assert await listener.handle_gateway_payload(_message_payload()) == "handled"
    await listener.drain_message_tasks()
    assert await listener.handle_gateway_payload(_message_payload()) == "duplicate"
    assert handler.calls == 2


@pytest.mark.asyncio
async def test_wrong_channel_user_and_bot_messages_do_not_extract_content() -> None:
    handler = _MessageHandler()
    listener = _listener(message_handler=handler)

    wrong_channel = _ContentGuard(
        {
            "id": MESSAGE,
            "channel_id": OTHER_CHANNEL,
            "author": {"id": USER, "bot": False},
            "timestamp": "2026-09-03T21:00:00Z",
        }
    )
    wrong_user = _ContentGuard(
        {
            "id": "333334444455556",
            "channel_id": CHANNEL,
            "author": {"id": OTHER_USER, "bot": False},
            "timestamp": "2026-09-03T21:00:00Z",
        }
    )
    bot_authored = _ContentGuard(
        {
            "id": "333334444455557",
            "channel_id": CHANNEL,
            "author": {"id": USER, "bot": True},
            "timestamp": "2026-09-03T21:00:00Z",
        }
    )

    assert await listener.handle_gateway_payload({"t": "MESSAGE_CREATE", "d": wrong_channel}) == (
        "unauthorized"
    )
    assert await listener.handle_gateway_payload({"t": "MESSAGE_CREATE", "d": wrong_user}) == (
        "unauthorized"
    )
    assert await listener.handle_gateway_payload({"t": "MESSAGE_CREATE", "d": bot_authored}) == (
        "ignored"
    )
    assert handler.messages == []


@pytest.mark.asyncio
async def test_message_create_is_ignored_when_free_text_is_disabled() -> None:
    handler = _MessageHandler()
    listener = _listener(message_content_enabled=False, message_handler=handler)

    assert await listener.handle_gateway_payload(_message_payload()) == "ignored"
    assert handler.messages == []
    assert listener.identity_intents == 0


def test_normalized_message_requires_bounded_content_and_timestamp() -> None:
    too_long = _message_payload(content="x" * 2_001)["d"]
    assert isinstance(too_long, dict)
    assert (
        normalize_academic_message(
            too_long,
            allowed_channel_ids={CHANNEL},
            authorized_user_ids={USER},
        )
        is None
    )

    invalid_timestamp = _message_payload()["d"]
    assert isinstance(invalid_timestamp, dict)
    invalid_timestamp["timestamp"] = "not a timestamp"
    assert (
        normalize_academic_message(
            invalid_timestamp,
            allowed_channel_ids={CHANNEL},
            authorized_user_ids={USER},
        )
        is None
    )


@pytest.mark.asyncio
async def test_privileged_intent_close_sets_safe_actionable_diagnostic() -> None:
    websocket = _ClosingWebSocket()

    async def connect(url: str) -> _ClosingWebSocket:
        assert url == "wss://gateway.discord.test/?v=10&encoding=json"
        return websocket

    listener = DiscordGatewayListener(
        token=SecretStr(TOKEN),
        api_base_url="https://discord.com/api/v10",
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
        handler=_ClarificationHandler(),
        message_content_enabled=True,
        http_client=_GatewayHttp(),
        websocket_connect=connect,
    )

    with pytest.raises(DiscordGatewayConfigurationError) as raised:
        await listener.run_once()

    assert websocket.closed is True
    assert "Message Content Intent" in raised.value.diagnostic
    assert raised.value.diagnostic == listener.last_diagnostic
    assert TOKEN not in raised.value.diagnostic


async def _never_connect() -> Any:
    raise AssertionError("gateway connection should not be used")
