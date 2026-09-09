"""Add assessment-scoped academic material metadata and chunk embeddings.

Revision ID: 0014_academic_materials
Revises: 0013_academic_todo_types
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0014_academic_materials"
down_revision = "0013_academic_todo_types"
branch_labels = None
depends_on = None

_UUID = sa.Uuid(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_SOURCE_KIND_VALID = (
    "source_kind IS NULL OR source_kind IN "
    "('notion_page_body','notion_property_file','notion_block_file')"
)
_EXTRACTION_STATUS_VALID = (
    "extraction_status IN "
    "('pending','extracted','partial','ocr_required','ocr_processing',"
    "'unsupported','failed','inactive')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("academic_documents", recreate="always") as batch_op:
            batch_op.drop_constraint("uq_academic_documents_version", type_="unique")
            _add_document_columns(batch_op)
            batch_op.create_unique_constraint(
                "uq_academic_documents_source_version",
                ["source_key", "document_version"],
            )
            batch_op.create_check_constraint(
                "academic_document_source_kind_valid", _SOURCE_KIND_VALID
            )
            batch_op.create_check_constraint(
                "source_key_nonempty", "source_key IS NULL OR length(source_key) > 0"
            )
            batch_op.create_check_constraint(
                "academic_document_extraction_status_valid", _EXTRACTION_STATUS_VALID
            )
        with op.batch_alter_table("academic_document_chunks", recreate="always") as batch_op:
            _add_chunk_embedding_columns(batch_op, dialect_name="sqlite")
            batch_op.create_check_constraint(
                "academic_chunk_embedding_dimensions_positive",
                "embedding_dimensions IS NULL OR embedding_dimensions > 0",
            )
    else:
        op.drop_constraint("uq_academic_documents_version", "academic_documents", type_="unique")
        _add_document_columns(op, table_name="academic_documents")
        op.create_unique_constraint(
            "uq_academic_documents_source_version",
            "academic_documents",
            ["source_key", "document_version"],
        )
        op.create_check_constraint(
            "academic_document_source_kind_valid",
            "academic_documents",
            _SOURCE_KIND_VALID,
        )
        op.create_check_constraint(
            "source_key_nonempty",
            "academic_documents",
            "source_key IS NULL OR length(source_key) > 0",
        )
        op.create_check_constraint(
            "academic_document_extraction_status_valid",
            "academic_documents",
            _EXTRACTION_STATUS_VALID,
        )
        _add_chunk_embedding_columns(
            op,
            dialect_name=bind.dialect.name,
            table_name="academic_document_chunks",
        )
        op.create_check_constraint(
            "academic_chunk_embedding_dimensions_positive",
            "academic_document_chunks",
            "embedding_dimensions IS NULL OR embedding_dimensions > 0",
        )

    op.create_index(
        "uq_academic_documents_legacy_version",
        "academic_documents",
        ["notion_id", "document_version"],
        unique=True,
        postgresql_where=sa.text("source_key IS NULL"),
        sqlite_where=sa.text("source_key IS NULL"),
    )
    op.create_index(
        "ix_academic_documents_assessment_active",
        "academic_documents",
        ["assessment_id", "active"],
    )
    op.create_index(
        "ix_academic_documents_source_active",
        "academic_documents",
        ["source_key", "active", "retrieved_at"],
    )
    op.create_index(
        "ix_academic_chunks_embedding_model",
        "academic_document_chunks",
        ["embedding_model", "embedding_dimensions"],
    )


def downgrade() -> None:
    op.drop_index("ix_academic_chunks_embedding_model", table_name="academic_document_chunks")
    op.drop_index("ix_academic_documents_source_active", table_name="academic_documents")
    op.drop_index("ix_academic_documents_assessment_active", table_name="academic_documents")
    op.drop_index("uq_academic_documents_legacy_version", table_name="academic_documents")

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("academic_document_chunks", recreate="always") as batch_op:
            batch_op.drop_constraint("academic_chunk_embedding_dimensions_positive", type_="check")
            batch_op.drop_column("embedding_dimensions")
            batch_op.drop_column("embedding_model")
            batch_op.drop_column("embedding")
        with op.batch_alter_table("academic_documents", recreate="always") as batch_op:
            batch_op.drop_constraint("academic_document_extraction_status_valid", type_="check")
            batch_op.drop_constraint("source_key_nonempty", type_="check")
            batch_op.drop_constraint("academic_document_source_kind_valid", type_="check")
            batch_op.drop_constraint("uq_academic_documents_source_version", type_="unique")
            _drop_document_columns(batch_op)
            batch_op.create_unique_constraint(
                "uq_academic_documents_version",
                ["notion_id", "document_version"],
            )
    else:
        op.drop_constraint(
            "academic_chunk_embedding_dimensions_positive",
            "academic_document_chunks",
            type_="check",
        )
        op.drop_column("academic_document_chunks", "embedding_dimensions")
        op.drop_column("academic_document_chunks", "embedding_model")
        op.drop_column("academic_document_chunks", "embedding")
        op.drop_constraint(
            "academic_document_extraction_status_valid",
            "academic_documents",
            type_="check",
        )
        op.drop_constraint("source_key_nonempty", "academic_documents", type_="check")
        op.drop_constraint(
            "academic_document_source_kind_valid", "academic_documents", type_="check"
        )
        op.drop_constraint(
            "uq_academic_documents_source_version",
            "academic_documents",
            type_="unique",
        )
        _drop_document_columns(op, table_name="academic_documents")
        op.create_unique_constraint(
            "uq_academic_documents_version",
            "academic_documents",
            ["notion_id", "document_version"],
        )


def _add_document_columns(target: object, *, table_name: str | None = None) -> None:
    _add_column(
        target,
        table_name,
        sa.Column(
            "assessment_id",
            _UUID,
            sa.ForeignKey(
                "assessments.id",
                ondelete="SET NULL",
                name="fk_academic_documents_assessment_id_assessments",
            ),
        ),
    )
    _add_column(target, table_name, sa.Column("source_kind", sa.String(64)))
    _add_column(target, table_name, sa.Column("source_page_id", sa.String(255)))
    _add_column(target, table_name, sa.Column("source_block_id", sa.String(255)))
    _add_column(target, table_name, sa.Column("source_property_id", sa.String(255)))
    _add_column(target, table_name, sa.Column("source_key", sa.String(512)))
    _add_column(target, table_name, sa.Column("original_filename", sa.String(500)))
    _add_column(target, table_name, sa.Column("media_type", sa.String(255)))
    _add_column(target, table_name, sa.Column("source_last_edited_at", _TS))
    _add_column(
        target,
        table_name,
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    _add_column(target, table_name, sa.Column("extraction_error_code", sa.String(128)))
    _add_column(target, table_name, sa.Column("extraction_error_detail", sa.String(1000)))


def _drop_document_columns(target: object, *, table_name: str | None = None) -> None:
    for column in (
        "extraction_error_detail",
        "extraction_error_code",
        "active",
        "source_last_edited_at",
        "media_type",
        "original_filename",
        "source_key",
        "source_property_id",
        "source_block_id",
        "source_page_id",
        "source_kind",
        "assessment_id",
    ):
        if table_name is None:
            target.drop_column(column)  # type: ignore[attr-defined]
        else:
            target.drop_column(table_name, column)  # type: ignore[attr-defined]


def _add_chunk_embedding_columns(
    target: object,
    *,
    dialect_name: str,
    table_name: str | None = None,
) -> None:
    _add_column(
        target,
        table_name,
        sa.Column("embedding", Vector() if dialect_name == "postgresql" else sa.JSON()),
    )
    _add_column(target, table_name, sa.Column("embedding_model", sa.String(255)))
    _add_column(target, table_name, sa.Column("embedding_dimensions", sa.Integer()))


def _add_column(target: object, table_name: str | None, column: sa.Column[object]) -> None:
    if table_name is None:
        target.add_column(column)  # type: ignore[attr-defined]
    else:
        target.add_column(table_name, column)  # type: ignore[attr-defined]
