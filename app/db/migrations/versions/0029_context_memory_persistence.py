"""Add native conversation compactions and generic user memory.

Revision ID: 0029_context_memory_persistence
Revises: 0028_native_conversations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0029_context_memory_persistence"
down_revision = "0028_native_conversations"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_DIMENSIONS = 1024


def upgrade() -> None:
    bind = op.get_bind()
    vector_type = Vector(_DIMENSIONS) if bind.dialect.name == "postgresql" else sa.JSON()

    op.create_table(
        "native_conversation_compactions",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("parent_compaction_id", _UUID),
        sa.Column("covered_from_message_index", sa.Integer(), nullable=False),
        sa.Column("covered_through_message_index", sa.Integer(), nullable=False),
        sa.Column("source_transcript_artifact_key", sa.String(64), nullable=False),
        sa.Column("source_fingerprint", sa.String(128), nullable=False),
        sa.Column("summary_artifact_key", sa.String(64), nullable=False),
        sa.Column("summary_model_identity", sa.String(255), nullable=False),
        sa.Column("summary_prompt_version", sa.String(128), nullable=False),
        sa.Column("estimated_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reported_input_tokens", sa.Integer()),
        sa.Column("reported_output_tokens", sa.Integer()),
        sa.Column("status", sa.String(32), nullable=False, server_default="valid"),
        sa.Column("error_code", sa.String(128)),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["native_conversation_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_compaction_id"],
            ["native_conversation_compactions.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "covered_from_message_index",
            "covered_through_message_index",
            "source_fingerprint",
            "summary_prompt_version",
            name="uq_native_compaction_source_range",
        ),
        sa.CheckConstraint("covered_from_message_index >= 0", name="covered_from_nonnegative"),
        sa.CheckConstraint(
            "covered_through_message_index >= covered_from_message_index",
            name="covered_range_valid",
        ),
        sa.CheckConstraint(
            "length(source_transcript_artifact_key) = 64",
            name="source_transcript_artifact_key_valid",
        ),
        sa.CheckConstraint("length(source_fingerprint) > 0", name="source_fingerprint_nonempty"),
        sa.CheckConstraint("length(summary_artifact_key) = 64", name="summary_artifact_key_valid"),
        sa.CheckConstraint("length(summary_model_identity) > 0", name="summary_model_nonempty"),
        sa.CheckConstraint("length(summary_prompt_version) > 0", name="summary_prompt_nonempty"),
        sa.CheckConstraint(
            "estimated_input_tokens >= 0", name="estimated_input_tokens_nonnegative"
        ),
        sa.CheckConstraint(
            "reported_input_tokens IS NULL OR reported_input_tokens >= 0",
            name="reported_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "reported_output_tokens IS NULL OR reported_output_tokens >= 0",
            name="reported_output_tokens_nonnegative",
        ),
        sa.CheckConstraint("status IN ('valid','superseded','failed')", name="status_valid"),
        sa.CheckConstraint(
            "error_code IS NULL OR length(error_code) > 0",
            name="error_code_nonempty",
        ),
    )
    op.create_index(
        "ix_native_compactions_conversation_status_range",
        "native_conversation_compactions",
        ["conversation_id", "status", "covered_through_message_index"],
    )
    op.create_index(
        "ix_native_compactions_parent",
        "native_conversation_compactions",
        ["parent_compaction_id"],
    )

    op.create_table(
        "user_memory_facts",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("owner_user_id", sa.String(32), nullable=False),
        sa.Column("owner_channel_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending_confirmation"),
        sa.Column("content_artifact_key", sa.String(64), nullable=False),
        sa.Column("redacted_preview", sa.String(2_000), nullable=False),
        sa.Column("normalized_subject", sa.String(255), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("sensitivity", sa.String(32), nullable=False, server_default="medium"),
        sa.Column("source_conversation_id", _UUID),
        sa.Column("source_external_event_id", sa.String(255)),
        sa.Column("evidence_artifact_key", sa.String(64)),
        sa.Column("embedding", vector_type),
        sa.Column("embedding_model", sa.String(255)),
        sa.Column("embedding_dimensions", sa.Integer()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(
            ["source_conversation_id"],
            ["native_conversation_sessions.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("length(owner_user_id) > 0", name="owner_user_id_nonempty"),
        sa.CheckConstraint("length(owner_channel_id) > 0", name="owner_channel_id_nonempty"),
        sa.CheckConstraint(
            "kind IN ('preference','profile','standing_instruction','constraint','personal_fact')",
            name="kind_valid",
        ),
        sa.CheckConstraint(
            "status IN ('active','pending_confirmation','superseded','deleted')",
            name="status_valid",
        ),
        sa.CheckConstraint("length(content_artifact_key) = 64", name="content_artifact_key_valid"),
        sa.CheckConstraint("length(redacted_preview) > 0", name="redacted_preview_nonempty"),
        sa.CheckConstraint("length(normalized_subject) > 0", name="normalized_subject_nonempty"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="confidence_probability",
        ),
        sa.CheckConstraint("sensitivity IN ('low','medium','high')", name="sensitivity_valid"),
        sa.CheckConstraint(
            "evidence_artifact_key IS NULL OR length(evidence_artifact_key) = 64",
            name="evidence_artifact_key_valid",
        ),
        sa.CheckConstraint(
            "embedding_dimensions IS NULL OR embedding_dimensions > 0",
            name="embedding_dimensions_positive",
        ),
        sa.CheckConstraint("revision >= 1", name="revision_positive"),
    )
    op.create_index(
        "ix_user_memory_owner_status_kind",
        "user_memory_facts",
        ["owner_user_id", "owner_channel_id", "status", "kind"],
    )
    op.create_index(
        "ix_user_memory_owner_subject",
        "user_memory_facts",
        ["owner_user_id", "owner_channel_id", "normalized_subject"],
    )
    if bind.dialect.name == "postgresql":
        op.create_index(
            "ix_user_memory_embedding_hnsw",
            "user_memory_facts",
            ["embedding"],
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=sa.text("embedding IS NOT NULL"),
        )

    op.create_table(
        "user_memory_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("memory_id", _UUID),
        sa.Column("owner_user_id", sa.String(32), nullable=False),
        sa.Column("owner_channel_id", sa.String(32), nullable=False),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(255)),
        sa.Column("occurred_at", _TS, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["memory_id"], ["user_memory_facts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "owner_user_id",
            "owner_channel_id",
            "external_event_id",
            name="uq_user_memory_events_owner_external",
        ),
        sa.CheckConstraint("length(owner_user_id) > 0", name="owner_user_id_nonempty"),
        sa.CheckConstraint("length(owner_channel_id) > 0", name="owner_channel_id_nonempty"),
        sa.CheckConstraint("length(external_event_id) > 0", name="external_event_id_nonempty"),
        sa.CheckConstraint(
            "event_type IN "
            "('create','confirm','correct','supersede','delete','retrieval_feedback')",
            name="event_type_valid",
        ),
        sa.CheckConstraint("actor IS NULL OR length(actor) > 0", name="actor_nonempty"),
    )
    op.create_index(
        "ix_user_memory_events_memory_time",
        "user_memory_events",
        ["memory_id", "occurred_at"],
    )
    op.create_index(
        "ix_user_memory_events_owner_time",
        "user_memory_events",
        ["owner_user_id", "owner_channel_id", "occurred_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index("ix_user_memory_events_owner_time", table_name="user_memory_events")
    op.drop_index("ix_user_memory_events_memory_time", table_name="user_memory_events")
    op.drop_table("user_memory_events")
    if bind.dialect.name == "postgresql":
        op.drop_index("ix_user_memory_embedding_hnsw", table_name="user_memory_facts")
    op.drop_index("ix_user_memory_owner_subject", table_name="user_memory_facts")
    op.drop_index("ix_user_memory_owner_status_kind", table_name="user_memory_facts")
    op.drop_table("user_memory_facts")
    op.drop_index("ix_native_compactions_parent", table_name="native_conversation_compactions")
    op.drop_index(
        "ix_native_compactions_conversation_status_range",
        table_name="native_conversation_compactions",
    )
    op.drop_table("native_conversation_compactions")
