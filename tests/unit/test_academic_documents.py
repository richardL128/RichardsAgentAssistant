"""Tests for cited PDF extraction, ambiguity, and deterministic chunks."""

from __future__ import annotations

import fitz
import pytest

from app.agents.academic_planner.documents import (
    chunk_document,
    detect_ambiguous_deadlines,
    extract_document,
    extract_notion_body_document,
)
from app.connectors.notion import NotionMaterialTextBlock


def _pdf(*pages: str) -> bytes:
    document = fitz.open()
    for value in pages:
        page = document.new_page()
        page.insert_text((72, 72), value)
    body = document.tobytes()
    document.close()
    return body


def test_pdf_pages_have_citations_and_ambiguous_deadlines_are_not_resolved() -> None:
    document = extract_document(
        _pdf("# Assessment\nDue 2026-09-10 or 2026-09-17."),
        document_id="outline-1",
        title="Outline",
        media_type="application/pdf",
    )
    ambiguity = detect_ambiguous_deadlines(document)
    assert document.pages[0].citation.locator == "page 1"
    assert ambiguity.ambiguous is True
    assert ambiguity.dates == ("2026-09-10", "2026-09-17")
    assert ambiguity.citations[0].page == 1


def test_chunking_is_bounded_and_preserves_metadata() -> None:
    document = extract_document(
        "# Policy\n" + "deadline policy text " * 700,
        document_id="policy-1",
        title="Policy",
        media_type="text/plain",
    )
    chunks = chunk_document(document, course="HIST-201", term="2026F")
    assert len(chunks) > 1
    assert all(len(chunk.content.split()) <= 500 for chunk in chunks)
    assert all(chunk.course == "HIST-201" and chunk.term == "2026F" for chunk in chunks)
    assert chunks[0].heading == "Policy"
    assert all(chunk.citation.locator == "page 1" for chunk in chunks)


def test_scanned_pdf_is_explicitly_marked_for_ocr() -> None:
    document = extract_document(
        _pdf(""), document_id="scan-1", title="Scan", media_type="application/pdf"
    )
    assert document.ocr_required is True
    assert document.extraction_status == "ocr_required"
    assert document.pages[0].citation.locator == "page 1"


def test_pdf_page_limit_allows_15_and_rejects_16_pages() -> None:
    fifteen = extract_document(
        _pdf(*("linear circuits page text" for _ in range(15))),
        document_id="rubric-15",
        title="Rubric",
        media_type="application/pdf",
    )
    assert len(fifteen.pages) == 15
    assert fifteen.extraction_status == "extracted"

    with pytest.raises(ValueError, match="too many pages"):
        extract_document(
            _pdf(*("linear circuits page text" for _ in range(16))),
            document_id="rubric-16",
            title="Rubric",
            media_type="application/pdf",
        )


def test_unsupported_binary_and_invalid_text_fail_closed() -> None:
    for content, media_type in (
        (b"\x00\x01\x02", "application/octet-stream"),
        (b"\x00bad", "text/plain"),
    ):
        with pytest.raises(ValueError, match="unsupported"):
            extract_document(
                content,
                document_id="binary",
                title="Binary",
                media_type=media_type,
            )


def test_scanned_pdf_uses_injected_ocr_and_preserves_page_citation() -> None:
    def ocr_extractor(content: bytes, page_number: int) -> str | None:
        assert content.startswith(b"%PDF-")
        return f"OCR text for page {page_number}"

    document = extract_document(
        _pdf(""),
        document_id="scan-ocr",
        title="Scan",
        media_type="application/pdf",
        ocr_extractor=ocr_extractor,
    )

    assert document.ocr_required is False
    assert document.extraction_status == "extracted"
    assert document.extraction_method == "pdf_text_ocr"
    assert document.pages[0].text == "OCR text for page 1"
    assert document.pages[0].citation.locator == "page 1"


def test_partial_ocr_is_recorded_without_losing_other_page_citations() -> None:
    def ocr_extractor(_content: bytes, page_number: int) -> str | None:
        return "Recovered page one" if page_number == 1 else None

    document = extract_document(
        _pdf("", ""),
        document_id="partial-scan",
        title="Partial Scan",
        media_type="application/pdf",
        ocr_extractor=ocr_extractor,
    )

    assert document.extraction_status == "partial"
    assert [page.citation.locator for page in document.pages] == ["page 1", "page 2"]
    assert document.pages[0].text == "Recovered page one"
    assert document.pages[1].text == ""


def test_notion_body_blocks_keep_block_citations_through_chunking() -> None:
    document = extract_notion_body_document(
        document_id="assessment-body",
        title="Assessment Body",
        blocks=[
            NotionMaterialTextBlock(
                source_page_id="assessment-1",
                source_block_id="block-2",
                source_key="assessment-1:body:block-2",
                order=2,
                block_type="paragraph",
                text="Then compare AC and DC circuits.",
            ),
            NotionMaterialTextBlock(
                source_page_id="assessment-1",
                source_block_id="block-1",
                source_key="assessment-1:body:block-1",
                order=1,
                block_type="bulleted_list_item",
                text="Start by solving linear circuits.",
            ),
        ],
    )
    chunks = chunk_document(document)

    assert [page.citation.locator for page in document.pages] == ["block block-1", "block block-2"]
    assert [chunk.citation.block for chunk in chunks] == ["block-1", "block-2"]
