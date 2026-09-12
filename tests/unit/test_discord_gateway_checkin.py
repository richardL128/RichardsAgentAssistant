from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from websockets.exceptions import WebSocketException

from app.agents.academic_planner import discord_checkin
from app.agents.academic_planner.contracts import AcademicRequestRouteDecision
from app.agents.academic_planner.discord_checkin import AcademicDiscordCheckinHandler
from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    DiscordFetchedAttachment,
    DiscordFetchedMessage,
)
from app.connectors.discord_gateway import (
    DiscordAcademicMessageAttachment,
    DiscordAcademicMessageCreate,
    DiscordClarificationCallbackResult,
    DiscordClarificationInteraction,
    DiscordGatewayConfigurationError,
    DiscordGatewayListener,
    DiscordGatewayReconnect,
    DiscordMessageCallbackResult,
    normalize_academic_message,
)
from app.core.errors import ErrorCode, LifeAgentError
from app.llm.ollama_runtime import OllamaRuntimeReady

CHANNEL = "987654321012345678"
OTHER_CHANNEL = "111112222233333"
USER = "222223333344444"
OTHER_USER = "555556666677777"
MESSAGE = "333334444455555"
ASSISTANT = "444445555566666"
TOKEN = "never-print-this-discord-token"
PRIVATE_CONTENT = "completed assessment-secret"
MESSAGE_CONTENT_INTENTS = 33_280
PDF_URL = "https://cdn.discordapp.com/attachments/1/2/rubric.pdf?ex=abc&is=def&hm=123"
PDF_BYTES = b"%PDF-1.7\nbounded test bytes\n"


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
        if key in {"content", "attachments"}:
            raise AssertionError("unauthorized Discord content or attachments were accessed")
        return super().get(key, default)


def _listener(
    *,
    message_content_enabled: bool = True,
    message_handler: Any | None = None,
) -> DiscordGatewayListener:
    return DiscordGatewayListener(
        token=SecretStr(TOKEN),
        api_base_url="https://discord.com/api/v10",
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
        clarification_enqueuer=_ClarificationHandler(),
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
    mentioned_user_ids: tuple[str, ...] = (),
    attachments: list[Mapping[str, object]] | None = None,
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
            "mentions": [{"id": item} for item in mentioned_user_ids],
            "attachments": attachments or [],
        },
    }


def _attachment_payload(
    *,
    attachment_id: str = "777778888899999",
    filename: str = "rubric.pdf",
    content_type: object = "application/pdf",
    size: object = len(PDF_BYTES),
    url: object = PDF_URL,
) -> dict[str, object]:
    return {
        "id": attachment_id,
        "filename": filename,
        "content_type": content_type,
        "size": size,
        "url": url,
    }


class _AcademicStore:
    confirmation_ttl_hours = 24

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def get_latest_daily_plan(self) -> None:
        return None

    def save_discord_checkin(self, *args: object, **kwargs: object) -> object:
        self.events.append("persist")
        return SimpleNamespace(status="created")


class _AcademicDelivery:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def send_response(self, content: str, *, idempotency_key: str) -> object:
        self.events.append("ack")
        return object()

    async def send_confirmation(self, proposal: object, *, idempotency_key: str) -> object:
        self.events.append("confirm")
        return object()


class _AcademicRuntime:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def ensure_ready(self) -> OllamaRuntimeReady:
        self.events.append("ready")
        return OllamaRuntimeReady(model="qwen-test:latest", digest="digest")


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
        clarification_enqueuer=_ClarificationHandler(),
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
        clarification_enqueuer=_ClarificationHandler(),
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
async def test_run_forever_reconnects_after_transient_websocket_failure() -> None:
    connection_attempts = 0
    sleeps: list[float] = []

    class TransientWebSocket:
        async def recv(self) -> str:
            raise WebSocketException("synthetic network reset")

        async def send(self, data: str) -> None:
            raise AssertionError(f"transient websocket should not send: {data}")

        async def close(self) -> None:
            return None

    async def connect(_url: str) -> TransientWebSocket:
        nonlocal connection_attempts
        connection_attempts += 1
        return TransientWebSocket()

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    listener = DiscordGatewayListener(
        token=SecretStr(TOKEN),
        api_base_url="https://discord.com/api/v10",
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
        clarification_enqueuer=_ClarificationHandler(),
        message_content_enabled=True,
        http_client=_GatewayHttp(),
        websocket_connect=connect,
        sleep=sleep,
    )

    await listener.run_forever(max_attempts=2)

    assert connection_attempts == 2
    assert sleeps == [1.0, 1.0]


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
async def test_attachment_only_pdf_message_is_scheduled_with_redacted_metadata() -> None:
    handler = _MessageHandler()
    listener = _listener(message_handler=handler)
    payload = _message_payload(content="", attachments=[_attachment_payload()])

    assert await listener.handle_gateway_payload(payload) == "handled"
    await asyncio.wait_for(handler.started.wait(), timeout=1)

    message = handler.messages[0]
    assert message.content.get_secret_value() == ""
    assert message.attachments == (
        DiscordAcademicMessageAttachment(
            id="777778888899999",
            filename="rubric.pdf",
            content_type="application/pdf",
            size=len(PDF_BYTES),
            url=SecretStr(PDF_URL),
        ),
    )
    assert message.inbound_material_ids == ()
    assert PDF_URL not in repr(message)
    assert PDF_URL not in str(message)

    handler.release.set()
    await listener.drain_message_tasks()


def test_normalized_message_keeps_only_first_five_candidate_pdf_attachments() -> None:
    raw = _message_payload(
        content="",
        attachments=[
            _attachment_payload(
                attachment_id=str(777778888899990 + index),
                filename=f"rubric-{index}.pdf",
            )
            for index in range(6)
        ],
    )["d"]
    assert isinstance(raw, dict)

    message = normalize_academic_message(
        raw,
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
    )

    assert message is not None
    assert [attachment.filename for attachment in message.attachments] == [
        "rubric-0.pdf",
        "rubric-1.pdf",
        "rubric-2.pdf",
        "rubric-3.pdf",
        "rubric-4.pdf",
    ]


@pytest.mark.parametrize(
    "attachment",
    [
        _attachment_payload(filename="rubric.txt"),
        _attachment_payload(filename="../rubric.pdf"),
        _attachment_payload(content_type="image/png"),
        _attachment_payload(size=0),
        _attachment_payload(size=20 * 1024 * 1024 + 1),
        _attachment_payload(url="https://example.com/attachments/1/2/rubric.pdf"),
        _attachment_payload(url="https://cdn.discordapp.com/not-attachments/rubric.pdf"),
        _attachment_payload(url=None),
    ],
)
def test_empty_message_requires_at_least_one_valid_pdf_attachment(
    attachment: Mapping[str, object],
) -> None:
    raw = _message_payload(content="", attachments=[attachment])["d"]
    assert isinstance(raw, dict)

    assert (
        normalize_academic_message(
            raw,
            allowed_channel_ids={CHANNEL},
            authorized_user_ids={USER},
        )
        is None
    )


def test_text_message_ignores_malformed_attachment_metadata() -> None:
    raw = _message_payload(
        content="please help with this",
        attachments=[_attachment_payload(filename="../rubric.pdf")],
    )["d"]
    assert isinstance(raw, dict)

    message = normalize_academic_message(
        raw,
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
    )

    assert message is not None
    assert message.attachments == ()


@pytest.mark.asyncio
async def test_duplicate_mentioned_event_runs_readiness_and_qwen_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def run_loop(**kwargs: object) -> object:
        events.append("qwen")
        return SimpleNamespace(changes=(), question="Which academic target should I change?")

    monkeypatch.setattr(discord_checkin, "run_academic_agent_loop", run_loop)

    class SemanticRouter:
        async def invoke_structured(self, **_kwargs: object) -> object:
            return SimpleNamespace(
                output=AcademicRequestRouteDecision(
                    calendar_request="help plan my quiz",
                )
            )

    handler = AcademicDiscordCheckinHandler(
        store=_AcademicStore(events),
        delivery=_AcademicDelivery(events),
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
        writer_provider=lambda: None,
        ollama_runtime=_AcademicRuntime(events),
        agent_gateway=object(),
        semantic_router_gateway=SemanticRouter(),
        agent_catalog=object(),
        assistant_user_id=ASSISTANT,
    )
    listener = _listener(message_handler=handler)
    payload = _message_payload(
        content=f"<@{ASSISTANT}> help plan my quiz",
        mentioned_user_ids=(ASSISTANT,),
    )

    assert await listener.handle_gateway_payload(payload) == "handled"
    assert await listener.handle_gateway_payload(payload) == "duplicate"
    await listener.drain_message_tasks()
    assert await listener.handle_gateway_payload(payload) == "duplicate"

    assert events == ["ready", "qwen", "persist", "ack"]


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


def test_refetched_message_contract_accepts_attachment_only_and_redacts_signed_url() -> None:
    message = DiscordFetchedMessage.model_validate(
        {
            "id": MESSAGE,
            "channel_id": CHANNEL,
            "author": {"id": USER},
            "timestamp": "2026-09-03T21:00:00.000000+00:00",
            "content": "",
            "attachments": [_attachment_payload()],
        }
    )

    assert message.attachments == (
        DiscordFetchedAttachment(
            id="777778888899999",
            filename="rubric.pdf",
            content_type="application/pdf",
            size=len(PDF_BYTES),
            url=SecretStr(PDF_URL),
        ),
    )
    assert PDF_URL not in repr(message)
    assert PDF_URL not in str(message)


def test_refetched_message_rejects_empty_body_without_candidate_pdf() -> None:
    with pytest.raises(ValueError, match="content or a candidate PDF"):
        DiscordFetchedMessage.model_validate(
            {
                "id": MESSAGE,
                "channel_id": CHANNEL,
                "author": {"id": USER},
                "timestamp": "2026-09-03T21:00:00.000000+00:00",
                "content": "",
                "attachments": [_attachment_payload(filename="rubric.txt")],
            }
        )


@pytest.mark.asyncio
async def test_fetch_message_exposes_bounded_refetched_attachment_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/channels/{CHANNEL}/messages/{MESSAGE}")
        assert request.headers["authorization"] == f"Bot {TOKEN}"
        return httpx.Response(
            200,
            json={
                "id": MESSAGE,
                "channel_id": CHANNEL,
                "author": {"id": USER},
                "timestamp": "2026-09-03T21:00:00.000000+00:00",
                "content": "",
                "attachments": [_attachment_payload()],
            },
        )

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr(TOKEN),
            allowed_channel_ids={CHANNEL},
            client=client,
        )

        message = await adapter.fetch_message(channel_id=CHANNEL, message_id=MESSAGE)

    assert message.attachments[0].filename == "rubric.pdf"
    assert message.attachments[0].url.get_secret_value() == PDF_URL


@pytest.mark.asyncio
async def test_download_pdf_attachment_uses_cdn_without_bot_auth_and_verifies_bytes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == PDF_URL
        assert "authorization" not in request.headers
        return httpx.Response(200, content=PDF_BYTES)

    attachment = DiscordFetchedAttachment(
        id="777778888899999",
        filename="rubric.pdf",
        content_type="application/pdf",
        size=len(PDF_BYTES),
        url=SecretStr(PDF_URL),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr(TOKEN),
            allowed_channel_ids={CHANNEL},
            client=client,
        )

        download = await adapter.download_pdf_attachment(attachment)

    assert download.attachment_id == attachment.id
    assert download.filename == "rubric.pdf"
    assert download.declared_size == len(PDF_BYTES)
    assert download.observed_size == len(PDF_BYTES)
    assert download.content == PDF_BYTES
    assert download.sha256_hex
    assert PDF_BYTES.decode() not in repr(download)


@pytest.mark.asyncio
async def test_download_pdf_attachment_rejects_redirects_size_mismatch_and_spoofed_bytes() -> None:
    cases: list[tuple[httpx.Response, str]] = [
        (
            httpx.Response(
                302,
                headers={"Location": "https://cdn.discordapp.com/attachments/1/2/other.pdf"},
            ),
            "redirect",
        ),
        (httpx.Response(200, content=PDF_BYTES + b"extra"), "size"),
        (httpx.Response(200, content=b"not a pdf".ljust(len(PDF_BYTES), b"x")), "PDF"),
    ]
    for response, expected in cases:

        async def handler(_request: httpx.Request, response: httpx.Response = response):
            return response

        attachment = DiscordFetchedAttachment(
            id="777778888899999",
            filename="rubric.pdf",
            content_type="application/pdf",
            size=len(PDF_BYTES),
            url=SecretStr(PDF_URL),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = DiscordAcademicPlannerAdapter(
                token=SecretStr(TOKEN),
                allowed_channel_ids={CHANNEL},
                client=client,
            )

            with pytest.raises(LifeAgentError) as raised:
                await adapter.download_pdf_attachment(attachment)

        assert raised.value.record.code is ErrorCode.INPUT_INVALID
        assert expected in raised.value.record.diagnostic


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
        clarification_enqueuer=_ClarificationHandler(),
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
