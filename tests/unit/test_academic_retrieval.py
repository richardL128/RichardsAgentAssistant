"""Tests for metadata-scoped PostgreSQL FTS retrieval."""

from __future__ import annotations

from app.agents.academic_planner.documents import DocumentChunk, PageCitation
from app.agents.academic_planner.retrieval import (
    FullTextQuery,
    build_full_text_query,
    retrieve_document_chunks,
)


def test_query_contains_fts_and_course_term_access_filters_without_embeddings() -> None:
    query = build_full_text_query(
        "late submission policy", course_id="course-1", term="2026F", document_type="outline"
    )
    assert "websearch_to_tsquery" in query.sql
    assert "ad.course_id = :course_id" in query.sql
    assert "term = :term" in query.sql
    assert "document_type = :document_type" in query.sql
    assert "embedding" not in query.sql.lower()
    assert query.parameters["course_id"] == "course-1"


async def test_retrieval_preserves_page_citations_and_limit() -> None:
    citation = PageCitation(3, "page 3")
    chunk = DocumentChunk("outline", 0, "late policy", 3, "Policy", citation)

    class Store:
        async def search_document_chunks(self, *, query: FullTextQuery) -> list[DocumentChunk]:
            assert query.parameters["term"] == "2026F"
            return [chunk]

    result = await retrieve_document_chunks(
        store=Store(), question="late policy", term="2026F", limit=1
    )
    assert result[0].citation.locator == "page 3"


async def test_retrieval_reconstructs_repository_source_page_citations() -> None:
    class Store:
        async def search_document_chunks(self, *, query: FullTextQuery) -> list[dict[str, object]]:
            return [
                {
                    "document_id": "outline",
                    "ordinal": 2,
                    "content": "office hours are required before makeup tests",
                    "source_page": 7,
                    "source_block": "policy-table",
                    "heading": "Makeup Tests",
                    "document_version": "etag-1",
                    "access_classification": "private",
                }
            ]

    result = await retrieve_document_chunks(store=Store(), question="makeup tests")
    assert result[0].chunk.page == 7
    assert result[0].citation.locator == "page 7, block policy-table"
