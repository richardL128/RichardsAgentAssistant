"""Deterministic academic-document extraction, ambiguity checks, and chunking."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Final, Protocol, cast
from uuid import UUID

import fitz
from sqlalchemy.orm import Session

from app.connectors.notion import DatabaseName, NotionMaterialTextBlock, NotionPage, NotionPageBatch
from app.db.academic import AcademicRepository, DocumentChunkInput, SourceCitation

MAX_PDF_BYTES: Final[int] = 25 * 1024 * 1024
MAX_PDF_PAGES: Final[int] = 15
MAX_DOCUMENT_CHARS: Final[int] = 1_000_000
DEFAULT_CHUNK_TOKENS: Final[int] = 500
DEFAULT_CHUNK_OVERLAP: Final[int] = 50
MIN_EXTRACTED_PAGE_CHARS: Final[int] = 12
OCR_TIMEOUT_SECONDS: Final[float] = 10.0
SUPPORTED_TEXT_MEDIA_TYPES: Final[frozenset[str]] = frozenset(
    {"text/plain", "text/markdown", "text/x-markdown"}
)
_DATE_PATTERNS = (
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?(?:,)?\s+\d{4}\b",
        re.IGNORECASE,
    ),
)
_DEADLINE_WORDS = re.compile(r"\b(?:due|deadline|submit(?:ted|ting)?|test|quiz|exam)\b", re.I)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class PageCitation:
    page: int | None
    locator: str
    block: str | None = None

    def __post_init__(self) -> None:
        if self.page is not None and self.page < 1:
            raise ValueError("page must be positive")
        if self.block is not None and not self.block.strip():
            raise ValueError("block must not be empty")


@dataclass(frozen=True, slots=True)
class DocumentPage:
    page: int | None
    text: str
    citation: PageCitation


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    document_id: str
    title: str
    pages: tuple[DocumentPage, ...]
    media_type: str
    document_version: str = "v1"
    ocr_required: bool = False
    extraction_status: str = "extracted"
    extraction_method: str = "text"
    diagnostics: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if page.text.strip())


@dataclass(frozen=True, slots=True)
class DeadlineAmbiguity:
    ambiguous: bool
    dates: tuple[str, ...]
    reason: str | None
    citations: tuple[PageCitation, ...]


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    document_id: str
    chunk_index: int
    content: str
    page: int | None
    heading: str | None
    citation: PageCitation
    course: str | None = None
    term: str | None = None
    access_classification: str = "private"
    document_version: str = "v1"
    chunk_id: str | None = None
    embedding: tuple[float, ...] | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None


OCRTextExtractor = Callable[[bytes, int], str | None]


class AcademicDocumentStore(Protocol):
    """Persistence seam for normalized Notion pages and extracted chunks."""

    async def get_sync_cursor(self, database: DatabaseName) -> str | None: ...

    async def upsert_notion_page(self, page: NotionPage) -> None: ...

    async def save_sync_cursor(self, database: DatabaseName, cursor: str | None) -> None: ...

    async def upsert_document(
        self,
        document: ExtractedDocument,
        *,
        course: str | None,
        term: str | None,
        source_page_id: str | None,
    ) -> str: ...

    async def replace_document_chunks(
        self, document_id: str, chunks: Sequence[DocumentChunk]
    ) -> None: ...


async def _maybe_await[T](value: T | Awaitable[T]) -> T:
    if isinstance(value, Awaitable):
        return await cast(Awaitable[T], value)
    return value


async def sync_notion_database(
    *,
    source: Any,
    store: AcademicDocumentStore,
    database: DatabaseName,
    last_edited_after: datetime | None = None,
    page_size: int = 100,
) -> int:
    """Delta-sync one configured Notion database with a durable cursor.

    A cursor advances only after every page in its response has been handed to
    the persistence seam.  Replaying a response is therefore safe when a
    worker stops between page persistence and cursor advancement.
    """

    cursor = await _maybe_await(store.get_sync_cursor(database))
    total = 0
    while True:
        batch: NotionPageBatch = await source.query_database(
            database,
            last_edited_after=last_edited_after,
            start_cursor=cursor,
            page_size=page_size,
        )
        for page in batch.pages:
            await _maybe_await(store.upsert_notion_page(page))
            total += 1
        cursor = batch.next_cursor if batch.has_more else None
        await _maybe_await(store.save_sync_cursor(database, cursor))
        if not batch.has_more:
            return total


def persist_extracted_document(
    *,
    session: Session,
    document: ExtractedDocument,
    document_type: str,
    retrieved_at: datetime,
    artifact_key: str,
    content_hash: str | None = None,
    source_url: str | None = None,
    course_id: UUID | None = None,
    course: str | None = None,
    term: str | None = None,
    access_classification: str = "private",
) -> UUID:
    """Persist document metadata and replace its deterministic FTS chunks."""

    del course, term  # course/term are represented by the concrete course_id.
    digest = content_hash or sha256(document.text.encode("utf-8")).hexdigest()
    row = AcademicRepository.upsert_document(
        session,
        notion_id=document.document_id,
        document_version=document.document_version,
        title=document.title,
        document_type=document_type,
        retrieved_at=retrieved_at,
        artifact_key=artifact_key,
        content_hash=digest,
        source_url=source_url,
        course_id=course_id,
        access_classification=access_classification,
        extraction_status=document.extraction_status,
    )
    chunks = chunk_document(
        document,
        access_classification=access_classification,
    )
    inputs = [
        DocumentChunkInput(
            ordinal=chunk.chunk_index,
            content=chunk.content,
            heading=chunk.heading,
            token_count=len(chunk.content.split()),
            content_hash=sha256(chunk.content.encode("utf-8")).hexdigest(),
            citation=SourceCitation(page=chunk.page, block=chunk.citation.block),
        )
        for chunk in chunks
    ]
    AcademicRepository.replace_document_chunks(session, document_id=row.id, chunks=inputs)
    return row.id


def extract_pdf_pages(
    content: bytes,
    *,
    max_bytes: int = MAX_PDF_BYTES,
    max_pages: int = MAX_PDF_PAGES,
    max_chars: int = MAX_DOCUMENT_CHARS,
    min_page_chars: int = MIN_EXTRACTED_PAGE_CHARS,
    ocr_extractor: OCRTextExtractor | None = None,
    ocr_timeout_seconds: float = OCR_TIMEOUT_SECONDS,
) -> tuple[DocumentPage, ...]:
    """Extract bounded layout-aware text with one citation per PDF page."""

    return _extract_pdf_document(
        content,
        max_bytes=max_bytes,
        max_pages=max_pages,
        max_chars=max_chars,
        min_page_chars=min_page_chars,
        ocr_extractor=ocr_extractor,
        ocr_timeout_seconds=ocr_timeout_seconds,
    ).pages


def _extract_pdf_document(
    content: bytes,
    *,
    max_bytes: int,
    max_pages: int,
    max_chars: int,
    min_page_chars: int,
    ocr_extractor: OCRTextExtractor | None,
    ocr_timeout_seconds: float,
) -> ExtractedDocument:
    if not content or len(content) > max_bytes:
        raise ValueError("PDF exceeds the configured size limit")
    if max_pages < 1 or max_chars < 1 or min_page_chars < 0 or ocr_timeout_seconds <= 0:
        raise ValueError("PDF extraction limits must be positive")
    if not content.startswith(b"%PDF-"):
        raise ValueError("document is not a readable PDF")
    try:
        document: Any = fitz.open(stream=content, filetype="pdf")
    except (RuntimeError, ValueError) as exc:
        raise ValueError("document is not a readable PDF") from exc
    try:
        if document.is_encrypted:
            raise ValueError("encrypted PDFs are not supported")
        if len(document) > max_pages:
            raise ValueError("PDF has too many pages")
        pages: list[DocumentPage] = []
        consumed = 0
        low_text_pages = 0
        ocr_attempted = False
        for index in range(1, len(document) + 1):
            page: Any = document[index - 1]
            text = _extract_layout_text(page)
            if len(text.strip()) < min_page_chars:
                if ocr_extractor is None:
                    low_text_pages += 1
                else:
                    ocr_attempted = True
                    started = time.monotonic()
                    try:
                        ocr_text = ocr_extractor(content, index)
                    except Exception:
                        low_text_pages += 1
                    else:
                        timed_out = time.monotonic() - started > ocr_timeout_seconds
                        if timed_out or ocr_text is None or not ocr_text.strip():
                            low_text_pages += 1
                        else:
                            text = _join_text(text, ocr_text)
            page_text = text[: max(0, max_chars - consumed)]
            pages.append(DocumentPage(index, page_text, PageCitation(index, f"page {index}")))
            consumed += len(page_text)
            if consumed >= max_chars:
                break
        if low_text_pages == 0:
            status = "extracted"
            diagnostics: tuple[str, ...] = ()
        elif ocr_extractor is None:
            status = "ocr_required"
            diagnostics = ("ocr_required",)
        else:
            status = "partial"
            diagnostics = ("partial_ocr",)
        return ExtractedDocument(
            document_id="pdf",
            title="PDF",
            pages=tuple(pages),
            media_type="application/pdf",
            ocr_required=status == "ocr_required",
            extraction_status=status,
            extraction_method="pdf_text_ocr" if ocr_attempted else "pdf_text",
            diagnostics=diagnostics,
        )
    finally:
        document.close()


def extract_document(
    content: bytes | str,
    *,
    document_id: str,
    title: str,
    media_type: str,
    document_version: str = "v1",
    ocr_extractor: OCRTextExtractor | None = None,
    max_bytes: int = MAX_PDF_BYTES,
    max_pages: int = MAX_PDF_PAGES,
    min_page_chars: int = MIN_EXTRACTED_PAGE_CHARS,
    ocr_timeout_seconds: float = OCR_TIMEOUT_SECONDS,
) -> ExtractedDocument:
    """Extract either PDF pages or a cited single-page text document."""

    if not document_id.strip() or not title.strip():
        raise ValueError("document_id and title must not be empty")
    normalized_media_type = _normalized_media_type(media_type, content)
    if normalized_media_type == "application/pdf":
        if not isinstance(content, bytes):
            raise TypeError("PDF content must be bytes")
        extracted = _extract_pdf_document(
            content,
            max_bytes=max_bytes,
            max_pages=max_pages,
            max_chars=MAX_DOCUMENT_CHARS,
            min_page_chars=min_page_chars,
            ocr_extractor=ocr_extractor,
            ocr_timeout_seconds=ocr_timeout_seconds,
        )
        pages = extracted.pages
        ocr_required = extracted.ocr_required
        status = extracted.extraction_status
        method = extracted.extraction_method
        diagnostics = extracted.diagnostics
    elif normalized_media_type in SUPPORTED_TEXT_MEDIA_TYPES:
        text = _decode_text_content(content)
        pages = (DocumentPage(1, text[:MAX_DOCUMENT_CHARS], PageCitation(1, "page 1")),)
        ocr_required = False
        status = "extracted"
        method = "text"
        diagnostics = ()
    else:
        raise ValueError(f"unsupported document media type: {media_type}")
    return ExtractedDocument(
        document_id=document_id,
        title=title,
        pages=pages,
        media_type=normalized_media_type,
        document_version=document_version,
        ocr_required=ocr_required,
        extraction_status=status,
        extraction_method=method,
        diagnostics=diagnostics,
    )


def extract_notion_body_document(
    *,
    document_id: str,
    title: str,
    blocks: Sequence[tuple[str, str] | NotionMaterialTextBlock],
    document_version: str = "v1",
) -> ExtractedDocument:
    """Create a cited text document from ordered Notion page-body blocks."""

    pages: list[DocumentPage] = []
    consumed = 0
    ordered_blocks = sorted(
        blocks,
        key=lambda block: block.order if isinstance(block, NotionMaterialTextBlock) else 0,
    )
    for block in ordered_blocks:
        if isinstance(block, NotionMaterialTextBlock):
            block_id = block.source_block_id
            text = block.text
        else:
            block_id, text = block
        clean = text.strip()
        if not clean:
            continue
        bounded = clean[: max(0, MAX_DOCUMENT_CHARS - consumed)]
        if not bounded:
            break
        pages.append(
            DocumentPage(
                None,
                bounded,
                PageCitation(None, f"block {block_id}", block=block_id),
            )
        )
        consumed += len(bounded)
        if consumed >= MAX_DOCUMENT_CHARS:
            break
    return ExtractedDocument(
        document_id=document_id,
        title=title,
        pages=tuple(pages),
        media_type="text/plain",
        document_version=document_version,
        extraction_status="extracted" if pages else "failed",
        extraction_method="notion_blocks",
        diagnostics=() if pages else ("empty_page_body",),
    )


def _extract_layout_text(page: Any) -> str:
    try:
        raw_blocks: Any = page.get_text("blocks")
    except Exception:
        return str(page.get_text("text"))
    if not isinstance(raw_blocks, list):
        return str(page.get_text("text"))
    text_blocks: list[tuple[float, float, str]] = []
    for raw_block in cast(list[Any], raw_blocks):
        if not isinstance(raw_block, tuple | list):
            continue
        block = cast(Sequence[Any], raw_block)
        if len(block) < 5:
            continue
        text = str(block[4]).strip()
        if text:
            try:
                y0 = float(block[1])
                x0 = float(block[0])
            except (TypeError, ValueError):
                y0 = 0.0
                x0 = 0.0
            text_blocks.append((y0, x0, text))
    text_blocks.sort(key=lambda item: (round(item[0], 1), round(item[1], 1)))
    return "\n".join(item[2] for item in text_blocks)


def _join_text(existing: str, extra: str) -> str:
    first = existing.strip()
    second = extra.strip()
    if not first:
        return second
    if not second:
        return first
    return f"{first}\n{second}"


def _normalized_media_type(media_type: str, content: bytes | str) -> str:
    value = media_type.casefold().split(";", 1)[0].strip()
    if isinstance(content, bytes) and content.startswith(b"%PDF-"):
        return "application/pdf"
    if value == "application/pdf" or value in SUPPORTED_TEXT_MEDIA_TYPES:
        return value
    raise ValueError(f"unsupported document media type: {media_type}")


def _decode_text_content(content: bytes | str) -> str:
    if isinstance(content, str):
        return content[:MAX_DOCUMENT_CHARS]
    if b"\x00" in content[:1024]:
        raise ValueError("unsupported binary document")
    try:
        return content.decode("utf-8")[:MAX_DOCUMENT_CHARS]
    except UnicodeDecodeError as exc:
        raise ValueError("unsupported binary document") from exc


def detect_ambiguous_deadlines(document: ExtractedDocument) -> DeadlineAmbiguity:
    """Flag conflicting date candidates in deadline/test language.

    This deterministic guard intentionally does not choose a date.  Callers
    must keep an ambiguous date out of hard scheduling constraints until a
    confirmation event resolves it.
    """

    dates: list[str] = []
    citations: list[PageCitation] = []
    for page in document.pages:
        page_dates: list[str] = []
        for pattern in _DATE_PATTERNS:
            page_dates.extend(match.group(0) for match in pattern.finditer(page.text))
        if page_dates and _DEADLINE_WORDS.search(page.text):
            for value in page_dates:
                if value not in dates:
                    dates.append(value)
            if page.citation not in citations:
                citations.append(page.citation)
    ambiguous = len(dates) > 1
    reason = (
        "conflicting deadline or test dates were found; confirmation is required"
        if ambiguous
        else None
    )
    return DeadlineAmbiguity(ambiguous, tuple(dates), reason, tuple(citations))


def chunk_document(
    document: ExtractedDocument,
    *,
    course: str | None = None,
    term: str | None = None,
    access_classification: str = "private",
    target_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_CHUNK_OVERLAP,
) -> tuple[DocumentChunk, ...]:
    """Create stable heading/page chunks using a bounded token approximation."""

    if target_tokens < 1 or target_tokens > 700:
        raise ValueError("target_tokens must be between 1 and 700")
    if overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise ValueError("overlap_tokens must be less than target_tokens")
    chunks: list[DocumentChunk] = []
    index = 0
    for page in document.pages:
        heading: str | None = None
        words: list[str] = []
        for line in page.text.splitlines():
            match = _HEADING.match(line)
            if match:
                heading = match.group(2).strip()[:300]
                continue
            words.extend(line.split())
        if not words:
            continue
        start = 0
        while start < len(words):
            end = min(len(words), start + target_tokens)
            content = " ".join(words[start:end])
            chunks.append(
                DocumentChunk(
                    document_id=document.document_id,
                    chunk_index=index,
                    content=content,
                    page=page.page,
                    heading=heading,
                    citation=page.citation,
                    course=course,
                    term=term,
                    access_classification=access_classification,
                    document_version=document.document_version,
                )
            )
            index += 1
            if end == len(words):
                break
            start = end - overlap_tokens
    return tuple(chunks)


__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_TOKENS",
    "MIN_EXTRACTED_PAGE_CHARS",
    "SUPPORTED_TEXT_MEDIA_TYPES",
    "DeadlineAmbiguity",
    "DocumentChunk",
    "DocumentPage",
    "ExtractedDocument",
    "PageCitation",
    "chunk_document",
    "detect_ambiguous_deadlines",
    "extract_document",
    "extract_notion_body_document",
    "extract_pdf_pages",
    "persist_extracted_document",
    "sync_notion_database",
]
