"""Authenticated loopback handoff from the native Discord wake daemon."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError
from sqlalchemy.orm import Session

from app.agents.academic_planner.commands import parse_academic_command
from app.artifacts.store import ArtifactMetadata, ArtifactStore
from app.connectors.discord import (
    DiscordAcademicPlannerAdapter,
    DiscordFetchedAttachment,
    DiscordFetchedMessage,
    DiscordPdfAttachmentDownload,
)
from app.connectors.discord_gateway import (
    DiscordAcademicMessageAttachment,
    DiscordAcademicMessageCreate,
)
from app.core.errors import LifeAgentError
from app.db.academic import AcademicInboundMaterialInput, AcademicInboundMaterialRepository
from app.db.discord_wake import (
    DiscordWakeAction,
    DiscordWakeInboundInput,
    DiscordWakeNonceReplayError,
    DiscordWakeRepository,
)
from app.host.handoff import (
    DiscordHostHandoff,
    DiscordHostHandoffEvent,
    DiscordHostInteractionHandoffEvent,
    verify_handoff_signature,
)
from app.queue import tasks as queue_tasks

router = APIRouter(prefix="/internal/discord/academic", tags=["internal"])


@router.post("/handoff", include_in_schema=False)
async def accept_discord_handoff(request: Request) -> JSONResponse:
    """Authenticate, refetch, durably record, and enqueue one Discord reference."""

    settings = request.app.state.settings
    secret = settings.discord_host_handoff_secret
    if secret is None:
        raise HTTPException(status_code=503, detail="Discord host handoff is not configured")
    if request.url.hostname not in {"127.0.0.1", "localhost", "testserver"}:
        raise HTTPException(status_code=404, detail="Not found")
    body = await _bounded_body(request, settings.discord_handoff_max_body_bytes)
    signature = request.headers.get("x-lifeagent-handoff-signature", "")
    if not verify_handoff_signature(body, signature, secret):
        raise HTTPException(status_code=401, detail="Invalid handoff authentication")
    try:
        payload: object = json.loads(body)
        if not isinstance(payload, dict):
            raise TypeError
        payload = cast(dict[str, object], payload)
        event: DiscordHostHandoff
        if payload.get("version") == "discord-academic-interaction-v1":
            event = DiscordHostInteractionHandoffEvent.model_validate(payload)
        else:
            event = DiscordHostHandoffEvent.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid handoff reference") from None
    now = datetime.now(UTC)
    if abs((now - event.handoff_timestamp).total_seconds()) > (
        settings.discord_handoff_max_clock_skew_seconds
    ):
        raise HTTPException(status_code=408, detail="Stale handoff reference")
    if isinstance(event, DiscordHostInteractionHandoffEvent):
        return await _accept_interaction(request, event)

    message = await _refetch_and_validate(request, event)
    command = parse_academic_command(message.content.get_secret_value().strip())
    if event.acknowledgement_message_id is None:
        raise HTTPException(status_code=422, detail="Wake acknowledgement is required")
    await _validate_acknowledgement(request, event)

    inbound_material_ids = await _capture_pdf_attachments(request, message)
    manifest = {
        "version": "discord-academic-inbound-v2",
        "message_text": message.content.get_secret_value(),
        "inbound_material_ids": [str(item) for item in inbound_material_ids],
    }
    artifact = ArtifactStore(
        settings.artifact_root,
        default_retention_days=settings.artifact_retention_days,
    ).put(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        media_type="application/json",
        data_class="discord_inbound_manifest",
        already_redacted=True,
    )
    action = "academic_checkin"
    if command is not None:
        action = "proposal_confirmation" if command[0] == "confirm" else "proposal_rejection"
    intake = DiscordWakeInboundInput(
        discord_event_id=event.message_id,
        handoff_nonce=event.nonce,
        event_kind="message",
        action=cast(DiscordWakeAction, action),
        action_id=command[1] if command is not None else None,
        content_artifact_key=artifact.key,
        received_at=event.event_timestamp,
        discord_channel_id=event.channel_id,
        discord_user_id=event.author_id,
        discord_message_id=event.message_id,
        ack_message_id=event.acknowledgement_message_id,
    )
    return await _persist_and_enqueue(request, intake)


async def _accept_interaction(
    request: Request,
    event: DiscordHostInteractionHandoffEvent,
) -> JSONResponse:
    settings = request.app.state.settings
    channel_id = settings.discord_academic_channel_id
    authorized_users = {str(user_id) for user_id in settings.discord_academic_authorized_user_ids}
    if event.channel_id != channel_id or event.user_id not in authorized_users:
        raise HTTPException(status_code=403, detail="Discord interaction is not authorized")
    artifact = ArtifactStore(
        settings.artifact_root,
        default_retention_days=settings.artifact_retention_days,
    ).put(
        json.dumps(
            {
                "interaction_id": event.interaction_id,
                "clarification_id": str(event.clarification_id),
                "action": event.action,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        media_type="application/json",
        data_class="discord_interaction_reference",
        already_redacted=True,
    )
    intake = DiscordWakeInboundInput(
        discord_event_id=event.interaction_id,
        handoff_nonce=event.nonce,
        event_kind="interaction",
        action="agent_clarification",
        clarification_id=event.clarification_id,
        interaction_action=event.action,
        content_artifact_key=artifact.key,
        received_at=event.event_timestamp,
        discord_channel_id=event.channel_id,
        discord_user_id=event.user_id,
        discord_interaction_id=event.interaction_id,
    )
    return await _persist_and_enqueue(request, intake)


async def _persist_and_enqueue(
    request: Request,
    intake: DiscordWakeInboundInput,
) -> JSONResponse:
    try:
        with Session(request.app.state.database.engine) as session, session.begin():
            accepted = DiscordWakeRepository.accept_verified_event(session, intake)
    except (DiscordWakeNonceReplayError, ValueError):
        raise HTTPException(status_code=409, detail="Handoff replay rejected") from None
    if accepted.status == "replayed" and accepted.enqueued:
        return JSONResponse(status_code=200, content={"status": "duplicate"})
    try:
        await queue_tasks.defer_discord_wake(str(accepted.wake_id))
    except Exception:
        raise HTTPException(status_code=503, detail="Discord request could not be queued") from None
    with Session(request.app.state.database.engine) as session, session.begin():
        DiscordWakeRepository.mark_enqueued(session, accepted.wake_id)
    status_code = 202 if accepted.status == "created" else 200
    status = "accepted" if status_code == 202 else "duplicate"
    return JSONResponse(status_code=status_code, content={"status": status})


async def _bounded_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Handoff body is too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid content length") from None
    body = await request.body()
    if not body or len(body) > max_bytes:
        raise HTTPException(status_code=413, detail="Handoff body is too large")
    return body


async def _refetch_and_validate(
    request: Request,
    event: DiscordHostHandoffEvent,
) -> DiscordAcademicMessageCreate:
    settings = request.app.state.settings
    token = settings.discord_bot_token
    channel_id = settings.discord_academic_channel_id
    authorized_users = {str(user_id) for user_id in settings.discord_academic_authorized_user_ids}
    if token is None or channel_id is None or settings.discord_application_id is None:
        raise HTTPException(status_code=503, detail="Discord backend is not configured")
    if event.channel_id != channel_id or event.author_id not in authorized_users:
        raise HTTPException(status_code=403, detail="Discord reference is not authorized")
    adapter = DiscordAcademicPlannerAdapter(
        token=token,
        allowed_channel_ids={channel_id},
        base_url=settings.discord_api_url,
    )
    try:
        fetched = await adapter.fetch_message(
            channel_id=event.channel_id,
            message_id=event.message_id,
        )
    except LifeAgentError as exc:
        status_code = 503 if exc.record.retryable else 422
        raise HTTPException(
            status_code=status_code,
            detail="Discord message is unavailable",
        ) from None
    except ValueError:
        raise HTTPException(status_code=422, detail="Discord message is unavailable") from None
    if (
        fetched.id != event.message_id
        or fetched.channel_id != event.channel_id
        or fetched.author.id != event.author_id
        or fetched.author.bot
        or fetched.timestamp != event.event_timestamp
    ):
        raise HTTPException(status_code=422, detail="Discord message reference changed")
    return _to_academic_message(fetched)


async def _validate_acknowledgement(
    request: Request,
    event: DiscordHostHandoffEvent,
) -> None:
    settings = request.app.state.settings
    token = settings.discord_bot_token
    channel_id = settings.discord_academic_channel_id
    application_id = settings.discord_application_id
    acknowledgement_id = event.acknowledgement_message_id
    if token is None or channel_id is None or application_id is None or acknowledgement_id is None:
        raise HTTPException(status_code=503, detail="Discord backend is not configured")
    adapter = DiscordAcademicPlannerAdapter(
        token=token,
        allowed_channel_ids={channel_id},
        base_url=settings.discord_api_url,
    )
    try:
        await adapter.validate_wake_acknowledgement(
            channel_id=channel_id,
            message_id=acknowledgement_id,
            bot_user_id=application_id,
        )
    except LifeAgentError as exc:
        status_code = 503 if exc.record.retryable else 422
        raise HTTPException(
            status_code=status_code,
            detail="Wake acknowledgement is invalid",
        ) from None
    except ValueError:
        raise HTTPException(status_code=422, detail="Wake acknowledgement is invalid") from None


def _to_academic_message(message: DiscordFetchedMessage) -> DiscordAcademicMessageCreate:
    return DiscordAcademicMessageCreate(
        message_id=message.id,
        channel_id=message.channel_id,
        author_id=message.author.id,
        timestamp=message.timestamp,
        content=SecretStr(message.content.get_secret_value()),
        attachments=tuple(
            DiscordAcademicMessageAttachment(
                id=attachment.id,
                filename=attachment.filename,
                content_type=attachment.content_type,
                size=attachment.size,
                url=attachment.url,
            )
            for attachment in message.attachments
        ),
        mentioned_user_ids=tuple(mention.id for mention in message.mentions),
    )


async def _capture_pdf_attachments(
    request: Request,
    message: DiscordAcademicMessageCreate,
) -> tuple[UUID, ...]:
    """Capture refetched PDFs privately and persist only safe durable metadata."""

    if not message.attachments:
        return ()
    settings = request.app.state.settings
    token = settings.discord_bot_token
    if token is None:
        raise HTTPException(status_code=503, detail="Discord backend is not configured")
    adapter = DiscordAcademicPlannerAdapter(
        token=token,
        allowed_channel_ids={message.channel_id},
        base_url=settings.discord_api_url,
    )
    retention_days = max(
        settings.artifact_retention_days,
        math.ceil(settings.discord_academic_pdf_intake_ttl_hours / 24) + 1,
    )
    artifact_store = ArtifactStore(
        settings.artifact_root,
        default_retention_days=settings.artifact_retention_days,
        retention_days_by_class={"discord_academic_pdf_private": retention_days},
    )
    captured: list[tuple[DiscordPdfAttachmentDownload, ArtifactMetadata]] = []
    try:
        for attachment in message.attachments[: settings.discord_academic_pdf_max_attachments]:
            downloaded = await adapter.download_pdf_attachment(
                DiscordFetchedAttachment(
                    id=attachment.id,
                    filename=attachment.filename,
                    content_type=attachment.content_type,
                    size=attachment.size,
                    url=attachment.url,
                ),
                max_bytes=settings.discord_academic_pdf_max_bytes,
                timeout_seconds=settings.discord_academic_pdf_download_timeout_seconds,
            )
            artifact = artifact_store.put(
                downloaded.content,
                media_type="application/pdf",
                data_class="discord_academic_pdf_private",
                already_redacted=True,
            )
            captured.append((downloaded, artifact))
    except LifeAgentError as exc:
        status_code = 503 if exc.record.retryable else 422
        raise HTTPException(status_code=status_code, detail="Discord PDF capture failed") from None
    except (OSError, ValueError):
        raise HTTPException(status_code=422, detail="Discord PDF capture failed") from None

    expires_at = datetime.now(UTC) + timedelta(hours=settings.discord_academic_pdf_intake_ttl_hours)
    material_ids: list[UUID] = []
    try:
        with Session(request.app.state.database.engine) as session, session.begin():
            for downloaded, artifact in captured:
                result = AcademicInboundMaterialRepository.create_or_replay(
                    session,
                    AcademicInboundMaterialInput(
                        discord_message_id=message.message_id,
                        discord_attachment_id=downloaded.attachment_id,
                        owner_discord_user_id=message.author_id,
                        discord_channel_id=message.channel_id,
                        filename=downloaded.filename,
                        media_type=downloaded.content_type or "application/pdf",
                        declared_byte_size=downloaded.declared_size,
                        observed_byte_size=downloaded.observed_size,
                        content_hash=downloaded.sha256_hex,
                        raw_artifact_key=artifact.key,
                        captured_at=datetime.now(UTC),
                        expires_at=expires_at,
                    ),
                )
                material_ids.append(result.row.id)
    except (ValueError, OSError):
        raise HTTPException(
            status_code=422, detail="Discord PDF intake could not be stored"
        ) from None
    return tuple(material_ids)


__all__ = ["accept_discord_handoff", "router"]
