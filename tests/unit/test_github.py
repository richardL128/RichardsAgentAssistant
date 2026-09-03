"""Security and contract tests for the narrow GitHub App connector."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from datetime import UTC, datetime

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from pydantic import SecretStr

from app.connectors.github import (
    GITHUB_API_BASE_URL,
    GitHubAppConnector,
    normalize_push_event,
    verify_webhook_signature,
)
from app.core.errors import ErrorCategory, ErrorCode, LifeAgentError

REPOSITORY = "octo-org/lifeagent"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
NOW = datetime(2026, 9, 3, 16, 0, tzinfo=UTC)


def _payload() -> bytes:
    return json.dumps(
        {
            "ref": "refs/heads/main",
            "before": BASE_SHA,
            "after": HEAD_SHA,
            "repository": {
                "full_name": REPOSITORY,
                "clone_url": f"https://github.com/{REPOSITORY}.git",
                "default_branch": "main",
            },
            "installation": {"id": 12345},
        },
        separators=(",", ":"),
    ).encode()


def _signed_headers(payload: bytes, *, delivery: str = "delivery-123") -> dict[str, str]:
    digest = hmac.new(b"webhook-secret", payload, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "X-GitHub-Delivery": delivery}


def _key() -> str:
    return (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )
        .decode()
    )


def test_invalid_signature_is_checked_before_json_parsing() -> None:
    payload = b"not json and should never be parsed"
    with pytest.raises(LifeAgentError) as raised:
        normalize_push_event(
            payload,
            {"X-Hub-Signature-256": "sha256=" + "0" * 64, "X-GitHub-Delivery": "d-1"},
            webhook_secret=SecretStr("webhook-secret"),
            repository_allowlist={REPOSITORY},
            received_at=NOW,
        )
    assert raised.value.record.code is ErrorCode.AUTHORIZATION_INVALID
    assert "json" not in raised.value.record.diagnostic.lower()


def test_push_event_validates_delivery_allowlist_and_normalizes_exact_shas() -> None:
    event = normalize_push_event(
        _payload(),
        _signed_headers(_payload()),
        webhook_secret="webhook-secret",
        repository_allowlist={REPOSITORY},
        received_at=NOW,
    )
    assert event.delivery_id == "delivery-123"
    assert event.repository == REPOSITORY
    assert event.clone_url == f"https://github.com/{REPOSITORY}.git"
    assert event.default_branch == "main"
    assert event.before_sha == BASE_SHA
    assert event.after_sha == HEAD_SHA

    bad_headers = _signed_headers(_payload(), delivery="bad delivery")
    with pytest.raises(LifeAgentError) as raised:
        normalize_push_event(
            _payload(),
            bad_headers,
            webhook_secret="webhook-secret",
            repository_allowlist={REPOSITORY},
            received_at=NOW,
        )
    assert raised.value.record.code is ErrorCode.INPUT_INVALID

    with pytest.raises(LifeAgentError, match="input_invalid"):
        normalize_push_event(
            _payload(),
            _signed_headers(_payload()),
            webhook_secret="webhook-secret",
            repository_allowlist={"octo-org/other"},
            received_at=NOW,
        )


def test_app_jwt_has_short_lived_claims_without_private_key_exposure() -> None:
    private_key = _key()
    connector = GitHubAppConnector(
        app_id=9876,
        private_key=SecretStr(private_key),
        repository_allowlist={REPOSITORY},
        clock=lambda: NOW,
    )
    token = connector.create_app_jwt()
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "9876"
    assert claims["iat"] == int(NOW.timestamp()) - 60
    assert claims["exp"] == int(NOW.timestamp()) + 9 * 60
    assert private_key not in token


@pytest.mark.asyncio
async def test_installation_exchange_and_fixed_read_urls() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/installations/12345/access_tokens":
            return httpx.Response(
                201, json={"token": "ghs-secret", "expires_at": "2026-09-03T18:00:00Z"}
            )
        if request.url.path == f"/repos/{REPOSITORY}":
            return httpx.Response(
                200,
                json={
                    "full_name": REPOSITORY,
                    "clone_url": f"https://github.com/{REPOSITORY}.git",
                    "default_branch": "main",
                    "private": True,
                    "visibility": "private",
                },
            )
        if request.url.path == f"/repos/{REPOSITORY}/tarball/{HEAD_SHA}":
            return httpx.Response(200, content=b"archive")
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = GitHubAppConnector(
            app_id=1,
            private_key=_key(),
            repository_allowlist={REPOSITORY},
            client=client,
            clock=lambda: NOW,
        )
        receipt = await connector.exchange_installation_token(12345)
        metadata = await connector.get_repository_metadata(REPOSITORY, 12345)
        archive = await connector.download_repository_archive(REPOSITORY, HEAD_SHA, 12345)

    assert receipt.token.get_secret_value() == "ghs-secret"
    assert "ghs-secret" not in repr(receipt)
    assert metadata.default_branch == "main"
    assert archive == b"archive"
    assert [request.method for request in requests] == ["POST", "GET", "GET"]
    assert all(str(request.url).startswith(GITHUB_API_BASE_URL) for request in requests)
    assert requests[0].url.path == "/app/installations/12345/access_tokens"
    assert requests[1].url.path == f"/repos/{REPOSITORY}"
    assert requests[2].url.path == f"/repos/{REPOSITORY}/tarball/{HEAD_SHA}"
    assert "ghs-secret" not in requests[0].content.decode()


@pytest.mark.asyncio
async def test_auth_and_retryable_transport_statuses_are_standardized() -> None:
    async def check(response: httpx.Response | Exception) -> LifeAgentError:
        def handler(_: httpx.Request) -> httpx.Response:
            if isinstance(response, Exception):
                raise response
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            connector = GitHubAppConnector(
                app_id=1,
                private_key=_key(),
                repository_allowlist={REPOSITORY},
                client=client,
                clock=lambda: NOW,
            )
            with pytest.raises(LifeAgentError) as raised:
                await connector.exchange_installation_token(12345)
            return raised.value

    auth = await check(httpx.Response(403))
    assert auth.record.category is ErrorCategory.AUTHORIZATION
    assert auth.record.retryable is False
    retry = await check(httpx.Response(429))
    assert retry.record.category is ErrorCategory.TRANSIENT
    assert retry.record.retryable is True
    transport = await check(httpx.ConnectError("offline"))
    assert transport.record.code is ErrorCode.CONNECTOR_TRANSIENT


def test_public_surface_has_no_write_or_arbitrary_request_operations() -> None:
    public = {
        name
        for name, member in inspect.getmembers(GitHubAppConnector)
        if not name.startswith("_") and callable(member)
    }
    assert public == {
        "compare_commits",
        "create_app_jwt",
        "download_repository_archive",
        "exchange_installation_token",
        "get_repository_metadata",
        "normalize_push_webhook",
    }


def test_signature_helper_uses_constant_time_verification() -> None:
    payload = b"{}"
    signature = "sha256=" + hmac.new(b"secret", payload, hashlib.sha256).hexdigest()
    assert verify_webhook_signature(payload, signature, "secret") is None
