"""Bounded backfill for academic RAG embeddings."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.academic import AcademicRepository
from app.db.models import Assessment


class MaterialEmbeddingBackfillGateway(Protocol):
    model_identity: str

    async def embed_academic_text(self, text: str) -> Any: ...


BootstrapIngestion = Callable[[], int | Awaitable[int]]


@dataclass(frozen=True, slots=True)
class MaterialEmbeddingBackfillResult:
    embedding_model: str
    dry_run: bool
    active_chunk_count: int
    pending_count: int
    embedded_count: int
    skipped_count: int
    failed_count: int
    remaining_count: int
    reflection_memory_count: int = 0
    material_pending_count: int = 0
    reflection_pending_count: int = 0
    material_embedded_count: int = 0
    reflection_embedded_count: int = 0
    material_skipped_count: int = 0
    reflection_skipped_count: int = 0
    material_failed_count: int = 0
    reflection_failed_count: int = 0
    material_remaining_count: int = 0
    reflection_remaining_count: int = 0
    bootstrap_job_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "embedding_model": self.embedding_model,
            "dry_run": self.dry_run,
            "active_chunk_count": self.active_chunk_count,
            "pending_count": self.pending_count,
            "embedded_count": self.embedded_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "remaining_count": self.remaining_count,
            "reflection_memory_count": self.reflection_memory_count,
            "material_pending_count": self.material_pending_count,
            "reflection_pending_count": self.reflection_pending_count,
            "material_embedded_count": self.material_embedded_count,
            "reflection_embedded_count": self.reflection_embedded_count,
            "material_skipped_count": self.material_skipped_count,
            "reflection_skipped_count": self.reflection_skipped_count,
            "material_failed_count": self.material_failed_count,
            "reflection_failed_count": self.reflection_failed_count,
            "material_remaining_count": self.material_remaining_count,
            "reflection_remaining_count": self.reflection_remaining_count,
            "bootstrap_job_count": self.bootstrap_job_count,
        }


async def backfill_material_embeddings(
    *,
    engine: Engine,
    embedding_gateway: MaterialEmbeddingBackfillGateway,
    batch_size: int = 64,
    max_batches: int | None = None,
    dry_run: bool = True,
    bootstrap_empty_corpus: BootstrapIngestion | None = None,
) -> MaterialEmbeddingBackfillResult:
    """Embed active material chunks and reflection memories missing current vectors."""

    if batch_size < 1 or batch_size > 500:
        raise ValueError("batch_size must be between 1 and 500")
    if max_batches is not None and max_batches < 1:
        raise ValueError("max_batches must be positive")
    embedding_model = str(embedding_gateway.model_identity)

    counts = _counts(engine, embedding_model)
    bootstrap_job_count = 0
    if counts.active_chunk_count == 0 and bootstrap_empty_corpus is not None and not dry_run:
        bootstrap_job_count = await _maybe_await(bootstrap_empty_corpus())
        counts = _counts(engine, embedding_model)
    if dry_run or counts.pending_count == 0:
        return _result(
            embedding_model=embedding_model,
            dry_run=dry_run,
            counts=counts,
            bootstrap_job_count=bootstrap_job_count,
        )

    material_embedded_count = 0
    material_skipped_count = 0
    material_failed_count = 0
    reflection_embedded_count = 0
    reflection_skipped_count = 0
    reflection_failed_count = 0
    after_chunk_id: UUID | None = None
    after_memory_id: UUID | None = None
    batches_processed = 0

    while max_batches is None or batches_processed < max_batches:
        with Session(engine) as session:
            material_candidates = AcademicRepository.list_material_embedding_backfill_candidates(
                session,
                embedding_model=embedding_model,
                limit=batch_size,
                after_chunk_id=after_chunk_id,
            )
            list_reflections = AcademicRepository.list_reflection_embedding_backfill_candidates
            reflection_candidates = list_reflections(
                session,
                embedding_model=embedding_model,
                limit=batch_size,
                after_memory_id=after_memory_id,
            )
        if not material_candidates and not reflection_candidates:
            break
        batches_processed += 1
        if material_candidates:
            after_chunk_id = material_candidates[-1].chunk_id
            embeddings = await _embed_texts(
                embedding_gateway,
                [candidate.content for candidate in material_candidates],
            )
            if len(embeddings) != len(material_candidates):
                raise RuntimeError("material embedding backfill returned the wrong count")
            with Session(engine) as session, session.begin():
                for candidate, embedding in zip(material_candidates, embeddings, strict=True):
                    vector = getattr(getattr(embedding, "embedding", None), "vector", None)
                    status = str(getattr(embedding, "status", ""))
                    if vector is None or status not in {"valid", "EmbeddingStatus.VALID"}:
                        material_failed_count += 1
                        continue
                    updated = AcademicRepository.update_material_chunk_embedding(
                        session,
                        chunk_id=candidate.chunk_id,
                        content_hash=candidate.content_hash,
                        embedding=vector,
                        embedding_model=str(getattr(embedding, "model_identity", embedding_model)),
                    )
                    material_embedded_count += int(updated)
                    material_skipped_count += int(not updated)
        if reflection_candidates:
            after_memory_id = reflection_candidates[-1].memory_id
            embeddings = await _embed_texts(
                embedding_gateway,
                [candidate.raw_text for candidate in reflection_candidates],
            )
            if len(embeddings) != len(reflection_candidates):
                raise RuntimeError("reflection embedding backfill returned the wrong count")
            with Session(engine) as session, session.begin():
                for candidate, embedding in zip(reflection_candidates, embeddings, strict=True):
                    vector = getattr(getattr(embedding, "embedding", None), "vector", None)
                    status = str(getattr(embedding, "status", ""))
                    if vector is None or status not in {"valid", "EmbeddingStatus.VALID"}:
                        reflection_failed_count += 1
                        continue
                    updated = AcademicRepository.update_reflection_memory_embedding(
                        session,
                        memory_id=candidate.memory_id,
                        raw_text_hash=candidate.raw_text_hash,
                        embedding=vector,
                        embedding_model=str(getattr(embedding, "model_identity", embedding_model)),
                    )
                    reflection_embedded_count += int(updated)
                    reflection_skipped_count += int(not updated)

    counts = _counts(engine, embedding_model)
    return _result(
        embedding_model=embedding_model,
        dry_run=False,
        counts=counts,
        material_embedded_count=material_embedded_count,
        reflection_embedded_count=reflection_embedded_count,
        material_skipped_count=material_skipped_count,
        reflection_skipped_count=reflection_skipped_count,
        material_failed_count=material_failed_count,
        reflection_failed_count=reflection_failed_count,
        bootstrap_job_count=bootstrap_job_count,
    )


@dataclass(frozen=True, slots=True)
class _BackfillCounts:
    active_chunk_count: int
    reflection_memory_count: int
    material_pending_count: int
    reflection_pending_count: int

    @property
    def pending_count(self) -> int:
        return self.material_pending_count + self.reflection_pending_count


def _counts(engine: Engine, embedding_model: str) -> _BackfillCounts:
    with Session(engine) as session:
        active_count = AcademicRepository.count_active_assessment_material_chunks(session)
        reflection_count = AcademicRepository.count_reflection_memories(session)
        material_pending_count = AcademicRepository.count_material_embedding_backfill_candidates(
            session,
            embedding_model=embedding_model,
        )
        reflection_pending_count = (
            AcademicRepository.count_reflection_embedding_backfill_candidates(
                session,
                embedding_model=embedding_model,
            )
        )
    return _BackfillCounts(
        active_chunk_count=active_count,
        reflection_memory_count=reflection_count,
        material_pending_count=material_pending_count,
        reflection_pending_count=reflection_pending_count,
    )


def _result(
    *,
    embedding_model: str,
    dry_run: bool,
    counts: _BackfillCounts,
    material_embedded_count: int = 0,
    reflection_embedded_count: int = 0,
    material_skipped_count: int = 0,
    reflection_skipped_count: int = 0,
    material_failed_count: int = 0,
    reflection_failed_count: int = 0,
    bootstrap_job_count: int = 0,
) -> MaterialEmbeddingBackfillResult:
    embedded_count = material_embedded_count + reflection_embedded_count
    skipped_count = material_skipped_count + reflection_skipped_count
    failed_count = material_failed_count + reflection_failed_count
    return MaterialEmbeddingBackfillResult(
        embedding_model=embedding_model,
        dry_run=dry_run,
        active_chunk_count=counts.active_chunk_count,
        reflection_memory_count=counts.reflection_memory_count,
        pending_count=counts.pending_count,
        material_pending_count=counts.material_pending_count,
        reflection_pending_count=counts.reflection_pending_count,
        embedded_count=embedded_count,
        material_embedded_count=material_embedded_count,
        reflection_embedded_count=reflection_embedded_count,
        skipped_count=skipped_count,
        material_skipped_count=material_skipped_count,
        reflection_skipped_count=reflection_skipped_count,
        failed_count=failed_count,
        material_failed_count=material_failed_count,
        reflection_failed_count=reflection_failed_count,
        remaining_count=counts.pending_count,
        material_remaining_count=counts.material_pending_count,
        reflection_remaining_count=counts.reflection_pending_count,
        bootstrap_job_count=bootstrap_job_count,
    )


async def _embed_texts(
    gateway: MaterialEmbeddingBackfillGateway,
    texts: Sequence[str],
) -> tuple[Any, ...]:
    embed_academic_texts = getattr(gateway, "embed_academic_texts", None)
    if callable(embed_academic_texts):
        batched = embed_academic_texts(list(texts))
        if inspect.isawaitable(batched):
            batched = await batched
        return _embedding_results_from_batch(batched)
    embed_documents = getattr(gateway, "embed_documents", None)
    if callable(embed_documents):
        legacy_batch = embed_documents(list(texts))
        if inspect.isawaitable(legacy_batch):
            legacy_batch = await legacy_batch
        return tuple(_embedding_result_sequence(legacy_batch))
    return tuple([await gateway.embed_academic_text(text) for text in texts])


def _embedding_result_sequence(value: object) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return cast(Sequence[Any], value)
    results = getattr(value, "results", None)
    if isinstance(results, Sequence) and not isinstance(results, (str, bytes, bytearray)):
        return cast(Sequence[Any], results)
    embeddings = getattr(value, "embeddings", None)
    if isinstance(embeddings, Sequence) and not isinstance(embeddings, (str, bytes, bytearray)):
        return cast(Sequence[Any], embeddings)
    raise RuntimeError("material embedding backfill returned an invalid payload")


def _embedding_results_from_batch(value: object) -> tuple[Any, ...]:
    status = str(getattr(value, "status", ""))
    if status not in {"valid", "EmbeddingStatus.VALID"}:
        raise RuntimeError("embedding backfill batch failed")
    model_identity = str(getattr(value, "model_identity", ""))
    embeddings = getattr(value, "embeddings", None)
    if not isinstance(embeddings, Sequence) or isinstance(embeddings, (str, bytes, bytearray)):
        raise RuntimeError("embedding backfill batch returned an invalid payload")
    return tuple(
        SimpleNamespace(status="valid", model_identity=model_identity, embedding=embedding)
        for embedding in cast(Sequence[Any], embeddings)
    )


async def _maybe_await[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def _enqueue_empty_corpus_bootstrap(engine: Engine) -> int:
    from app.agents.academic_planner.material_ingestion import assessment_material_fingerprint
    from app.queue.app import procrastinate_app
    from app.queue.tasks import defer_academic_material_ingestion

    with Session(engine) as session:
        rows = list(
            session.execute(
                select(Assessment.notion_id, Assessment.notion_last_edited_at)
                .where(
                    Assessment.active.is_(True),
                    Assessment.archived.is_(False),
                    Assessment.notion_last_edited_at.is_not(None),
                )
                .order_by(Assessment.id)
                .limit(500)
            )
        )
    queued = 0
    async with procrastinate_app.open_async():
        for page_id, last_edited_at in rows:
            if not isinstance(last_edited_at, datetime):
                continue
            fingerprint = assessment_material_fingerprint(str(page_id), last_edited_at)
            await defer_academic_material_ingestion(str(page_id), fingerprint)
            queued += 1
    return queued


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run or apply academic RAG embedding backfill for material chunks "
            "and reflection memories. Output is counts-only JSON."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist embeddings. Without this flag the command is a dry-run.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Rows per corpus per batch, between 1 and 500. Default: 64.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        help="Stop after this many bounded batches. Omit to run until no candidates remain.",
    )
    parser.add_argument(
        "--bootstrap-empty-corpus",
        action="store_true",
        help=(
            "With --apply, enqueue normal assessment-material ingestion if the "
            "active material corpus is empty."
        ),
    )
    return parser.parse_args(list(argv))


async def _run_cli(argv: Sequence[str]) -> int:
    from app.core.config import get_settings
    from app.db.session import Database
    from app.llm.embeddings import AcademicEmbeddingGateway

    args = _parse_args(argv)
    settings = get_settings()
    database = Database(settings)
    try:
        result = await backfill_material_embeddings(
            engine=database.engine,
            embedding_gateway=AcademicEmbeddingGateway(settings),
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            dry_run=not args.apply,
            bootstrap_empty_corpus=(
                (lambda: _enqueue_empty_corpus_bootstrap(database.engine))
                if args.bootstrap_empty_corpus and args.apply
                else None
            ),
        )
        print(json.dumps(result.as_dict(), sort_keys=True))
    finally:
        database.dispose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_run_cli(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MaterialEmbeddingBackfillResult",
    "backfill_material_embeddings",
    "main",
]
