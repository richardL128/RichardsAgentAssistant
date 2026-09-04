"""Tests for cited PDF extraction, ambiguity, and deterministic chunks."""

from __future__ import annotations

import fitz

from app.agents.academic_planner.documents import (
    chunk_document,
    detect_ambiguous_deadlines,
    extract_document,
)


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
