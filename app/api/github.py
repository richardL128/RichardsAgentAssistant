"""Signed, bounded GitHub webhook intake for Phase 3 code review."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.agents.code_review.contracts import PushEvent, validate_repo_path
from app.agents.code_review.risk import classify_risk
from app.connectors.github import normalize_push_event, verify_webhook_signature
from app.core.errors import ErrorCategory, LifeAgentError
from app.db.code_review import CodeReviewRepository, ReviewIntake

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class GitHubWebhookResponse(BaseModel):
    """Public response containing durable identifiers and no source content."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted", "duplicate", "ignored"]
    run_id: str | None = None
    reviewed_commit_id: str | None = None


async def _bounded_body(request: Request, maximum_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            return b""
        if declared < 0 or declared > maximum_bytes:
            raise _PayloadTooLargeError
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise _PayloadTooLargeError
        body.extend(chunk)
    return bytes(body)


class _PayloadTooLargeError(Exception):
    pass


def _push_policy(
    body: bytes, *, quick_scan_enabled: bool
) -> tuple[Literal["push", "quick_scan"], Literal["high", "medium", "low"]]:
    """Classify signed webhook path metadata without trusting it as source content."""

    paths: set[str] = set()
    try:
        payload_value: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "push", "medium"
    if not isinstance(payload_value, dict):
        return "push", "medium"
    payload = cast(dict[str, Any], payload_value)
    commits_value = payload.get("commits", [])
    if isinstance(commits_value, list):
        commits = cast(list[Any], commits_value)
        for commit_value in commits[:250]:
            if not isinstance(commit_value, dict):
                continue
            commit = cast(dict[str, Any], commit_value)
            for field in ("added", "modified", "removed"):
                values_value = commit.get(field, [])
                if not isinstance(values_value, list):
                    continue
                values = cast(list[Any], values_value)
                for value in values[:500]:
                    if not isinstance(value, str):
                        continue
                    try:
                        paths.add(validate_repo_path(value))
                    except ValueError:
                        continue
    risk, _ = classify_risk(sorted(paths))
    if quick_scan_enabled and risk.value == "high":
        return "quick_scan", "high"
    return "push", risk.value


def _accept_push(
    request: Request,
    event: PushEvent,
    *,
    trigger: Literal["push", "quick_scan"],
    risk: Literal["high", "medium", "low"],
) -> ReviewIntake:
    settings = request.app.state.settings
    with Session(request.app.state.database.engine) as session, session.begin():
        return CodeReviewRepository.accept_push(
            session,
            event=event,
            allowlist_version=settings.repository_allowlist_version,
            model_version=request.app.state.model_identity,
            config_version=request.app.state.model_config_version,
            trigger=trigger,
            risk=risk,
        )


@router.post("/github", response_model=GitHubWebhookResponse, status_code=202)
async def github_webhook(request: Request) -> GitHubWebhookResponse | JSONResponse:
    """Authenticate a push, persist its repository/SHA identity, then enqueue it."""

    settings = request.app.state.settings
    if (
        settings.github_webhook_secret is None
        or settings.github_app_id is None
        or settings.github_installation_id is None
        or settings.github_private_key is None
        or not settings.repository_allowlist
        or settings.repository_allowlist_version is None
    ):
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "error_code": "github_not_configured"},
        )
    try:
        body = await _bounded_body(request, settings.github_webhook_max_body_bytes)
    except _PayloadTooLargeError:
        return JSONResponse(
            status_code=413,
            content={"status": "rejected", "error_code": "payload_too_large"},
        )

    try:
        # Verify before dispatching on event type or decoding any JSON.
        verify_webhook_signature(
            body,
            request.headers.get("x-hub-signature-256"),
            settings.github_webhook_secret,
        )
        delivery_id = request.headers.get("x-github-delivery")
        if delivery_id is None or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", delivery_id) is None:
            return JSONResponse(
                status_code=400,
                content={"status": "rejected", "error_code": "input_invalid"},
            )
        if request.headers.get("x-github-event") != "push":
            return GitHubWebhookResponse(status="ignored")
        event = normalize_push_event(
            body,
            request.headers,
            webhook_secret=settings.github_webhook_secret,
            repository_allowlist=settings.repository_allowlist,
            received_at=datetime.now(UTC),
        )
        if event.installation_id != settings.github_installation_id:
            return JSONResponse(
                status_code=403,
                content={"status": "rejected", "error_code": "installation_not_allowed"},
            )
        # Deleted refs do not identify a commit that can be reviewed.
        if set(event.after_sha) == {"0"}:
            return GitHubWebhookResponse(status="ignored")
        trigger, risk = _push_policy(
            body, quick_scan_enabled=settings.code_review_quick_scan_enabled
        )
        intake = await asyncio.to_thread(
            _accept_push,
            request,
            event,
            trigger=trigger,
            risk=risk,
        )
    except LifeAgentError as exc:
        status_code = 401 if exc.record.category is ErrorCategory.AUTHORIZATION else 400
        return JSONResponse(
            status_code=status_code,
            content={"status": "rejected", "error_code": exc.record.code.value},
        )
    except (PermissionError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"status": "rejected", "error_code": "input_invalid"},
        )

    if intake.status not in {"succeeded", "attention", "failed", "cancelled"}:
        enqueue: Callable[[str, str], Awaitable[object]] = request.app.state.enqueue_code_review
        try:
            await enqueue(str(intake.run_id), intake.idempotency_key)
        except Exception:
            # The durable queued record remains replayable. Never expose driver,
            # credential, or queue exception details to the webhook caller.
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "error_code": "queue_unavailable"},
            )
    return GitHubWebhookResponse(
        status="accepted" if intake.created else "duplicate",
        run_id=str(intake.run_id),
        reviewed_commit_id=str(intake.reviewed_commit_id),
    )


__all__ = ["GitHubWebhookResponse", "github_webhook", "router"]
