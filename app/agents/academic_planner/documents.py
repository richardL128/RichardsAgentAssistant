"""Deterministic academic-document extraction, ambiguity checks, and chunking."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Final, Protocol, cast
from uuid import UUID

import fitz
from sqlalchemy.orm import Session

from app.connectors.notion import DatabaseName, NotionPage, NotionPageBatch
from app.db.academic import AcademicRepository, DocumentChunkInput, SourceCitation

MAX_PDF_BYTES: Final[int] = 25 * 1024 * 1024
MAX_PDF_PAGES: Final[int] = 250
MAX_DOCUMENT_CHARS: Final[int] = 1_000_000
DEFAULT_CHUNK_TOKENS: Final[int] = 500
DEFAULT_CHUNK_OVERLAP: Final[int] = 50
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
    page: int
    locator: str

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("page must be positive")


@dataclass(frozen=True, slots=True)
class DocumentPage:
    page: int
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
    page: int
    heading: str | None
    citation: PageCitation
    course: str | None = None
    term: str | None = None
    access_classification: str = "private"
    document_version: str = "v1"


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
        extraction_status="ocr_required" if document.ocr_required else "extracted",
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
            citation=SourceCitation(page=chunk.page),
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
) -> tuple[DocumentPage, ...]:
    """Extract bounded UTF-8 text with one citation per PDF page."""

    if not content or len(content) > max_bytes:
        raise ValueError("PDF exceeds the configured size limit")
    if max_pages < 1 or max_chars < 1:
        raise ValueError("PDF extraction limits must be positive")
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
        for index in range(1, len(document) + 1):
            page: Any = document[index - 1]
            text = str(page.get_text("text"))[: max(0, max_chars - consumed)]
            if text:
                pages.append(DocumentPage(index, text, PageCitation(index, f"page {index}")))
                consumed += len(text)
            if consumed >= max_chars:
                break
        return tuple(pages)
    finally:
        document.close()


def extract_document(
    content: bytes | str,
    *,
    document_id: str,
    title: str,
    media_type: str,
    document_version: str = "v1",
) -> ExtractedDocument:
    """Extract either PDF pages or a cited single-page text document."""

    if not document_id.strip() or not title.strip():
        raise ValueError("document_id and title must not be empty")
    if media_type.casefold() == "application/pdf":
        if not isinstance(content, bytes):
            raise TypeError("PDF content must be bytes")
        pages = extract_pdf_pages(content)
        ocr_required = not any(page.text.strip() for page in pages)
    else:
        text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        pages = (DocumentPage(1, text[:MAX_DOCUMENT_CHARS], PageCitation(1, "page 1")),)
        ocr_required = False
    return ExtractedDocument(
        document_id=document_id,
        title=title,
        pages=pages,
        media_type=media_type,
        document_version=document_version,
        ocr_required=ocr_required,
    )


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
    "DeadlineAmbiguity",
    "DocumentChunk",
    "DocumentPage",
    "ExtractedDocument",
    "PageCitation",
    "chunk_document",
    "detect_ambiguous_deadlines",
    "extract_document",
    "extract_pdf_pages",
    "persist_extracted_document",
    "sync_notion_database",
]
