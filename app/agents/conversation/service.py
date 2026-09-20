"""Service layer for durable native conversation sessions."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, messages_to_dict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.conversation.contracts import (
    NativeConversationBeginResult,
    NativeConversationBeginStatus,
    NativeConversationDisposition,
    NativeLifecycleEvent,
    NativeToolCheckpointManifest,
    NativeTranscriptManifest,
)
from app.artifacts.store import ArtifactMetadata, ArtifactStore
from app.db.conversation import NativeConversationRepository

_TRANSCRIPT_DATA_CLASS = "native_conversation_transcript"
_CHECKPOINT_DATA_CLASS = "native_tool_checkpoint"
_MEDIA_TYPE = "application/json"
_MAX_TRANSCRIPT_BYTES = 1_048_576
_MAX_CHECKPOINT_BYTES = 262_144
_MAX_SUMMARY_BYTES = 32_768
_EXPIRED_RESPONSE = "That conversation expired. Please resend the complete request."


class NativeConversationCorruptionError(ValueError):
    """Raised after a corrupt conversation artifact has been failed closed."""


@dataclass(frozen=True, slots=True)
class NativeCompactionRecord:
    id: uuid.UUID
    summary_artifact_key: str
    covered_through_message_index: int
    source_fingerprint: str


class NativeConversationService:
    """Durable, artifact-backed session manager for native agent conversations."""

    def __init__(
        self,
        *,
        engine: Any,
        artifact_store: ArtifactStore,
        session_ttl_hours: int = 24,
        clock: Any | None = None,
    ) -> None:
        if session_ttl_hours < 1 or session_ttl_hours > 168:
            raise ValueError("session_ttl_hours must be between 1 and 168")
        self._engine = engine
        self._artifacts = artifact_store
        self._session_ttl_hours = session_ttl_hours
        self._clock = clock or (lambda: datetime.now(UTC))

    def begin_turn(
        self,
        *,
        external_event_id: str,
        discord_channel_id: str,
        owner_discord_user_id: str,
        content: str,
        model_identity: str | None,
        prompt_config_version: str | None,
        now: datetime | None = None,
    ) -> NativeConversationBeginResult:
        """Create or resume an owner/channel conversation and append the user message."""

        current = _aware(now or self._clock())
        user_message = HumanMessage(content=content)
        with Session(self._engine) as session, session.begin():
            expired_owner_row = NativeConversationRepository.expire_open_session_for_owner(
                session,
                discord_channel_id=discord_channel_id,
                owner_discord_user_id=owner_discord_user_id,
                now=current,
            )
            NativeConversationRepository.expire_open_sessions(session, now=current)
            replay = NativeConversationRepository.lock_by_event_id(session, external_event_id)
            if replay is not None:
                row, _event = replay
                try:
                    transcript = self._load_transcript_for_row(session, row.id)
                    checkpoint = self._load_checkpoint_for_row(row, session=session)
                except NativeConversationCorruptionError:
                    return NativeConversationBeginResult(
                        status="corrupt",
                        session_id=row.id,
                        root_event_id=row.root_event_id,
                        state="failed",
                        revision=row.revision,
                        response="I could not safely resume that conversation. Please start again.",
                        duplicate=True,
                    )
                if row.state == "expired":
                    return NativeConversationBeginResult(
                        status="expired",
                        session_id=row.id,
                        root_event_id=row.root_event_id,
                        state=row.state,
                        revision=row.revision,
                        transcript_messages=transcript.to_messages(),
                        checkpoint=checkpoint,
                        response=_EXPIRED_RESPONSE,
                        duplicate=True,
                    )
                if row.state == "processing":
                    return NativeConversationBeginResult(
                        status="resumed",
                        session_id=row.id,
                        root_event_id=row.root_event_id,
                        state=row.state,
                        revision=row.revision,
                        transcript_messages=transcript.to_messages(),
                        checkpoint=checkpoint,
                        duplicate=True,
                    )
                return NativeConversationBeginResult(
                    status="duplicate",
                    session_id=row.id,
                    root_event_id=row.root_event_id,
                    state=row.state,
                    revision=row.revision,
                    transcript_messages=transcript.to_messages(),
                    checkpoint=checkpoint,
                    response=_last_lifecycle_content(transcript),
                    duplicate=True,
                )

            row = NativeConversationRepository.lock_open_for_owner(
                session,
                discord_channel_id=discord_channel_id,
                owner_discord_user_id=owner_discord_user_id,
                now=current,
            )
            if row is not None and row.state == "processing":
                return NativeConversationBeginResult(
                    status="in_progress",
                    session_id=row.id,
                    root_event_id=row.root_event_id,
                    state=row.state,
                    revision=row.revision,
                    response="I am still working on the current conversation.",
                )
            if row is None:
                if expired_owner_row is not None:
                    NativeConversationRepository.record_inbound_event(
                        session,
                        conversation_id=expired_owner_row.id,
                        external_event_id=external_event_id,
                        received_at=current,
                    )
                    return NativeConversationBeginResult(
                        status="expired",
                        session_id=expired_owner_row.id,
                        root_event_id=expired_owner_row.root_event_id,
                        state=expired_owner_row.state,
                        revision=expired_owner_row.revision,
                        response=_EXPIRED_RESPONSE,
                    )
                transcript = NativeTranscriptManifest.from_messages(
                    (user_message,),
                    revision=1,
                )
                transcript_key = self._store_transcript(transcript).key
                try:
                    row = NativeConversationRepository.create_session(
                        session,
                        root_event_id=external_event_id,
                        discord_channel_id=discord_channel_id,
                        owner_discord_user_id=owner_discord_user_id,
                        transcript_artifact_key=transcript_key,
                        started_at=current,
                        expires_at=current + timedelta(hours=self._session_ttl_hours),
                        model_identity=model_identity,
                        prompt_config_version=prompt_config_version,
                    )
                except IntegrityError:
                    # A different inbound event may have won the partial-unique
                    # owner/channel race after our initial locked lookup. Coalesce
                    # it into the already-processing conversation instead of
                    # leaking a database exception to Discord.
                    winner = NativeConversationRepository.lock_open_for_owner(
                        session,
                        discord_channel_id=discord_channel_id,
                        owner_discord_user_id=owner_discord_user_id,
                        now=current,
                    )
                    if winner is None:
                        raise
                    return NativeConversationBeginResult(
                        status="in_progress",
                        session_id=winner.id,
                        root_event_id=winner.root_event_id,
                        state=winner.state,
                        revision=winner.revision,
                        response="I am still working on the current conversation.",
                    )
                NativeConversationRepository.record_inbound_event(
                    session,
                    conversation_id=row.id,
                    external_event_id=external_event_id,
                    received_at=current,
                )
                transcript = transcript.model_copy(update={"conversation_id": row.id})
                transcript_key = self._store_transcript(transcript).key
                row = NativeConversationRepository.update_artifacts(
                    session,
                    conversation_id=row.id,
                    transcript_artifact_key=transcript_key,
                    now=current,
                    state="processing",
                )
                return NativeConversationBeginResult(
                    status="started",
                    session_id=row.id,
                    root_event_id=row.root_event_id,
                    state=row.state,
                    revision=row.revision,
                    transcript_messages=transcript.to_messages(),
                    checkpoint={},
                )

            try:
                transcript = self._load_transcript_for_row(session, row.id)
                checkpoint = self._load_checkpoint_for_row(row, session=session)
            except NativeConversationCorruptionError:
                return NativeConversationBeginResult(
                    status="corrupt",
                    session_id=row.id,
                    root_event_id=row.root_event_id,
                    state="failed",
                    revision=row.revision,
                    response="I could not safely resume that conversation. Please start again.",
                )
            event = NativeConversationRepository.record_inbound_event(
                session,
                conversation_id=row.id,
                external_event_id=external_event_id,
                received_at=current,
            )
            if not event.created:
                status = "resumed" if row.state == "processing" else "duplicate"
                return NativeConversationBeginResult(
                    status=status,
                    session_id=row.id,
                    root_event_id=row.root_event_id,
                    state=row.state,
                    revision=row.revision,
                    transcript_messages=transcript.to_messages(),
                    checkpoint=checkpoint,
                    response=(
                        None if row.state == "processing" else _last_lifecycle_content(transcript)
                    ),
                    duplicate=True,
                )
            next_revision = row.revision + 1
            transcript = transcript.with_appended_messages((user_message,), revision=next_revision)
            transcript_key = self._store_transcript(transcript).key
            row = NativeConversationRepository.update_artifacts(
                session,
                conversation_id=row.id,
                transcript_artifact_key=transcript_key,
                now=current,
                state="processing",
            )
            return NativeConversationBeginResult(
                status="resumed",
                session_id=row.id,
                root_event_id=row.root_event_id,
                state=row.state,
                revision=row.revision,
                transcript_messages=transcript.to_messages(),
                checkpoint=checkpoint,
            )

    def open_proactive_prompt(
        self,
        *,
        root_event_id: str,
        discord_channel_id: str,
        owner_discord_user_id: str,
        prompt_text: str,
        expires_at: datetime,
        model_identity: str | None,
        prompt_config_version: str | None,
        proactive_kind: str,
        proactive_period: str,
        now: datetime | None = None,
    ) -> NativeConversationBeginResult:
        """Open an artifact-backed host prompt that waits for the owner's reply.

        Raw prompt content is stored only in the private transcript artifact. The
        relational row and lifecycle metadata carry stable, non-content ids.
        """

        current = _aware(now or self._clock())
        expiry = _aware(expires_at)
        if expiry <= current:
            raise ValueError("expires_at must be in the future")
        expiry = min(expiry, current + timedelta(hours=24))
        kind = _safe_metadata_value(proactive_kind, "proactive_kind", 128)
        period = _safe_metadata_value(proactive_period, "proactive_period", 255)
        prompt = prompt_text.strip()
        if not prompt:
            raise ValueError("prompt_text must not be empty")
        lifecycle = NativeLifecycleEvent(
            disposition="awaiting_user",
            content=None,
            occurred_at=current,
            metadata={
                "proactive": {
                    "kind": kind,
                    "period": period,
                    "root_event_id": root_event_id,
                }
            },
        )

        with Session(self._engine) as session, session.begin():
            NativeConversationRepository.expire_open_session_for_owner(
                session,
                discord_channel_id=discord_channel_id,
                owner_discord_user_id=owner_discord_user_id,
                now=current,
            )
            NativeConversationRepository.expire_open_sessions(session, now=current)
            winner = NativeConversationRepository.lock_open_for_owner(
                session,
                discord_channel_id=discord_channel_id,
                owner_discord_user_id=owner_discord_user_id,
                now=current,
            )
            if winner is not None:
                if winner.root_event_id != root_event_id:
                    return NativeConversationBeginResult(
                        status="in_progress",
                        session_id=winner.id,
                        root_event_id=winner.root_event_id,
                        state=winner.state,
                        revision=winner.revision,
                        response="Another conversation is already waiting for this owner.",
                    )
                return self._finish_existing_proactive_prompt(
                    session,
                    row=winner,
                    lifecycle=lifecycle,
                    now=current,
                )

            transcript = NativeTranscriptManifest.from_messages(
                (AIMessage(content=prompt),),
                revision=1,
                lifecycle_events=(lifecycle,),
            )
            transcript_key = self._store_transcript(transcript).key
            try:
                row = NativeConversationRepository.create_session(
                    session,
                    root_event_id=root_event_id,
                    discord_channel_id=discord_channel_id,
                    owner_discord_user_id=owner_discord_user_id,
                    transcript_artifact_key=transcript_key,
                    started_at=current,
                    expires_at=expiry,
                    model_identity=model_identity,
                    prompt_config_version=prompt_config_version,
                )
            except IntegrityError:
                winner = NativeConversationRepository.lock_open_for_owner(
                    session,
                    discord_channel_id=discord_channel_id,
                    owner_discord_user_id=owner_discord_user_id,
                    now=current,
                )
                if winner is None:
                    raise
                return NativeConversationBeginResult(
                    status="in_progress",
                    session_id=winner.id,
                    root_event_id=winner.root_event_id,
                    state=winner.state,
                    revision=winner.revision,
                    response="Another conversation is already waiting for this owner.",
                )
            if row.root_event_id == root_event_id and row.transcript_artifact_key != transcript_key:
                return self._finish_existing_proactive_prompt(
                    session,
                    row=row,
                    lifecycle=lifecycle,
                    now=current,
                )
            transcript = transcript.model_copy(
                update={"conversation_id": row.id, "revision": row.revision + 1}
            )
            transcript_key = self._store_transcript(transcript).key
            row = NativeConversationRepository.update_artifacts(
                session,
                conversation_id=row.id,
                transcript_artifact_key=transcript_key,
                now=current,
                state="awaiting_user",
                last_disposition="awaiting_user",
            )
            return NativeConversationBeginResult(
                status="started",
                session_id=row.id,
                root_event_id=row.root_event_id,
                state=row.state,
                revision=row.revision,
                transcript_messages=transcript.to_messages(),
                checkpoint={},
            )

    def checkpoint_messages(
        self,
        *,
        session_id: uuid.UUID,
        messages: Sequence[BaseMessage],
        now: datetime | None = None,
    ) -> tuple[BaseMessage, ...]:
        """Replace the transcript with the supplied ordered native messages."""

        current = _aware(now or self._clock())
        with Session(self._engine) as session, session.begin():
            row = NativeConversationRepository.lock_by_id(session, session_id)
            transcript = NativeTranscriptManifest.from_messages(
                tuple(messages),
                revision=row.revision + 1,
                conversation_id=row.id,
                lifecycle_events=self._load_transcript_for_row(session, row.id).lifecycle_events,
            )
            key = self._store_transcript(transcript).key
            NativeConversationRepository.update_artifacts(
                session,
                conversation_id=row.id,
                transcript_artifact_key=key,
                now=current,
            )
            return transcript.to_messages()

    def append_message(
        self,
        *,
        session_id: uuid.UUID,
        message: BaseMessage,
        now: datetime | None = None,
    ) -> tuple[BaseMessage, ...]:
        """Append one AI/tool/native message and checkpoint immediately."""

        current = _aware(now or self._clock())
        with Session(self._engine) as session, session.begin():
            row = NativeConversationRepository.lock_by_id(session, session_id)
            transcript = self._load_transcript_for_row(session, row.id).with_appended_messages(
                (message,),
                revision=row.revision + 1,
            )
            key = self._store_transcript(transcript).key
            NativeConversationRepository.update_artifacts(
                session,
                conversation_id=row.id,
                transcript_artifact_key=key,
                now=current,
            )
            return transcript.to_messages()

    def append_checkpoint_message(
        self,
        *,
        session_id: uuid.UUID,
        message: BaseMessage,
        now: datetime | None = None,
    ) -> tuple[BaseMessage, ...]:
        """Append only one real assistant/tool message from a harness checkpoint.

        Derived summary and memory context never crosses this boundary. Repeated
        delivery of the same last checkpoint is idempotent.
        """

        current = _aware(now or self._clock())
        with Session(self._engine) as session, session.begin():
            row = NativeConversationRepository.lock_by_id(session, session_id)
            current_manifest = self._load_transcript_for_row(session, row.id)
            serialized = messages_to_dict([message])[0]
            if current_manifest.messages and current_manifest.messages[-1].message == serialized:
                return current_manifest.to_messages()
            transcript = current_manifest.with_appended_messages(
                (message,),
                revision=row.revision + 1,
            )
            key = self._store_transcript(transcript).key
            NativeConversationRepository.update_artifacts(
                session,
                conversation_id=row.id,
                transcript_artifact_key=key,
                now=current,
            )
            return transcript.to_messages()

    def load_visible_blocks(self, *, session_id: uuid.UUID) -> tuple[Any, ...]:
        """Load model-eligible transcript blocks while excluding hidden reasoning."""

        with Session(self._engine) as session:
            manifest = self._load_transcript_for_row(session, session_id)
            return tuple(
                block
                for block in manifest.blocks
                if getattr(block, "kind", None) != "assistant_reasoning"
            )

    def load_lifecycle_history(self, *, session_id: uuid.UUID) -> tuple[NativeLifecycleEvent, ...]:
        """Load host-owned lifecycle events for clarification enforcement."""

        with Session(self._engine) as session:
            return self._load_transcript_for_row(session, session_id).lifecycle_events

    def load_transcript(self, *, session_id: uuid.UUID) -> NativeTranscriptManifest:
        """Load and validate the canonical immutable transcript manifest."""

        with Session(self._engine) as session:
            return self._load_transcript_for_row(session, session_id)

    def transcript_artifact_key(self, *, session_id: uuid.UUID) -> str:
        with Session(self._engine) as session:
            return NativeConversationRepository.lock_by_id(
                session, session_id
            ).transcript_artifact_key

    def latest_valid_compaction(self, *, session_id: uuid.UUID) -> NativeCompactionRecord | None:
        with Session(self._engine) as session:
            row = NativeConversationRepository.latest_valid_compaction(
                session,
                conversation_id=session_id,
            )
            return _compaction_record(row) if row is not None else None

    def publish_compaction(
        self,
        *,
        session_id: uuid.UUID,
        parent_compaction_id: uuid.UUID | None,
        covered_from_message_index: int,
        covered_through_message_index: int,
        source_transcript_artifact_key: str,
        source_fingerprint: str,
        summary_artifact_key: str,
        summary_model_identity: str,
        summary_prompt_version: str,
        estimated_input_tokens: int,
        reported_input_tokens: int | None = None,
        reported_output_tokens: int | None = None,
    ) -> NativeCompactionRecord:
        """Atomically publish a successor only after its summary artifact exists."""

        with Session(self._engine) as session, session.begin():
            row = NativeConversationRepository.create_compaction(
                session,
                conversation_id=session_id,
                parent_compaction_id=parent_compaction_id,
                covered_from_message_index=covered_from_message_index,
                covered_through_message_index=covered_through_message_index,
                source_transcript_artifact_key=source_transcript_artifact_key,
                source_fingerprint=source_fingerprint,
                summary_artifact_key=summary_artifact_key,
                summary_model_identity=summary_model_identity,
                summary_prompt_version=summary_prompt_version,
                estimated_input_tokens=estimated_input_tokens,
                reported_input_tokens=reported_input_tokens,
                reported_output_tokens=reported_output_tokens,
                status="valid",
            )
            NativeConversationRepository.supersede_valid_compactions(
                session,
                conversation_id=session_id,
                through_message_index=covered_through_message_index,
                excluding_compaction_id=row.id,
            )
            return _compaction_record(row)

    def record_failed_compaction(
        self,
        *,
        session_id: uuid.UUID,
        covered_from_message_index: int,
        covered_through_message_index: int,
        source_fingerprint: str,
        error_code: str,
    ) -> NativeCompactionRecord:
        """Record only a stable failure code, never private prompt/output text."""

        payload = json.dumps(
            {"schema_version": "native_conversation_summary_failure.v1"},
            separators=(",", ":"),
        )
        artifact = self._artifacts.put(
            payload,
            media_type=_MEDIA_TYPE,
            data_class="native_conversation_summary",
            preserve_private_content=True,
        )
        with Session(self._engine) as session, session.begin():
            conversation = NativeConversationRepository.lock_by_id(session, session_id)
            row = NativeConversationRepository.create_compaction(
                session,
                conversation_id=session_id,
                covered_from_message_index=covered_from_message_index,
                covered_through_message_index=covered_through_message_index,
                source_transcript_artifact_key=conversation.transcript_artifact_key,
                source_fingerprint=source_fingerprint,
                summary_artifact_key=artifact.key,
                summary_model_identity="unavailable",
                summary_prompt_version="native-summary-v1",
                estimated_input_tokens=0,
                status="failed",
                error_code=error_code,
            )
            return _compaction_record(row)

    def save_checkpoint(
        self,
        *,
        session_id: uuid.UUID,
        checkpoint: Mapping[str, Any],
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        """Persist a host-trusted tool checkpoint without exposing it to the model."""

        current = _aware(now or self._clock())
        with Session(self._engine) as session, session.begin():
            row = NativeConversationRepository.lock_by_id(session, session_id)
            manifest = NativeToolCheckpointManifest.from_mapping(
                checkpoint,
                conversation_id=row.id,
                revision=row.revision + 1,
            )
            checkpoint_key = self._store_checkpoint(manifest).key
            NativeConversationRepository.update_artifacts(
                session,
                conversation_id=row.id,
                transcript_artifact_key=row.transcript_artifact_key,
                tool_checkpoint_artifact_key=checkpoint_key,
                now=current,
            )
            return manifest.checkpoint

    def inspect_open(
        self,
        *,
        discord_channel_id: str,
        owner_discord_user_id: str,
        now: datetime | None = None,
    ) -> NativeConversationBeginResult:
        current = _aware(now or self._clock())
        with Session(self._engine) as session, session.begin():
            NativeConversationRepository.expire_open_sessions(session, now=current)
            row = NativeConversationRepository.lock_open_for_owner(
                session,
                discord_channel_id=discord_channel_id,
                owner_discord_user_id=owner_discord_user_id,
                now=current,
            )
            if row is None:
                return NativeConversationBeginResult(
                    status="no_open",
                    session_id=None,
                    root_event_id=None,
                    state=None,
                    revision=None,
                )
            try:
                transcript = self._load_transcript_for_row(session, row.id)
                checkpoint = self._load_checkpoint_for_row(row, session=session)
            except NativeConversationCorruptionError:
                return NativeConversationBeginResult(
                    status="corrupt",
                    session_id=row.id,
                    root_event_id=row.root_event_id,
                    state="failed",
                    revision=row.revision,
                    response="I could not safely resume that conversation. Please start again.",
                )
            return NativeConversationBeginResult(
                status="in_progress" if row.state == "processing" else "resumed",
                session_id=row.id,
                root_event_id=row.root_event_id,
                state=row.state,
                revision=row.revision,
                transcript_messages=transcript.to_messages(),
                checkpoint=checkpoint,
                response=_last_lifecycle_content(transcript),
            )

    def finish_turn(
        self,
        *,
        session_id: uuid.UUID,
        disposition: Literal["awaiting_user", "completed"],
        content: str,
        metadata: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> NativeConversationBeginResult:
        """Persist the model's terminal lifecycle disposition for this turn."""

        state = "awaiting_user" if disposition == "awaiting_user" else "completed"
        return self._finish(
            session_id=session_id,
            disposition=disposition,
            state=state,
            content=content,
            metadata=metadata or {},
            now=now,
        )

    def pause(
        self,
        *,
        session_id: uuid.UUID,
        error_code: str,
        content: str,
        now: datetime | None = None,
    ) -> NativeConversationBeginResult:
        """Keep a session open while asking the owner for an actionable next step."""

        return self._finish(
            session_id=session_id,
            disposition="awaiting_user",
            state="awaiting_user",
            content=content,
            metadata={"error_code": error_code},
            error_code=error_code,
            now=now,
        )

    def fail(
        self,
        *,
        session_id: uuid.UUID,
        error_code: str,
        content: str | None = None,
        now: datetime | None = None,
    ) -> NativeConversationBeginResult:
        return self._finish(
            session_id=session_id,
            disposition="failed",
            state="failed",
            content=content,
            metadata={"error_code": error_code},
            error_code=error_code,
            now=now,
        )

    def cancel(
        self,
        *,
        session_id: uuid.UUID,
        content: str | None = "No problem. Nothing was changed.",
        now: datetime | None = None,
    ) -> NativeConversationBeginResult:
        return self._finish(
            session_id=session_id,
            disposition="cancelled",
            state="cancelled",
            content=content,
            metadata={},
            now=now,
        )

    def cancel_open(
        self,
        *,
        discord_channel_id: str,
        owner_discord_user_id: str,
        external_event_id: str | None = None,
        content: str | None = "No problem. Nothing was changed.",
        now: datetime | None = None,
    ) -> NativeConversationBeginResult:
        current = _aware(now or self._clock())
        with Session(self._engine) as session, session.begin():
            NativeConversationRepository.expire_open_sessions(session, now=current)
            row = NativeConversationRepository.lock_open_for_owner(
                session,
                discord_channel_id=discord_channel_id,
                owner_discord_user_id=owner_discord_user_id,
                now=current,
            )
            if row is None:
                return NativeConversationBeginResult(
                    status="no_open",
                    session_id=None,
                    root_event_id=None,
                    state=None,
                    revision=None,
                    response="No active conversation was waiting. Nothing was changed.",
                )
            if external_event_id is not None:
                NativeConversationRepository.record_inbound_event(
                    session,
                    conversation_id=row.id,
                    external_event_id=external_event_id,
                    received_at=current,
                )
            session_id = row.id
        return self.cancel(session_id=session_id, content=content, now=current)

    def expire(self, *, now: datetime | None = None) -> int:
        with Session(self._engine) as session, session.begin():
            return NativeConversationRepository.expire_open_sessions(
                session,
                now=_aware(now or self._clock()),
            )

    def load_messages(self, *, session_id: uuid.UUID) -> tuple[BaseMessage, ...]:
        with Session(self._engine) as session, session.begin():
            return self._load_transcript_for_row(session, session_id).to_messages()

    def _finish(
        self,
        *,
        session_id: uuid.UUID,
        disposition: NativeConversationDisposition,
        state: str,
        content: str | None,
        metadata: Mapping[str, Any],
        error_code: str | None = None,
        now: datetime | None,
    ) -> NativeConversationBeginResult:
        current = _aware(now or self._clock())
        with Session(self._engine) as session, session.begin():
            row = NativeConversationRepository.lock_by_id(session, session_id)
            transcript = self._load_transcript_for_row(session, row.id).with_lifecycle_event(
                NativeLifecycleEvent(
                    disposition=disposition,
                    content=content,
                    metadata=dict(metadata),
                    occurred_at=current,
                ),
                revision=row.revision + 1,
            )
            key = self._store_transcript(transcript).key
            row = NativeConversationRepository.update_artifacts(
                session,
                conversation_id=row.id,
                transcript_artifact_key=key,
                now=current,
                state=state,
                last_disposition=disposition,
                error_code=error_code,
            )
            return NativeConversationBeginResult(
                status=_result_status(disposition),
                session_id=row.id,
                root_event_id=row.root_event_id,
                state=row.state,
                revision=row.revision,
                transcript_messages=transcript.to_messages(),
                checkpoint=self._load_checkpoint_for_row(row, session=session),
                response=content,
            )

    def _finish_existing_proactive_prompt(
        self,
        session: Session,
        *,
        row: Any,
        lifecycle: NativeLifecycleEvent,
        now: datetime,
    ) -> NativeConversationBeginResult:
        try:
            transcript = self._load_transcript_for_row(session, row.id)
            checkpoint = self._load_checkpoint_for_row(row, session=session)
        except NativeConversationCorruptionError:
            return NativeConversationBeginResult(
                status="corrupt",
                session_id=row.id,
                root_event_id=row.root_event_id,
                state="failed",
                revision=row.revision,
                response="I could not safely resume that conversation. Please start again.",
            )
        if row.state == "processing" and _looks_like_unanswered_proactive_prompt(transcript):
            if not _has_matching_proactive_lifecycle(transcript, lifecycle):
                transcript = transcript.with_lifecycle_event(lifecycle, revision=row.revision + 1)
            else:
                transcript = transcript.model_copy(
                    update={
                        "conversation_id": row.id,
                        "revision": row.revision + 1,
                    }
                )
            key = self._store_transcript(transcript).key
            row = NativeConversationRepository.update_artifacts(
                session,
                conversation_id=row.id,
                transcript_artifact_key=key,
                now=now,
                state="awaiting_user",
                last_disposition="awaiting_user",
            )
            return NativeConversationBeginResult(
                status="duplicate",
                session_id=row.id,
                root_event_id=row.root_event_id,
                state=row.state,
                revision=row.revision,
                transcript_messages=transcript.to_messages(),
                checkpoint=checkpoint,
                duplicate=True,
            )
        return NativeConversationBeginResult(
            status="duplicate",
            session_id=row.id,
            root_event_id=row.root_event_id,
            state=row.state,
            revision=row.revision,
            transcript_messages=transcript.to_messages(),
            checkpoint=checkpoint,
            response=_last_lifecycle_content(transcript),
            duplicate=True,
        )

    def _load_transcript_for_row(
        self,
        session: Session,
        session_id: uuid.UUID,
    ) -> NativeTranscriptManifest:
        row = NativeConversationRepository.lock_by_id(session, session_id)
        try:
            metadata = self._artifacts.get_metadata(row.transcript_artifact_key)
            _validate_metadata(
                metadata,
                data_class=_TRANSCRIPT_DATA_CLASS,
                max_bytes=_MAX_TRANSCRIPT_BYTES,
            )
            payload = json.loads(self._artifacts.get(row.transcript_artifact_key).decode("utf-8"))
            manifest = NativeTranscriptManifest.model_validate(payload)
            if manifest.conversation_id is not None and manifest.conversation_id != row.id:
                raise ValueError("transcript conversation_id mismatch")
            return manifest
        except Exception as exc:
            NativeConversationRepository.fail_corrupt(session, conversation_id=row.id)
            raise NativeConversationCorruptionError(
                "native conversation transcript is corrupt"
            ) from exc

    def _load_checkpoint_for_row(
        self,
        row: Any,
        *,
        session: Session | None = None,
    ) -> Mapping[str, Any]:
        key = row.tool_checkpoint_artifact_key
        if key is None:
            return {}
        try:
            metadata = self._artifacts.get_metadata(key)
            _validate_metadata(
                metadata,
                data_class=_CHECKPOINT_DATA_CLASS,
                max_bytes=_MAX_CHECKPOINT_BYTES,
            )
            payload = json.loads(self._artifacts.get(key).decode("utf-8"))
            manifest = NativeToolCheckpointManifest.model_validate(payload)
            if manifest.conversation_id != row.id:
                raise ValueError("tool checkpoint conversation_id mismatch")
            return manifest.checkpoint
        except Exception as exc:
            if session is not None:
                NativeConversationRepository.fail_corrupt(session, conversation_id=row.id)
            raise NativeConversationCorruptionError(
                "native conversation tool checkpoint is corrupt"
            ) from exc

    def _store_transcript(self, manifest: NativeTranscriptManifest) -> ArtifactMetadata:
        return self._store_json(
            manifest.model_dump(mode="json", exclude_none=True),
            data_class=_TRANSCRIPT_DATA_CLASS,
            max_bytes=_MAX_TRANSCRIPT_BYTES,
        )

    def _store_checkpoint(self, manifest: NativeToolCheckpointManifest) -> ArtifactMetadata:
        return self._store_json(
            manifest.model_dump(mode="json", exclude_none=True),
            data_class=_CHECKPOINT_DATA_CLASS,
            max_bytes=_MAX_CHECKPOINT_BYTES,
        )

    def _store_json(
        self,
        payload: Mapping[str, Any],
        *,
        data_class: str,
        max_bytes: int,
    ) -> ArtifactMetadata:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError(f"{data_class} artifact is too large")
        metadata = self._artifacts.put(
            encoded,
            media_type=_MEDIA_TYPE,
            data_class=data_class,
            preserve_private_content=True,
        )
        _validate_metadata(metadata, data_class=data_class, max_bytes=max_bytes)
        return metadata


def _validate_metadata(metadata: ArtifactMetadata, *, data_class: str, max_bytes: int) -> None:
    if metadata.data_class != data_class:
        raise ValueError("conversation artifact has wrong data class")
    if metadata.media_type != _MEDIA_TYPE:
        raise ValueError("conversation artifact has wrong media type")
    if metadata.size > max_bytes:
        raise ValueError("conversation artifact is too large")


def _compaction_record(row: Any) -> NativeCompactionRecord:
    return NativeCompactionRecord(
        id=row.id,
        summary_artifact_key=row.summary_artifact_key,
        covered_through_message_index=row.covered_through_message_index,
        source_fingerprint=row.source_fingerprint,
    )


def _result_status(disposition: NativeConversationDisposition) -> NativeConversationBeginStatus:
    if disposition == "completed":
        return "finished"
    if disposition == "cancelled":
        return "cancelled"
    if disposition == "failed":
        return "failed"
    if disposition == "expired":
        return "expired"
    return "resumed"


def _last_lifecycle_content(manifest: NativeTranscriptManifest) -> str | None:
    for event in reversed(manifest.lifecycle_events):
        if event.content:
            return event.content
    return None


def _safe_metadata_value(value: str, field: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} must not be empty")
    if len(cleaned) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return cleaned


def _looks_like_unanswered_proactive_prompt(manifest: NativeTranscriptManifest) -> bool:
    messages = manifest.to_messages()
    return len(messages) == 1 and isinstance(messages[0], AIMessage)


def _has_matching_proactive_lifecycle(
    manifest: NativeTranscriptManifest,
    lifecycle: NativeLifecycleEvent,
) -> bool:
    expected_obj = lifecycle.metadata.get("proactive")
    if not isinstance(expected_obj, Mapping):
        return False
    expected = cast(Mapping[str, object], expected_obj)
    for event in manifest.lifecycle_events:
        if event.disposition != "awaiting_user":
            continue
        actual_obj = event.metadata.get("proactive")
        if not isinstance(actual_obj, Mapping):
            continue
        actual = cast(Mapping[str, object], actual_obj)
        if dict(actual) == dict(expected):
            return True
    return False


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "NativeCompactionRecord",
    "NativeConversationCorruptionError",
    "NativeConversationService",
]
