"""Authenticated loopback handoff from the native Discord wake daemon."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr, ValidationError
from sqlalchemy.orm import Session

from app.agents.academic_planner.commands import (
    is_discord_abort_command,
    parse_academic_command,
)
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
from app.db.academic import (
    AcademicInboundMaterialInput,
    AcademicInboundMaterialRepository,
    AcademicRepository,
)
from app.db.discord_wake import (
    DiscordAbortRequestRecord,
    DiscordWakeAbortRequestResult,
    DiscordWakeAbortStatusSnapshot,
    DiscordWakeAction,
    DiscordWakeInboundInput,
    DiscordWakeNonceReplayError,
    DiscordWakeRepository,
)
from app.host.handoff import (
    DiscordHostAbortEvent,
    DiscordHostAbortReceipt,
    DiscordHostHandoff,
    DiscordHostHandoffEvent,
    DiscordHostInteractionHandoffEvent,
    verify_handoff_signature,
)
from app.queue import tasks as queue_tasks

router = APIRouter(prefix="/internal/discord/academic", tags=["internal"])

_SAFE_TOOL_ACTIVITY_LABELS = {
    "archive_assessment": "proposal_drafting",
    "attach_material_to_assessment": "proposal_drafting",
    "create_assessment": "proposal_drafting",
    "create_course_event": "proposal_drafting",
    "create_misc_task": "proposal_drafting",
    "find_course_event_slots": "availability_data",
    "inspect_inbound_pdf": "assessment_data",
    "manage_academic_memory": "memory_data",
    "prepare_job_interview": "interview_preparation",
    "propose_interview_date": "proposal_drafting",
    "propose_interview_plan_save": "proposal_drafting",
    "search_assessment_materials": "assessment_data",
    "search_assessments": "assessment_data",
    "search_courses": "course_data",
    "search_job_interviews": "interview_data",
    "search_jobs_context": "interview_data",
    "search_pending_assessment_creates": "assessment_data",
    "update_assessment": "proposal_drafting",
}


@router.post("/abort", include_in_schema=False)
async def accept_discord_abort(request: Request) -> JSONResponse:
    """Authenticate and interrupt earlier owner/channel Discord turns."""

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
        event = DiscordHostAbortEvent.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid abort reference") from None
    now = datetime.now(UTC)
    if abs((now - event.handoff_timestamp).total_seconds()) > (
        settings.discord_handoff_max_clock_skew_seconds
    ):
        raise HTTPException(status_code=408, detail="Stale abort reference")

    await _refetch_and_validate_abort(request, event)
    await _validate_acknowledgement(request, event)

    with Session(request.app.state.database.engine) as session, session.begin():
        abort_record = DiscordWakeRepository.record_abort_request(
            session,
            abort_event_id=event.abort_message_id,
            handoff_nonce=event.nonce,
            channel_id=event.channel_id,
            user_id=event.author_id,
            ack_message_id=event.acknowledgement_message_id,
            received_at=event.event_timestamp,
        )
        if not abort_record.created and abort_record.status != "processing":
            receipt = _recorded_abort_receipt(abort_record)
            return JSONResponse(status_code=200, content=receipt.model_dump(mode="json"))
        requested = DiscordWakeRepository.request_abort_for_scope(
            session,
            channel_id=event.channel_id,
            user_id=event.author_id,
            abort_event_id=event.abort_message_id,
            abort_received_at=event.event_timestamp,
        )
        AcademicRepository.abort_owner_channel_continuations(
            session,
            discord_channel_id=event.channel_id,
            discord_user_id=event.author_id,
            abort_event_id=event.abort_message_id,
            aborted_at=event.event_timestamp,
        )
        target_ids = tuple(target.wake_id for target in requested.targets)
        terminally_aborted_ids: list[UUID] = []
        for target in requested.targets:
            if target.queue_job_id is None:
                DiscordWakeRepository.mark_aborted(
                    session,
                    target.wake_id,
                    abort_event_id=event.abort_message_id,
                )
                terminally_aborted_ids.append(target.wake_id)

    infrastructure_aborted_ids: list[UUID] = []
    for target in requested.targets:
        if target.queue_job_id is None or target.state != "abort_requested":
            continue
        cancelled = await queue_tasks.procrastinate_app.job_manager.cancel_job_by_id_async(
            target.queue_job_id,
            abort=True,
        )
        infrastructure_status: str | None = None
        if not cancelled:
            job_status = await queue_tasks.procrastinate_app.job_manager.get_job_status_async(
                target.queue_job_id
            )
            infrastructure_status = getattr(job_status, "value", str(job_status))
        if (cancelled and target.prior_state == "queued") or infrastructure_status in {
            "cancelled",
            "aborted",
        }:
            infrastructure_aborted_ids.append(target.wake_id)

    if infrastructure_aborted_ids:
        with Session(request.app.state.database.engine) as session, session.begin():
            for wake_id in infrastructure_aborted_ids:
                DiscordWakeRepository.mark_queue_cancelled_aborted(
                    session,
                    wake_id,
                    abort_event_id=event.abort_message_id,
                )
        terminally_aborted_ids.extend(infrastructure_aborted_ids)

    snapshot = await _wait_for_abort_status(request, target_ids)
    await _edit_cancelled_progress(request, tuple(dict.fromkeys(terminally_aborted_ids)))
    receipt = _abort_receipt(requested, snapshot)
    with Session(request.app.state.database.engine) as session, session.begin():
        DiscordWakeRepository.finalize_abort_request(
            session,
            abort_event_id=event.abort_message_id,
            status="accepted" if receipt.status == "duplicate" else receipt.status,
            target_count=receipt.target_count,
            running_count=receipt.running_count,
            queued_count=receipt.queued_count,
            safe_activity_label=receipt.safe_activity_label,
            safe_tool_status=receipt.safe_tool_status,
        )
    return JSONResponse(status_code=200, content=receipt.model_dump(mode="json"))


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
    if is_discord_abort_command(message.content.get_secret_value()):
        raise HTTPException(status_code=422, detail="ABORT requires the abort handoff")
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
    with Session(request.app.state.database.engine) as session, session.begin():
        enqueue = DiscordWakeRepository.enqueue_decision(session, accepted.wake_id)
    if not enqueue.should_enqueue:
        if enqueue.state == "aborted":
            await _edit_cancelled_progress(request, (accepted.wake_id,))
        return JSONResponse(status_code=200, content={"status": "duplicate"})
    try:
        queue_job_id = await queue_tasks.defer_discord_wake(str(accepted.wake_id))
    except Exception:
        raise HTTPException(status_code=503, detail="Discord request could not be queued") from None
    with Session(request.app.state.database.engine) as session, session.begin():
        bound = DiscordWakeRepository.bind_queue_job_id(
            session,
            accepted.wake_id,
            queue_job_id,
        )
    if bound.should_cancel_queue_job and bound.queue_job_id is not None:
        cancelled = await queue_tasks.procrastinate_app.job_manager.cancel_job_by_id_async(
            bound.queue_job_id,
            abort=True,
        )
        if cancelled:
            with Session(request.app.state.database.engine) as session, session.begin():
                DiscordWakeRepository.mark_queue_cancelled_aborted(
                    session,
                    accepted.wake_id,
                )
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
    event: DiscordHostHandoffEvent | DiscordHostAbortEvent,
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


async def _refetch_and_validate_abort(
    request: Request,
    event: DiscordHostAbortEvent,
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
            message_id=event.abort_message_id,
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
        fetched.id != event.abort_message_id
        or fetched.channel_id != event.channel_id
        or fetched.author.id != event.author_id
        or fetched.author.bot
        or fetched.timestamp != event.event_timestamp
        or not is_discord_abort_command(fetched.content.get_secret_value())
    ):
        raise HTTPException(status_code=422, detail="Discord abort reference changed")
    return _to_academic_message(fetched)


async def _wait_for_abort_status(
    request: Request,
    wake_ids: tuple[UUID, ...],
) -> DiscordWakeAbortStatusSnapshot:
    timeout = request.app.state.settings.discord_abort_wait_timeout_seconds
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        with Session(request.app.state.database.engine) as session:
            snapshot = DiscordWakeRepository.abort_status_snapshot(session, wake_ids)
        if snapshot.abort_requested_count == 0:
            return snapshot
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return snapshot
        await asyncio.sleep(min(0.05, remaining))


def _abort_receipt(
    requested: DiscordWakeAbortRequestResult,
    snapshot: DiscordWakeAbortStatusSnapshot,
) -> DiscordHostAbortReceipt:
    running_count = sum(target.prior_state == "running" for target in requested.targets)
    queued_count = sum(target.prior_state == "queued" for target in requested.targets)
    if snapshot.total_count == 0:
        status = "no_active"
    elif snapshot.abort_requested_count:
        status = "unconfirmed"
    elif requested.newly_requested_count == 0:
        status = "duplicate"
    else:
        status = "accepted"

    activity = snapshot.activity
    activity_label: str | None = None
    if activity is not None:
        if activity.activity_tool_name is not None:
            safe_tool_activity = _SAFE_TOOL_ACTIVITY_LABELS.get(activity.activity_tool_name)
            if safe_tool_activity is not None:
                activity_label = f"tool activity: {safe_tool_activity}"
        elif activity.activity_phase is not None:
            activity_label = {
                "accepted": "queue handoff",
                "queued": "queue handoff",
                "abort_requested": "turn shutdown",
                "terminal": "queue handoff" if requested.running_count == 0 else "turn shutdown",
            }.get(
                activity.activity_phase,
                activity.activity_phase.replace("_", " "),
            )
        if activity_label is not None:
            activity_label = activity_label[:80]

    if snapshot.completed_count:
        tool_status = "completed_before_cancel"
    elif snapshot.abort_requested_count:
        if (
            activity is not None
            and activity.activity_side_effect_class in {"durable_local_write", "external_write"}
            and activity.activity_tool_status in {"running", "cancellation_requested", "unknown"}
        ):
            tool_status = "unknown"
        else:
            tool_status = "cancellation_requested"
    elif snapshot.aborted_count:
        if (
            activity is not None
            and activity.activity_side_effect_class in {"durable_local_write", "external_write"}
            and activity.activity_tool_status not in {"succeeded", "failed", "cancelled"}
        ):
            tool_status = "unknown"
        else:
            tool_status = "cancelled"
    else:
        tool_status = "none"

    return DiscordHostAbortReceipt(
        status=status,
        target_count=min(snapshot.total_count, 100),
        running_count=min(running_count, 100),
        queued_count=min(queued_count, 100),
        safe_activity_label=activity_label,
        safe_tool_status=tool_status,
    )


def _recorded_abort_receipt(record: DiscordAbortRequestRecord) -> DiscordHostAbortReceipt:
    if record.status == "processing":
        raise ValueError("Discord abort receipt is still processing")
    return DiscordHostAbortReceipt(
        status="duplicate" if record.status == "accepted" else record.status,
        target_count=record.target_count,
        running_count=record.running_count,
        queued_count=record.queued_count,
        safe_activity_label=record.safe_activity_label,
        safe_tool_status=record.safe_tool_status,
    )


async def _edit_cancelled_progress(request: Request, wake_ids: tuple[UUID, ...]) -> None:
    if not wake_ids:
        return
    settings = request.app.state.settings
    token = settings.discord_bot_token
    channel_id = settings.discord_academic_channel_id
    if token is None or channel_id is None:
        return
    with Session(request.app.state.database.engine) as session:
        acknowledgement_ids = tuple(
            row.ack_message_id
            for wake_id in wake_ids
            if (row := DiscordWakeRepository.get_by_id(session, wake_id)) is not None
            and row.ack_message_id is not None
        )
    if not acknowledgement_ids:
        return
    adapter = DiscordAcademicPlannerAdapter(
        token=token,
        allowed_channel_ids={channel_id},
        base_url=settings.discord_api_url,
    )

    async def edit(message_id: str) -> None:
        try:
            await adapter.edit_academic_message(
                channel_id=channel_id,
                message_id=message_id,
                content="Aborted. I stopped this Discord turn before it could continue.",
            )
        except Exception:
            return

    try:
        async with asyncio.timeout(min(1.0, settings.discord_abort_wait_timeout_seconds)):
            await asyncio.gather(*(edit(message_id) for message_id in acknowledgement_ids))
    except TimeoutError:
        return


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


__all__ = ["accept_discord_abort", "accept_discord_handoff", "router"]
