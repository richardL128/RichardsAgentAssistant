from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.connectors.learn_bridge import (
    LearnBridgeConnector,
    LearnBridgeError,
    _response_signature,
)

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
SECRET = "learn-secret-" + "s" * 32


def _signed_headers(request: httpx.Request, body: bytes) -> dict[str, str]:
    timestamp = request.headers["x-lifeagent-learn-timestamp"]
    nonce = "response-nonce-1234567890"
    return {
        "x-lifeagent-learn-timestamp": timestamp,
        "x-lifeagent-learn-nonce": nonce,
        "x-lifeagent-learn-signature": _response_signature(
            secret=SECRET,
            status_code=200,
            path=request.url.path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        ),
    }


@pytest.mark.asyncio
async def test_learn_bridge_signs_snapshot_request_and_verifies_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.dumps(
            {
                "courses": [
                    {
                        "org_unit_id": "course-1",
                        "code": "ECE 240",
                        "name": "Electronic Circuits",
                        "term": "Fall 2026",
                        "active": True,
                        "url": "https://learn.uwaterloo.ca/d2l/home/course-1",
                    }
                ],
                "scheduled_items": [],
                "announcements": [],
                "generated_at": NOW.isoformat(),
            },
            separators=(",", ":"),
        ).encode()
        return httpx.Response(200, content=body, headers=_signed_headers(request, body))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = LearnBridgeConnector(
            base_url="http://127.0.0.1:8123",
            hmac_secret=SECRET,
            client=client,
            clock=lambda: NOW,
            nonce_factory=lambda: "request-nonce-1234567890",
        )
        snapshot = await connector.snapshot(start_at=NOW, end_at=NOW)

    assert snapshot.courses[0].code == "ECE 240"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/snapshot"
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers
    assert request.headers["x-lifeagent-learn-signature"].startswith("sha256=")
    assert json.loads(request.content)["announcement_limit"] == 250


@pytest.mark.asyncio
async def test_learn_bridge_rejects_unsigned_or_forbidden_payloads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.dumps(
            {
                "courses": [],
                "scheduled_items": [],
                "announcements": [],
                "generated_at": NOW.isoformat(),
                "cookies": "must-not-cross",
            },
            separators=(",", ":"),
        ).encode()
        return httpx.Response(200, content=body, headers=_signed_headers(request, body))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = LearnBridgeConnector(
            base_url="http://localhost:8123",
            hmac_secret=SECRET,
            client=client,
            clock=lambda: NOW,
            nonce_factory=lambda: "request-nonce-1234567890",
        )
        with pytest.raises(LearnBridgeError, match="snapshot payload was invalid"):
            await connector.snapshot(start_at=NOW, end_at=NOW)


def test_learn_bridge_requires_host_local_http_url() -> None:
    with pytest.raises(ValueError, match="host-local"):
        LearnBridgeConnector(base_url="http://learn.uwaterloo.ca", hmac_secret=SECRET)
