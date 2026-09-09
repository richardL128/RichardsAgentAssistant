import httpx
import pytest
from pydantic import SecretStr

from app.host.discord import (
    HOST_WAKE_ACKNOWLEDGEMENT,
    DiscordWakeAckAdapter,
    safe_failure_content,
    wake_ack_nonce,
)


@pytest.mark.asyncio
async def test_discord_ack_uses_exact_message_deterministic_nonce_and_no_mentions() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "444444444444444444"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await DiscordWakeAckAdapter(
            token=SecretStr("discord-token"),
            allowed_channel_id="222222222222222222",
            client=client,
        ).send_acknowledgement(
            channel_id="222222222222222222",
            root_message_id="111111111111111111",
        )

    assert result == "444444444444444444"
    body = request_json(requests[0])
    assert body["content"] == HOST_WAKE_ACKNOWLEDGEMENT
    assert body["nonce"] == wake_ack_nonce("111111111111111111")
    assert body["enforce_nonce"] is True
    assert body["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_discord_edit_uses_safe_failure_text() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "444444444444444444"})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        await DiscordWakeAckAdapter(
            token=SecretStr("discord-token"),
            allowed_channel_id="222222222222222222",
            client=client,
        ).edit_acknowledgement(
            channel_id="222222222222222222",
            acknowledgement_message_id="444444444444444444",
            content=safe_failure_content("docker_timeout"),
        )

    assert request_json(requests[0])["content"].startswith(
        "LifeAgent could not start Docker Desktop."
    )


def request_json(request: httpx.Request) -> dict[str, object]:
    import json

    loaded = json.loads(request.content.decode("utf-8"))
    assert isinstance(loaded, dict)
    return loaded
