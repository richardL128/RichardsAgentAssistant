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


@pytest.mark.asyncio
async def test_discord_edit_retries_bounded_rate_limit_with_fixed_body() -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"})
        return httpx.Response(200, json={"id": "444444444444444444"})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        await DiscordWakeAckAdapter(
            token=SecretStr("discord-token"),
            allowed_channel_id="222222222222222222",
            client=client,
            sleep=sleep,
        ).edit_acknowledgement(
            channel_id="222222222222222222",
            acknowledgement_message_id="444444444444444444",
            content="Still starting LifeAgent (8s elapsed).",
        )

    assert sleeps == [0.25]
    assert len(requests) == 2
    assert requests[0].method == requests[1].method == "PATCH"
    assert requests[0].url == requests[1].url
    assert request_json(requests[0]) == request_json(requests[1])


@pytest.mark.asyncio
async def test_discord_edit_uses_json_retry_after_when_header_is_missing() -> None:
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"retry_after": 0.1, "secret": "not exposed"})
        return httpx.Response(200, json={"id": "444444444444444444"})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        await DiscordWakeAckAdapter(
            token=SecretStr("discord-token"),
            allowed_channel_id="222222222222222222",
            client=client,
            sleep=sleep,
        ).edit_acknowledgement(
            channel_id="222222222222222222",
            acknowledgement_message_id="444444444444444444",
            content="Still starting LifeAgent (8s elapsed).",
        )

    assert sleeps == [0.1]
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", ["invalid", "nan", "-1", "6"])
async def test_discord_edit_rejects_invalid_or_excessive_retry_after(
    retry_after: str,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after})

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RuntimeError, match="Discord acknowledgement edit failed"):
            await DiscordWakeAckAdapter(
                token=SecretStr("discord-token"),
                allowed_channel_id="222222222222222222",
                client=client,
            ).edit_acknowledgement(
                channel_id="222222222222222222",
                acknowledgement_message_id="444444444444444444",
                content="Still starting LifeAgent (8s elapsed).",
            )

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_discord_edit_does_not_retry_ambiguous_server_error() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="upstream body must not be surfaced")

    async with httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RuntimeError, match="Discord acknowledgement edit failed"):
            await DiscordWakeAckAdapter(
                token=SecretStr("discord-token"),
                allowed_channel_id="222222222222222222",
                client=client,
            ).edit_acknowledgement(
                channel_id="222222222222222222",
                acknowledgement_message_id="444444444444444444",
                content="Still starting LifeAgent (8s elapsed).",
            )

    assert len(requests) == 1


def request_json(request: httpx.Request) -> dict[str, object]:
    import json

    loaded = json.loads(request.content.decode("utf-8"))
    assert isinstance(loaded, dict)
    return loaded
