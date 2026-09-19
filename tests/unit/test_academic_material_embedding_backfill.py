from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.agents.academic_planner.material_embedding_backfill import (
    _enqueue_empty_corpus_bootstrap,
    backfill_material_embeddings,
)
from app.db.academic import (
    AcademicRepository,
    AcademicSemanticUnavailableError,
    DocumentChunkInput,
    LearningFocusMemoryInput,
    SourceCitation,
    SQLAlchemyAcademicPlannerStore,
)
from app.db.models import AcademicDocumentChunk, AcademicReflectionMemory, Assessment, Base
from app.llm.embeddings import EmbeddingStatus

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
PGVECTOR_READINESS = importlib.import_module(
    "app.db.migrations.versions.0025_academic_material_pgvector_readiness"
)


class _Gateway:
    model_identity = "qwen3-embedding:4b@current"

    def __init__(self) -> None:
        self.batch_calls: list[list[str]] = []

    async def embed_academic_texts(self, texts: list[str]) -> SimpleNamespace:
        self.batch_calls.append(texts)
        return SimpleNamespace(
            status="valid",
            model_identity=self.model_identity,
            embeddings=[
                SimpleNamespace(vector=[float(index + 1), 1.0]) for index, _text in enumerate(texts)
            ],
        )

    async def embed_academic_text(self, text: str) -> SimpleNamespace:
        raise AssertionError(f"unexpected single embedding call for {text}")


class _ReflectionQueryGateway:
    model_identity = "qwen3-embedding:4b@current"

    async def embed_reflection_text(self, text: str) -> SimpleNamespace:
        del text
        return SimpleNamespace(
            status=EmbeddingStatus.VALID,
            model_identity=self.model_identity,
            embedding=SimpleNamespace(vector=[1.0, 0.0]),
        )


@pytest.mark.asyncio
async def test_semantic_search_reports_unavailable_without_embedding_gateway(
    engine: Engine,
) -> None:
    store = SQLAlchemyAcademicPlannerStore(engine)

    with pytest.raises(AcademicSemanticUnavailableError):
        await store.search_semantic_assessment_materials(
            "assessment-page",
            "what should I study",
        )
    with pytest.raises(AcademicSemanticUnavailableError):
        await store.search_semantic_focuses("what do you remember", limit=2)


@pytest.fixture
def engine(tmp_path: Path):
    created = create_engine(f"sqlite+pysqlite:///{tmp_path / 'backfill.db'}")
    Base.metadata.create_all(created)
    try:
        yield created
    finally:
        created.dispose()


def _seed_material_chunks(engine: Engine) -> tuple[UUID, UUID]:
    with Session(engine) as session, session.begin():
        course = AcademicRepository.upsert_course(
            session,
            notion_id="course-page",
            course_code="ECE 222",
            title="Linear Circuits",
            term="2026F",
        )
        assessment = AcademicRepository.upsert_assessment(
            session,
            notion_id="assessment-page",
            course_id=course.id,
            title="Assignment 1",
            assessment_type="assignment",
            due_at=NOW,
            grade_weight_percent=None,
            estimated_minutes=120,
            confidence=1,
            fact_state="confirmed",
            citation=SourceCitation(),
        )
        document = AcademicRepository.upsert_document(
            session,
            notion_id="assessment-page",
            document_version="version-1",
            title="Rubric",
            document_type="application/pdf",
            retrieved_at=NOW,
            artifact_key="a" * 64,
            content_hash="b" * 64,
            course_id=course.id,
            assessment_id=assessment.id,
            source_kind="notion_property_file",
            source_page_id="assessment-page",
            source_property_id="files",
            source_key="assessment-page:files:rubric",
            extraction_status="extracted",
        )
        chunks = AcademicRepository.replace_document_chunks(
            session,
            document_id=document.id,
            chunks=[
                DocumentChunkInput(
                    ordinal=0,
                    content="derive the AC circuit response",
                    citation=SourceCitation(page=1),
                    embedding=[9.0, 1.0],
                    embedding_model="legacy-model",
                ),
                DocumentChunkInput(
                    ordinal=1,
                    content="simulate the DC operating point",
                    citation=SourceCitation(page=2),
                ),
            ],
        )
        return chunks[0].id, chunks[1].id


def _seed_reflection_memory(engine: Engine) -> UUID:
    with Session(engine) as session, session.begin():
        memory = AcademicRepository.create_learning_focus(
            session,
            topic="recursion",
            now=NOW,
            memory=LearningFocusMemoryInput(
                raw_text="Recursive tracing still needs practice.",
                embedding=[8.0, 1.0],
                embedding_model="legacy-model",
            ),
        )
        row = session.scalar(
            select(AcademicReflectionMemory).where(AcademicReflectionMemory.focus_id == memory.id)
        )
        assert row is not None
        return row.id


@pytest.mark.asyncio
async def test_material_embedding_backfill_dry_run_counts_without_model_calls(
    engine: Engine,
) -> None:
    _seed_material_chunks(engine)
    _seed_reflection_memory(engine)
    gateway = _Gateway()

    result = await backfill_material_embeddings(
        engine=engine,
        embedding_gateway=gateway,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.active_chunk_count == 2
    assert result.reflection_memory_count == 1
    assert result.pending_count == 3
    assert result.material_pending_count == 2
    assert result.reflection_pending_count == 1
    assert result.remaining_count == 3
    assert gateway.batch_calls == []


@pytest.mark.asyncio
async def test_material_embedding_backfill_updates_current_model_vectors(
    engine: Engine,
) -> None:
    first_id, second_id = _seed_material_chunks(engine)
    memory_id = _seed_reflection_memory(engine)
    gateway = _Gateway()
    material_text_by_id = {
        first_id: "derive the AC circuit response",
        second_id: "simulate the DC operating point",
    }
    selected_material_id = min(material_text_by_id)
    skipped_material_id = max(material_text_by_id)

    result = await backfill_material_embeddings(
        engine=engine,
        embedding_gateway=gateway,
        batch_size=1,
        max_batches=1,
        dry_run=False,
    )

    assert result.embedded_count == 2
    assert result.material_embedded_count == 1
    assert result.reflection_embedded_count == 1
    assert result.remaining_count == 1
    assert gateway.batch_calls == [
        [material_text_by_id[selected_material_id]],
        ["Recursive tracing still needs practice."],
    ]
    with Session(engine) as session:
        selected = session.get(AcademicDocumentChunk, selected_material_id)
        skipped = session.get(AcademicDocumentChunk, skipped_material_id)
        memory = session.get(AcademicReflectionMemory, memory_id)
        assert selected is not None
        assert skipped is not None
        assert memory is not None
        assert selected.embedding_model == "qwen3-embedding:4b@current"
        assert selected.embedding_dimensions == 2
        assert skipped.embedding_model in {None, "legacy-model"}
        assert memory.embedding_model == "qwen3-embedding:4b@current"
        assert memory.embedding_dimensions == 2


def test_material_embedding_update_skips_when_content_hash_changed(engine: Engine) -> None:
    first_id, _second_id = _seed_material_chunks(engine)
    with Session(engine) as session:
        original = session.scalar(
            select(AcademicDocumentChunk.content_hash).where(AcademicDocumentChunk.id == first_id)
        )
        assert original is not None

    with Session(engine) as session, session.begin():
        updated = AcademicRepository.update_material_chunk_embedding(
            session,
            chunk_id=first_id,
            content_hash="0" * 64,
            embedding=[1.0, 1.0],
            embedding_model="qwen3-embedding:4b@current",
        )

    assert updated is False
    with Session(engine) as session:
        row = session.get(AcademicDocumentChunk, first_id)
        assert row is not None
        assert row.embedding_model == "legacy-model"
        assert row.content_hash == original


@pytest.mark.asyncio
async def test_reflection_semantic_search_filters_exact_current_model(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        AcademicRepository.create_learning_focus(
            session,
            topic="legacy-vector",
            now=NOW,
            memory=LearningFocusMemoryInput(
                raw_text="legacy reflection",
                embedding=[1.0, 0.0],
                embedding_model="legacy-model",
            ),
        )
        AcademicRepository.create_learning_focus(
            session,
            topic="current-vector",
            now=NOW,
            memory=LearningFocusMemoryInput(
                raw_text="current reflection",
                embedding=[0.0, 1.0],
                embedding_model="qwen3-embedding:4b@current",
            ),
        )

    candidates = await SQLAlchemyAcademicPlannerStore(
        engine,
        embedding_gateway=_ReflectionQueryGateway(),
    ).search_semantic_focuses("which vector", limit=2)

    assert [candidate.focus.topic for candidate in candidates if candidate.focus] == [
        "current-vector"
    ]


def test_pgvector_readiness_migration_follows_career_repair_revision() -> None:
    assert PGVECTOR_READINESS.down_revision == "0024_career_link_schema_repair"
    assert PGVECTOR_READINESS.revision == "0025_academic_embedding_hnsw"
    assert PGVECTOR_READINESS._DIMENSIONS == 1024
    assert "academic_reflection_memories" in PGVECTOR_READINESS._CLEAR_VECTOR_SQL
    assert PGVECTOR_READINESS._STALE_MODEL_LIKE == "qwen3-embedding:0.6b%"


def test_embedding_backfill_cli_syntax_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    from app.agents.academic_planner.material_embedding_backfill import _parse_args

    args = _parse_args(["--apply", "--batch-size", "12", "--max-batches", "3"])
    assert args.apply is True
    assert args.batch_size == 12
    assert args.max_batches == 3

    with pytest.raises(SystemExit) as exc:
        _parse_args(["--help"])
    assert exc.value.code == 0
    assert "--bootstrap-empty-corpus" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_empty_corpus_can_bootstrap_via_normal_ingestion_hook(engine: Engine) -> None:
    calls = 0

    async def bootstrap() -> int:
        nonlocal calls
        calls += 1
        return 3

    result = await backfill_material_embeddings(
        engine=engine,
        embedding_gateway=_Gateway(),
        dry_run=False,
        bootstrap_empty_corpus=bootstrap,
    )

    assert calls == 1
    assert result.bootstrap_job_count == 3
    assert result.active_chunk_count == 0


@pytest.mark.asyncio
async def test_operator_bootstrap_opens_queue_before_deferring(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_material_chunks(engine)
    with Session(engine) as session, session.begin():
        assessment = session.scalar(select(Assessment))
        assert assessment is not None
        assessment.notion_last_edited_at = NOW
    opened = False
    deferred: list[tuple[str, str]] = []

    @asynccontextmanager
    async def open_queue():
        nonlocal opened
        opened = True
        yield

    async def defer(page_id: str, fingerprint: str) -> None:
        assert opened is True
        deferred.append((page_id, fingerprint))

    from app.queue import tasks
    from app.queue.app import procrastinate_app

    monkeypatch.setattr(procrastinate_app, "open_async", open_queue)
    monkeypatch.setattr(tasks, "defer_academic_material_ingestion", defer)

    queued = await _enqueue_empty_corpus_bootstrap(engine)

    assert queued == 1
    assert len(deferred) == 1
    assert len(deferred[0][1]) == 64
