"""Harden academic embeddings for indexed retrieval.

Revision ID: 0025_academic_embedding_hnsw
Revises: 0024_career_link_schema_repair
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0025_academic_embedding_hnsw"
down_revision = "0024_career_link_schema_repair"
branch_labels = None
depends_on = None

_DIMENSIONS = 1024
_STALE_MODEL_LIKE = "qwen3-embedding:0.6b%"
_CLEAR_VECTOR_SQL = {
    "academic_document_chunks": """
        UPDATE academic_document_chunks
        SET embedding = NULL,
            embedding_model = NULL,
            embedding_dimensions = NULL
        WHERE embedding IS NOT NULL
          AND (
            embedding_model IS NULL
            OR embedding_dimensions IS NULL
            OR vector_dims(embedding) != :dimensions
            OR embedding_model LIKE :stale_model_like
          )
        """,
    "academic_reflection_memories": """
        UPDATE academic_reflection_memories
        SET embedding = NULL,
            embedding_model = NULL,
            embedding_dimensions = NULL
        WHERE embedding IS NOT NULL
          AND (
            embedding_model IS NULL
            OR embedding_dimensions IS NULL
            OR vector_dims(embedding) != :dimensions
            OR embedding_model LIKE :stale_model_like
          )
        """,
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    _clear_incompatible_vectors("academic_document_chunks")
    _clear_incompatible_vectors("academic_reflection_memories")
    op.alter_column(
        "academic_document_chunks",
        "embedding",
        existing_type=Vector(),
        type_=Vector(_DIMENSIONS),
        postgresql_using=f"embedding::vector({_DIMENSIONS})",
        existing_nullable=True,
    )
    op.alter_column(
        "academic_reflection_memories",
        "embedding",
        existing_type=Vector(),
        type_=Vector(_DIMENSIONS),
        postgresql_using=f"embedding::vector({_DIMENSIONS})",
        existing_nullable=True,
    )
    op.create_index(
        "ix_academic_chunks_embedding_hnsw",
        "academic_document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )
    op.create_index(
        "ix_academic_reflections_embedding_hnsw",
        "academic_reflection_memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_where=sa.text("embedding IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.drop_index(
        "ix_academic_reflections_embedding_hnsw",
        table_name="academic_reflection_memories",
    )
    op.drop_index("ix_academic_chunks_embedding_hnsw", table_name="academic_document_chunks")
    op.alter_column(
        "academic_reflection_memories",
        "embedding",
        existing_type=Vector(_DIMENSIONS),
        type_=Vector(),
        postgresql_using="embedding::vector",
        existing_nullable=True,
    )
    op.alter_column(
        "academic_document_chunks",
        "embedding",
        existing_type=Vector(_DIMENSIONS),
        type_=Vector(),
        postgresql_using="embedding::vector",
        existing_nullable=True,
    )


def _clear_incompatible_vectors(table_name: str) -> None:
    op.execute(
        sa.text(_CLEAR_VECTOR_SQL[table_name]).bindparams(
            dimensions=_DIMENSIONS,
            stale_model_like=_STALE_MODEL_LIKE,
        )
    )
