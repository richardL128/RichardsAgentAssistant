"""Cited PostgreSQL full-text retrieval for academic documents.

This module intentionally contains no vector or embedding code.  The SQL
builder keeps course, term, document-type, and access filters alongside the
full-text predicate so a result cannot accidentally cross course boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.agents.academic_planner.documents import DocumentChunk, PageCitation
from app.db.academic import AcademicRepository


@dataclass(frozen=True, slots=True)
class FullTextQuery:
    sql: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A matching chunk whose citation remains attached to the result."""

    chunk: DocumentChunk
    rank: float | None = None

    @property
    def citation(self) -> PageCitation:
        return self.chunk.citation


class AcademicRetrievalStore(Protocol):
    """Persistence seam for executing the generated PostgreSQL query."""

    async def search_document_chunks(
        self,
        *,
        query: FullTextQuery,
    ) -> Sequence[DocumentChunk | Mapping[str, Any]]: ...


def build_full_text_query(
    question: str,
    *,
    course_id: UUID | str | None = None,
    term: str | None = None,
    document_type: str | None = None,
    access_classification: str = "private",
    limit: int = 8,
) -> FullTextQuery:
    """Build a parameterized PostgreSQL FTS query with metadata filters."""

    if not question.strip():
        raise ValueError("retrieval question must not be empty")
    if limit < 1 or limit > 100:
        raise ValueError("retrieval limit must be between 1 and 100")
    if not access_classification.strip():
        raise ValueError("access classification must not be empty")
    sql = """
SELECT adc.id, adc.document_id, adc.ordinal, adc.content, adc.source_page,
       adc.source_block, adc.heading, ad.document_version,
       ad.course_id, c.term, ad.access_classification,
       ts_rank(adc.search_vector, websearch_to_tsquery('simple', :question)) AS rank
FROM academic_document_chunks AS adc
JOIN academic_documents AS ad ON ad.id = adc.document_id
LEFT JOIN courses AS c ON c.id = ad.course_id
WHERE adc.search_vector @@ websearch_to_tsquery('simple', :question)
  AND (:course_id IS NULL OR ad.course_id = :course_id)
  AND (:term IS NULL OR c.term = :term)
  AND (:document_type IS NULL OR ad.document_type = :document_type)
  AND ad.access_classification = :access_classification
ORDER BY rank DESC, adc.document_id ASC, adc.ordinal ASC
LIMIT :limit
""".strip()
    return FullTextQuery(
        sql=sql,
        parameters={
            "question": question.strip(),
            "course_id": course_id,
            "term": term,
            "document_type": document_type,
            "access_classification": access_classification,
            "limit": limit,
        },
    )


def _chunk_from_mapping(row: Mapping[str, Any]) -> DocumentChunk:
    citation = row.get("citation")
    if not isinstance(citation, PageCitation):
        page = int(row.get("page", row.get("source_page", 1)) or 1)
        block = row.get("block", row.get("source_block"))
        locator = f"page {page}"
        if block is not None:
            locator += f", block {block}"
        citation = PageCitation(page, locator)
    return DocumentChunk(
        document_id=str(row["document_id"]),
        chunk_index=int(row.get("ordinal", row.get("chunk_index", 0))),
        content=str(row["content"]),
        page=citation.page,
        heading=(str(row["heading"]) if row.get("heading") is not None else None),
        citation=citation,
        course=(str(row["course"]) if row.get("course") is not None else None),
        term=(str(row["term"]) if row.get("term") is not None else None),
        access_classification=str(row.get("access_classification", "private")),
        document_version=str(row.get("document_version", "v1")),
    )


async def retrieve_document_chunks(
    *,
    store: AcademicRetrievalStore,
    question: str,
    course_id: UUID | str | None = None,
    term: str | None = None,
    document_type: str | None = None,
    access_classification: str = "private",
    limit: int = 8,
) -> tuple[RetrievedChunk, ...]:
    """Execute bounded lexical retrieval while retaining page citations."""

    query = build_full_text_query(
        question,
        course_id=course_id,
        term=term,
        document_type=document_type,
        access_classification=access_classification,
        limit=limit,
    )
    rows = await store.search_document_chunks(query=query)
    result: list[RetrievedChunk] = []
    for row in rows[:limit]:
        if isinstance(row, DocumentChunk):
            result.append(RetrievedChunk(row))
        else:
            result.append(RetrievedChunk(_chunk_from_mapping(row), _rank(row.get("rank"))))
    return tuple(result)


def retrieve_with_academic_repository(
    session: Session,
    *,
    question: str,
    course_id: UUID | None = None,
    term: str | None = None,
    limit: int = 12,
) -> tuple[RetrievedChunk, ...]:
    """Use the concrete SQLAlchemy repository, retaining source-page citations."""

    rows: Sequence[Any] = AcademicRepository.search_document_chunks(  # type: ignore[attr-defined]
        session, query=question, course_id=course_id, term=term, limit=limit
    )
    result: list[RetrievedChunk] = []
    for raw_row in rows:
        row: Any = raw_row
        page = int(row.source_page or 1)
        result.append(
            RetrievedChunk(
                DocumentChunk(
                    document_id=str(row.document_id),
                    chunk_index=int(row.ordinal),
                    content=str(row.content),
                    page=page,
                    heading=str(row.heading) if row.heading is not None else None,
                    citation=PageCitation(page, f"page {page}"),
                )
            )
        )
    return tuple(result)


def _rank(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = [
    "AcademicRetrievalStore",
    "FullTextQuery",
    "RetrievedChunk",
    "build_full_text_query",
    "retrieve_document_chunks",
    "retrieve_with_academic_repository",
]
