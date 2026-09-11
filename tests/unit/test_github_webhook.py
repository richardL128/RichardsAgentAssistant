"""End-to-end unit tests for signed GitHub webhook intake."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.github import router as github_router
from app.core.config import Settings
from app.db.models import AgentRun, Base, ReviewedCommit
from app.main import create_app

REPOSITORY = "octo-org/lifeagent"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _payload(*, modified: list[str] | None = None) -> bytes:
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
            "commits": [{"added": [], "modified": modified or [], "removed": []}],
        },
        separators=(",", ":"),
    ).encode()


def _headers(body: bytes, delivery: str) -> dict[str, str]:
    digest = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    return {
        "X-GitHub-Delivery": delivery,
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": f"sha256={digest}",
        "Content-Type": "application/json",
    }


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'webhook.db'}",
        artifact_root=tmp_path / "artifacts",
        github_app_id=1,
        github_installation_id=12345,
        github_private_key=SecretStr("configured-but-not-used-by-intake"),
        github_webhook_secret=SecretStr("webhook-secret"),
        repository_allowlist=[REPOSITORY],
        repository_allowlist_version="fixture-v1",
    )


def _app(settings: Settings):
    """Mount the planned webhook explicitly; the configured app leaves it disabled."""

    application = create_app(settings)
    application.include_router(github_router)
    return application


@pytest.mark.asyncio
async def test_signed_push_is_durable_and_repository_sha_replay_is_deduplicated(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    app = _app(settings)
    Base.metadata.create_all(app.state.database.engine)
    queued: list[tuple[str, str]] = []

    async def enqueue(run_id: str, key: str) -> int:
        queued.append((run_id, key))
        return 1

    app.state.enqueue_code_review = enqueue
    body = _payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post("/webhooks/github", content=body, headers=_headers(body, "d-1"))
        replay = await client.post("/webhooks/github", content=body, headers=_headers(body, "d-2"))

    assert first.status_code == 202
    assert replay.status_code == 202
    assert first.json()["status"] == "accepted"
    assert replay.json()["status"] == "duplicate"
    assert first.json()["run_id"] == replay.json()["run_id"]
    assert queued[0] == queued[1]
    assert queued[0][1] == f"code-review:{REPOSITORY}:{HEAD_SHA}"
    with Session(app.state.database.engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
        assert session.scalar(select(func.count()).select_from(ReviewedCommit)) == 1


@pytest.mark.asyncio
async def test_signature_failure_and_oversize_body_never_create_a_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"github_webhook_max_body_bytes": 64})
    app = _app(settings)
    Base.metadata.create_all(app.state.database.engine)
    app.state.enqueue_code_review = pytest.fail
    body = _payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        oversized = await client.post(
            "/webhooks/github", content=body, headers=_headers(body, "d-big")
        )
        invalid = await client.post(
            "/webhooks/github",
            content=b"{}",
            headers={
                "X-GitHub-Delivery": "d-bad",
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
            },
        )

    assert oversized.status_code == 413
    assert invalid.status_code == 401
    with Session(app.state.database.engine) as session:
        assert session.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert session.scalar(select(func.count()).select_from(ReviewedCommit)) == 0


@pytest.mark.asyncio
async def test_unconfigured_endpoint_fails_closed(tmp_path: Path) -> None:
    app = _app(
        Settings(
            database_url=f"sqlite+pysqlite:///{tmp_path / 'unconfigured.db'}",
            artifact_root=tmp_path,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/webhooks/github", content=b"{}")
    assert response.status_code == 503
    assert response.json()["error_code"] == "github_not_configured"


@pytest.mark.asyncio
async def test_high_risk_push_is_durably_marked_for_quick_scan(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = _app(settings)
    Base.metadata.create_all(app.state.database.engine)

    async def enqueue(_: str, __: str) -> int:
        return 1

    app.state.enqueue_code_review = enqueue
    body = _payload(modified=["app/auth/session.py"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhooks/github", content=body, headers=_headers(body, "d-high")
        )

    assert response.status_code == 202
    with Session(app.state.database.engine) as session:
        commit = session.scalar(select(ReviewedCommit))
        assert commit is not None
        assert commit.trigger == "quick_scan"
        assert commit.risk == "high"
