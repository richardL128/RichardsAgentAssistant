from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from app.host.handoff import (
    DiscordHostHandoffClient,
    DiscordHostHandoffEvent,
    HandoffRejectedError,
    canonical_handoff_body,
    handoff_nonce,
    sign_handoff_body,
    verify_handoff_signature,
)


def _event() -> DiscordHostHandoffEvent:
    return DiscordHostHandoffEvent(
        message_id="111111111111111111",
        channel_id="222222222222222222",
        author_id="333333333333333333",
        event_timestamp=datetime(2026, 9, 9, tzinfo=UTC),
        acknowledgement_message_id="444444444444444444",
        handoff_timestamp=datetime(2026, 9, 9, 1, tzinfo=UTC),
        nonce=handoff_nonce("111111111111111111"),
    )


def test_handoff_body_is_canonical_reference_only_and_signed() -> None:
    secret = SecretStr("handoff-secret")
    body = canonical_handoff_body(_event())
    signature = sign_handoff_body(body, secret)

    assert b"content" not in body
    assert b"token" not in body
    assert signature.startswith("sha256=")
    assert verify_handoff_signature(body, signature, secret)
    assert not verify_handoff_signature(body, signature, SecretStr("wrong"))


@pytest.mark.asyncio
async def test_handoff_client_posts_signed_body_and_accepts_202() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"status": "accepted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DiscordHostHandoffClient(
            endpoint_url="http://127.0.0.1:8000/internal/discord/academic/handoff",
            secret=SecretStr("handoff-secret"),
            client=client,
        ).submit(_event())

    assert result.status == "accepted"
    assert requests[0].headers["x-lifeagent-handoff-signature"].startswith("sha256=")
    assert requests[0].content == canonical_handoff_body(_event())


@pytest.mark.asyncio
async def test_handoff_client_rejects_non_loopback_and_auth_failure() -> None:
    with pytest.raises(ValueError, match="localhost-only"):
        DiscordHostHandoffClient(
            endpoint_url="http://example.com/handoff",
            secret=SecretStr("handoff-secret"),
        )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HandoffRejectedError) as raised:
            await DiscordHostHandoffClient(
                endpoint_url="http://127.0.0.1:8000/internal/discord/academic/handoff",
                secret=SecretStr("handoff-secret"),
                client=client,
            ).submit(_event())

    assert raised.value.code == "handoff_auth"


@pytest.mark.asyncio
async def test_handoff_client_does_not_treat_nonce_conflict_as_duplicate() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(409)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HandoffRejectedError) as raised:
            await DiscordHostHandoffClient(
                endpoint_url="http://127.0.0.1:8000/internal/discord/academic/handoff",
                secret=SecretStr("handoff-secret"),
                client=client,
            ).submit(_event())

    assert raised.value.code == "handoff_rejected"
