"""Tests for metadata-scoped PostgreSQL FTS retrieval."""

from __future__ import annotations

from app.agents.academic_planner.documents import DocumentChunk, PageCitation
from app.agents.academic_planner.retrieval import (
    FullTextQuery,
    build_full_text_query,
    read_assessment_material_chunks,
    retrieve_document_chunks,
    semantic_search_assessment_materials,
)


def test_query_contains_fts_and_course_term_access_filters_without_embeddings() -> None:
    query = build_full_text_query(
        "late submission policy", course_id="course-1", term="2026F", document_type="outline"
    )
    assert "websearch_to_tsquery" in query.sql
    assert "ad.course_id = :course_id" in query.sql
    assert "ad.assessment_id = :assessment_id" in query.sql
    assert "term = :term" in query.sql
    assert "document_type = :document_type" in query.sql
    assert "embedding" not in query.sql.lower()
    assert query.parameters["course_id"] == "course-1"
    assert query.parameters["assessment_id"] is None


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
    assert result[0].chunk_id is None


async def test_assessment_scoped_lexical_query_sets_host_filters() -> None:
    class Store:
        async def search_document_chunks(self, *, query: FullTextQuery) -> list[dict[str, object]]:
            assert query.parameters["assessment_id"] == "assessment-1"
            assert query.parameters["active_only"] is True
            return [
                {
                    "chunk_id": "chunk-1",
                    "document_id": "doc-1",
                    "ordinal": 0,
                    "content": "linear circuit practice",
                    "source_page": 2,
                    "heading": None,
                    "document_version": "v1",
                    "access_classification": "private",
                }
            ]

    result = await retrieve_document_chunks(
        store=Store(),
        question="circuits",
        assessment_id="assessment-1",
        active_only=True,
    )

    assert result[0].chunk_id == "chunk-1"
    assert result[0].citation.locator == "page 2"


async def test_semantic_assessment_search_delegates_to_scoped_store() -> None:
    class Store:
        async def search_semantic_assessment_materials(
            self, assessment_id: str, query: str, *, limit: int = 8
        ) -> list[dict[str, object]]:
            assert assessment_id == "assessment-1"
            assert query == "what should I do first"
            assert limit == 2
            return [
                {
                    "chunk_id": "chunk-2",
                    "document_id": "doc-1",
                    "ordinal": 1,
                    "content": "Start with AC and DC comparison problems.",
                    "source_page": 3,
                    "source_block": "table-1",
                    "heading": "Rubric",
                    "document_version": "v2",
                    "access_classification": "private",
                    "score": 0.87,
                }
            ]

    result = await semantic_search_assessment_materials(
        store=Store(),
        assessment_id="assessment-1",
        query="what should I do first",
        limit=2,
    )

    assert result[0].chunk_id == "chunk-2"
    assert result[0].score == 0.87
    assert result[0].citation.locator == "page 3, block table-1"


def test_guarded_chunk_read_delegates_to_scoped_store() -> None:
    class Store:
        def read_assessment_material_chunks(
            self, assessment_id: str, chunk_ids: list[str], *, limit: int = 50
        ) -> list[dict[str, object]]:
            assert assessment_id == "assessment-1"
            assert chunk_ids == ["chunk-1", "chunk-other"]
            assert limit == 2
            return [
                {
                    "chunk_id": "chunk-1",
                    "document_id": "doc-1",
                    "ordinal": 0,
                    "content": "Assessment-owned chunk",
                    "source_page": 1,
                    "heading": None,
                    "document_version": "v1",
                    "access_classification": "private",
                }
            ]

    result = read_assessment_material_chunks(
        store=Store(),
        assessment_id="assessment-1",
        chunk_ids=["chunk-1", "chunk-other"],
        limit=2,
    )

    assert [item.chunk_id for item in result] == ["chunk-1"]
