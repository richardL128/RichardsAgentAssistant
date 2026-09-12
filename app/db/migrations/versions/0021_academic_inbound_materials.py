"""Add durable academic inbound material intake.

Revision ID: 0021_academic_inbound_materials
Revises: 0020_job_interviews
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_academic_inbound_materials"
down_revision = "0020_job_interviews"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_SUPERSEDED_FK = "fk_academic_proposals_superseded_by"

_PROPOSAL_STATES = (
    "state IN ('pending','confirmed','applying','rejected','applied','expired','superseded')"
)
_OLD_PROPOSAL_STATES = "state IN ('pending','confirmed','applying','rejected','applied','expired')"


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def upgrade() -> None:
    proposal_columns = (
        sa.Column("owner_discord_user_id", sa.String(32)),
        sa.Column("discord_channel_id", sa.String(32)),
        sa.Column("superseded_by_id", _UUID),
        sa.Column("superseded_reason", sa.String(128)),
        sa.Column("superseded_at", _TS),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("academic_proposed_changes", recreate="always") as batch_op:
            for column in proposal_columns:
                batch_op.add_column(column)
            batch_op.drop_constraint("state_valid", type_="check")
            batch_op.create_check_constraint("state_valid", _PROPOSAL_STATES)
            batch_op.create_foreign_key(
                _SUPERSEDED_FK,
                "academic_proposed_changes",
                ["superseded_by_id"],
                ["id"],
                ondelete="SET NULL",
            )
    else:
        for column in proposal_columns:
            op.add_column("academic_proposed_changes", column)
        # Alembic 0006 created this constraint without the ORM naming convention.
        op.drop_constraint("state_valid", "academic_proposed_changes", type_="check")
        op.create_check_constraint("state_valid", "academic_proposed_changes", _PROPOSAL_STATES)
        op.create_foreign_key(
            _SUPERSEDED_FK,
            "academic_proposed_changes",
            "academic_proposed_changes",
            ["superseded_by_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_academic_proposals_owner_channel",
        "academic_proposed_changes",
        ["owner_discord_user_id", "discord_channel_id"],
    )

    op.create_table(
        "academic_inbound_materials",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("discord_message_id", sa.String(32), nullable=False),
        sa.Column("discord_attachment_id", sa.String(32), nullable=False),
        sa.Column("owner_discord_user_id", sa.String(32), nullable=False),
        sa.Column("discord_channel_id", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(128)),
        sa.Column("declared_byte_size", sa.BigInteger),
        sa.Column("observed_byte_size", sa.BigInteger, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("raw_artifact_key", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="captured"),
        sa.Column("assessment_id", _UUID, sa.ForeignKey("assessments.id", ondelete="SET NULL")),
        sa.Column(
            "proposal_id",
            _UUID,
            sa.ForeignKey("academic_proposed_changes.id", ondelete="SET NULL"),
        ),
        sa.Column("notion_page_id", sa.String(255)),
        sa.Column("notion_block_id", sa.String(255)),
        sa.Column("notion_upload_id", sa.String(255)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("expires_at", _TS),
        *_timestamps(),
        sa.CheckConstraint("length(discord_message_id) > 0", name="discord_message_id_nonempty"),
        sa.CheckConstraint(
            "length(discord_attachment_id) > 0", name="discord_attachment_id_nonempty"
        ),
        sa.CheckConstraint(
            "length(owner_discord_user_id) > 0", name="owner_discord_user_id_nonempty"
        ),
        sa.CheckConstraint("length(discord_channel_id) > 0", name="discord_channel_id_nonempty"),
        sa.CheckConstraint("length(filename) > 0", name="filename_nonempty"),
        sa.CheckConstraint(
            "declared_byte_size IS NULL OR declared_byte_size > 0",
            name="declared_size_positive",
        ),
        sa.CheckConstraint("observed_byte_size > 0", name="observed_size_positive"),
        sa.CheckConstraint("length(content_hash) = 64", name="content_hash_length"),
        sa.CheckConstraint("length(raw_artifact_key) = 64", name="raw_artifact_key_length"),
        sa.CheckConstraint(
            "state IN "
            "('captured','awaiting_target','proposal_pending','seeding','seeded','failed',"
            "'uncertain','expired')",
            name="state_valid",
        ),
        sa.UniqueConstraint(
            "discord_message_id",
            "discord_attachment_id",
            name="uq_academic_inbound_materials_discord_attachment",
        ),
    )
    op.create_index(
        "ix_academic_inbound_materials_owner_pending",
        "academic_inbound_materials",
        ["owner_discord_user_id", "discord_channel_id", "state", "created_at"],
    )
    op.create_index(
        "ix_academic_inbound_materials_assessment",
        "academic_inbound_materials",
        ["assessment_id", "state"],
    )
    op.create_index(
        "ix_academic_inbound_materials_proposal",
        "academic_inbound_materials",
        ["proposal_id", "state"],
    )
    op.create_index(
        "ix_academic_inbound_materials_hash",
        "academic_inbound_materials",
        ["content_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_academic_inbound_materials_hash", table_name="academic_inbound_materials")
    op.drop_index("ix_academic_inbound_materials_proposal", table_name="academic_inbound_materials")
    op.drop_index(
        "ix_academic_inbound_materials_assessment", table_name="academic_inbound_materials"
    )
    op.drop_index(
        "ix_academic_inbound_materials_owner_pending", table_name="academic_inbound_materials"
    )
    op.drop_table("academic_inbound_materials")

    op.drop_index("ix_academic_proposals_owner_channel", table_name="academic_proposed_changes")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("academic_proposed_changes", recreate="always") as batch_op:
            batch_op.drop_constraint(_SUPERSEDED_FK, type_="foreignkey")
            batch_op.drop_constraint("state_valid", type_="check")
            batch_op.create_check_constraint("state_valid", _OLD_PROPOSAL_STATES)
            batch_op.drop_column("superseded_at")
            batch_op.drop_column("superseded_reason")
            batch_op.drop_column("superseded_by_id")
            batch_op.drop_column("discord_channel_id")
            batch_op.drop_column("owner_discord_user_id")
    else:
        op.drop_constraint(_SUPERSEDED_FK, "academic_proposed_changes", type_="foreignkey")
        op.drop_constraint("state_valid", "academic_proposed_changes", type_="check")
        op.create_check_constraint("state_valid", "academic_proposed_changes", _OLD_PROPOSAL_STATES)
        op.drop_column("academic_proposed_changes", "superseded_at")
        op.drop_column("academic_proposed_changes", "superseded_reason")
        op.drop_column("academic_proposed_changes", "superseded_by_id")
        op.drop_column("academic_proposed_changes", "discord_channel_id")
        op.drop_column("academic_proposed_changes", "owner_discord_user_id")
