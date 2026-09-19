from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.conversation import NativeConversationRepository
from app.db.models import Base, NativeConversationCompaction

NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)
CHANNEL = "222222222222222222"
OWNER = "333333333333333333"
ARTIFACT_A = "a" * 64
ARTIFACT_B = "b" * 64
ARTIFACT_C = "c" * 64


def test_compaction_lifecycle_keeps_latest_valid_and_idempotent(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'compactions.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session, session.begin():
            conversation = NativeConversationRepository.create_session(
                session,
                root_event_id="event-1",
                discord_channel_id=CHANNEL,
                owner_discord_user_id=OWNER,
                transcript_artifact_key=ARTIFACT_A,
                started_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                model_identity="qwen@test",
                prompt_config_version="policy-v1",
            )
            first = NativeConversationRepository.create_compaction(
                session,
                conversation_id=conversation.id,
                covered_from_message_index=0,
                covered_through_message_index=4,
                source_transcript_artifact_key=ARTIFACT_A,
                source_fingerprint="fingerprint-1",
                summary_artifact_key=ARTIFACT_B,
                summary_model_identity="qwen@test",
                summary_prompt_version="summary-v1",
                estimated_input_tokens=1200,
                reported_input_tokens=1180,
                reported_output_tokens=240,
            )
            replay = NativeConversationRepository.create_compaction(
                session,
                conversation_id=conversation.id,
                covered_from_message_index=0,
                covered_through_message_index=4,
                source_transcript_artifact_key=ARTIFACT_A,
                source_fingerprint="fingerprint-1",
                summary_artifact_key=ARTIFACT_B,
                summary_model_identity="qwen@test",
                summary_prompt_version="summary-v1",
                estimated_input_tokens=1200,
            )
            second = NativeConversationRepository.create_compaction(
                session,
                conversation_id=conversation.id,
                parent_compaction_id=first.id,
                covered_from_message_index=0,
                covered_through_message_index=9,
                source_transcript_artifact_key=ARTIFACT_A,
                source_fingerprint="fingerprint-2",
                summary_artifact_key=ARTIFACT_C,
                summary_model_identity="qwen@test",
                summary_prompt_version="summary-v1",
                estimated_input_tokens=1800,
            )
            superseded = NativeConversationRepository.supersede_valid_compactions(
                session,
                conversation_id=conversation.id,
                through_message_index=9,
                excluding_compaction_id=second.id,
            )

            assert replay.id == first.id
            assert superseded == 1
            assert first.status == "superseded"
            assert second.status == "valid"
            assert (
                NativeConversationRepository.latest_valid_compaction(
                    session,
                    conversation_id=conversation.id,
                ).id
                == second.id
            )
            assert (
                NativeConversationRepository.valid_compaction_for_range(
                    session,
                    conversation_id=conversation.id,
                    covered_from_message_index=0,
                    covered_through_message_index=9,
                ).summary_artifact_key
                == ARTIFACT_C
            )
            assert (
                session.scalar(select(func.count()).select_from(NativeConversationCompaction)) == 2
            )
    finally:
        engine.dispose()


def test_valid_retry_can_replace_failed_compaction_metadata(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'compaction-retry.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session, session.begin():
            conversation = NativeConversationRepository.create_session(
                session,
                root_event_id="event-retry",
                discord_channel_id=CHANNEL,
                owner_discord_user_id=OWNER,
                transcript_artifact_key=ARTIFACT_A,
                started_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                model_identity="qwen@test",
                prompt_config_version="policy-v1",
            )
            failed = NativeConversationRepository.create_compaction(
                session,
                conversation_id=conversation.id,
                covered_from_message_index=1,
                covered_through_message_index=4,
                source_transcript_artifact_key=ARTIFACT_A,
                source_fingerprint="retry-fingerprint",
                summary_artifact_key=ARTIFACT_B,
                summary_model_identity="unavailable",
                summary_prompt_version="summary-v1",
                estimated_input_tokens=0,
                status="failed",
                error_code="summary_generation_failed",
            )
            retried = NativeConversationRepository.create_compaction(
                session,
                conversation_id=conversation.id,
                covered_from_message_index=1,
                covered_through_message_index=4,
                source_transcript_artifact_key=ARTIFACT_A,
                source_fingerprint="retry-fingerprint",
                summary_artifact_key=ARTIFACT_C,
                summary_model_identity="qwen@test",
                summary_prompt_version="summary-v1",
                estimated_input_tokens=900,
                status="valid",
            )

            assert retried.id == failed.id
            assert retried.status == "valid"
            assert retried.error_code is None
            assert retried.summary_artifact_key == ARTIFACT_C
            assert retried.summary_model_identity == "qwen@test"
    finally:
        engine.dispose()
