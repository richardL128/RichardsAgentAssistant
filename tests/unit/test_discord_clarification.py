from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.connectors.discord import (
    AcademicClarificationMessage,
    AcademicSetupReminderMessage,
    DiscordAcademicPlannerAdapter,
)
from app.connectors.discord_gateway import (
    DiscordClarificationCallbackResult,
    DiscordClarificationInteraction,
    DiscordGatewayListener,
    DiscordGatewayReconnect,
    parse_clarification_custom_id,
)

CHANNEL = "987654321012345678"
OTHER_CHANNEL = "111112222233333"
USER = "222223333344444"
OTHER_USER = "555556666677777"
TOKEN = "never-print-this-discord-token"


@pytest.mark.asyncio
async def test_clarification_message_has_three_opaque_buttons_and_previews() -> None:
    clarification_id = uuid4()
    delivery_id = uuid4()
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "123456789012345678", "guild_id": "42"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(respond),
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr(TOKEN),
            allowed_channel_ids={CHANNEL},
            client=client,
        )
        receipt = await adapter.send_clarification(
            AcademicClarificationMessage(
                delivery_id=delivery_id,
                clarification_id=clarification_id,
                channel_id=CHANNEL,
                current_title="Chapter 4",
                quiz_title_preview="Quiz — Chapter 4",
                assignment_title_preview="Assignment — Chapter 4",
            )
        )

    assert receipt.external_id == "123456789012345678"
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["nonce"] == str(delivery_id)
    assert body["enforce_nonce"] is True
    assert body["allowed_mentions"] == {"parse": []}
    assert body["content"] == (
        "Please classify this Notion assessment before any title change.\n"
        "Current title: Chapter 4\n"
        "Quiz preview: Quiz — Chapter 4\n"
        "Assignment preview: Assignment — Chapter 4"
    )
    buttons = body["components"][0]["components"]
    assert [button["label"] for button in buttons] == ["Quiz", "Assignment", "Ignore"]
    assert [button["custom_id"] for button in buttons] == [
        f"academic_clarify:{clarification_id}:quiz",
        f"academic_clarify:{clarification_id}:assignment",
        f"academic_clarify:{clarification_id}:ignore",
    ]
    assert TOKEN not in requests[0].content.decode()


@pytest.mark.asyncio
async def test_setup_reminder_has_required_text_and_no_components() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "123456789012345678"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(respond),
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr(TOKEN),
            allowed_channel_ids={CHANNEL},
            client=client,
        )
        await adapter.send_setup_reminder(
            AcademicSetupReminderMessage(
                delivery_id=uuid4(),
                channel_id=CHANNEL,
                condition="missing seeded Assessments calendar",
                affected_course_codes=("CS101", "MATH-240"),
            )
        )

    body = json.loads(requests[0].content)
    assert body["components"] == []
    assert "share/configure the Courses database" in body["content"]
    assert "create course pages from the New Course template" in body["content"]
    assert "No Notion changes were made." in body["content"]
    assert "CS101, MATH-240" in body["content"]
    assert "custom_id" not in requests[0].content.decode()
    with pytest.raises(ValidationError):
        AcademicSetupReminderMessage(
            delivery_id=uuid4(),
            channel_id=CHANNEL,
            condition="duplicate matching child calendars",
            affected_course_codes=tuple(f"COURSE{i}" for i in range(11)),
        )


@pytest.mark.asyncio
async def test_clarification_rejects_non_allowlisted_channel_before_http() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "123456789012345678"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(respond),
    ) as client:
        adapter = DiscordAcademicPlannerAdapter(
            token=SecretStr(TOKEN),
            allowed_channel_ids={OTHER_CHANNEL},
            client=client,
        )
        with pytest.raises(ValueError, match="allowlisted"):
            await adapter.send_clarification(
                AcademicClarificationMessage(
                    delivery_id=uuid4(),
                    clarification_id=uuid4(),
                    channel_id=CHANNEL,
                    current_title="Chapter 4",
                    quiz_title_preview="Quiz — Chapter 4",
                    assignment_title_preview="Assignment — Chapter 4",
                )
            )

    assert requests == []


class _GatewayHttp:
    def __init__(self) -> None:
        self.posts: list[tuple[str, Mapping[str, object] | None]] = []

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
        self.posts.append((url, json))
        return httpx.Response(204, request=httpx.Request("POST", url))


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


class _Handler:
    def __init__(self) -> None:
        self.interactions: list[DiscordClarificationInteraction] = []

    async def __call__(
        self,
        interaction: DiscordClarificationInteraction,
    ) -> DiscordClarificationCallbackResult:
        self.interactions.append(interaction)
        return DiscordClarificationCallbackResult(status="handled")


def _interaction_payload(
    *,
    interaction_id: str,
    clarification_id: str,
    channel_id: str = CHANNEL,
    user_id: str = USER,
    message_content: str = "private Discord content must stay raw-only",
) -> dict[str, object]:
    return {
        "op": 0,
        "s": 2,
        "t": "INTERACTION_CREATE",
        "d": {
            "id": interaction_id,
            "token": "private-interaction-token",
            "type": 3,
            "channel_id": channel_id,
            "member": {"user": {"id": user_id}},
            "message": {"content": message_content},
            "data": {
                "custom_id": f"academic_clarify:{clarification_id}:quiz",
            },
        },
    }


@pytest.mark.asyncio
async def test_gateway_identifies_heartbeats_dispatches_and_resumes() -> None:
    clarification_id = uuid4()
    first_ws = _FakeWebSocket(
        [
            {"op": 10, "d": {"heartbeat_interval": 60_000}},
            {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "session-1"}},
            _interaction_payload(
                interaction_id="123456789012345678",
                clarification_id=str(clarification_id),
            ),
            {"op": 1, "d": None},
            {"op": 7, "d": None},
        ]
    )
    second_ws = _FakeWebSocket(
        [
            {"op": 10, "d": {"heartbeat_interval": 60_000}},
            {"op": 7, "d": None},
        ]
    )
    websockets = [first_ws, second_ws]

    async def connect(url: str) -> _FakeWebSocket:
        assert url == "wss://gateway.discord.test/?v=10&encoding=json"
        return websockets.pop(0)

    http_client = _GatewayHttp()
    handler = _Handler()
    listener = DiscordGatewayListener(
        token=SecretStr(TOKEN),
        api_base_url="https://discord.com/api/v10",
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
        handler=handler,
        http_client=http_client,
        websocket_connect=connect,
    )

    with pytest.raises(DiscordGatewayReconnect):
        await listener.run_once()
    with pytest.raises(DiscordGatewayReconnect):
        await listener.run_once()

    assert first_ws.closed is True
    assert second_ws.closed is True
    assert first_ws.sent[0]["op"] == 2
    assert first_ws.sent[0]["d"]["intents"] == 0
    assert first_ws.sent[-1] == {"op": 1, "d": 2}
    assert second_ws.sent[0]["op"] == 6
    assert second_ws.sent[0]["d"]["session_id"] == "session-1"
    assert handler.interactions == [
        DiscordClarificationInteraction(
            interaction_id="123456789012345678",
            channel_id=CHANNEL,
            user_id=USER,
            clarification_id=clarification_id,
            action="quiz",
        )
    ]
    assert http_client.posts == [
        (
            "/interactions/123456789012345678/private-interaction-token/callback",
            {"type": 6},
        )
    ]
    assert "private Discord content" not in repr(handler.interactions)
    assert "private-interaction-token" not in repr(handler.interactions)


@pytest.mark.asyncio
async def test_gateway_authorizes_user_and_makes_replays_harmless() -> None:
    clarification_id = uuid4()
    http_client = _GatewayHttp()
    handler = _Handler()
    listener = DiscordGatewayListener(
        token=SecretStr(TOKEN),
        api_base_url="https://discord.com/api/v10",
        allowed_channel_ids={CHANNEL},
        authorized_user_ids={USER},
        handler=handler,
        http_client=http_client,
        websocket_connect=lambda _: _never_connect(),
    )
    payload = _interaction_payload(
        interaction_id="123456789012345678",
        clarification_id=str(clarification_id),
    )
    unauthorized = _interaction_payload(
        interaction_id="223456789012345678",
        clarification_id=str(clarification_id),
        user_id=OTHER_USER,
    )

    assert await listener.handle_gateway_payload(payload) == "handled"
    assert await listener.handle_gateway_payload(payload) == "duplicate"
    assert await listener.handle_gateway_payload(unauthorized) == "unauthorized"

    assert len(handler.interactions) == 1
    assert len(http_client.posts) == 3
    assert "private Discord content" not in repr(handler.interactions[0])
    assert parse_clarification_custom_id(f"academic_clarify:{clarification_id}:assignment") == (
        clarification_id,
        "assignment",
    )
    assert parse_clarification_custom_id(f"academic_clarify:{clarification_id}:paper") is None


async def _never_connect() -> Any:
    raise AssertionError("gateway connection should not be used")
