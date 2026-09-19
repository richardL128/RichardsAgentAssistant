"""Service layer for generic durable user memory."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Sequence
from datetime import datetime
from typing import Any, Protocol, cast

from app.agents.memory.context import render_user_memory_context
from app.agents.memory.contracts import (
    UserMemoryAction,
    UserMemoryActionResult,
    UserMemoryContextItem,
    UserMemoryContextResult,
    UserMemoryCreate,
    UserMemoryEmbeddingPayload,
    UserMemoryHostContext,
    UserMemoryKind,
    UserMemoryOwnerScope,
    UserMemoryRecord,
    UserMemoryRetrievalResult,
    UserMemorySemanticStatus,
    UserMemoryStatus,
    UserMemoryToolCommand,
)

_EXPLICIT_REMEMBER_PATTERNS = (
    re.compile(r"^\s*(?:please\s+)?remember\b", re.IGNORECASE),
    re.compile(r"\bremember\s+(?:that|this|to|my)\b", re.IGNORECASE),
    re.compile(r"\bplease\s+remember\b", re.IGNORECASE),
)
_EXPLICIT_FORGET_PATTERNS = (
    re.compile(r"^\s*(?:please\s+)?forget\b", re.IGNORECASE),
    re.compile(r"\bforget\s+(?:that|this|what|my|everything|the)\b", re.IGNORECASE),
    re.compile(r"\bdelete\s+what\s+you\s+remember\b", re.IGNORECASE),
    re.compile(r"\bstop\s+remembering\b", re.IGNORECASE),
)
_EXPLICIT_CORRECT_PATTERNS = (
    re.compile(r"^\s*(?:please\s+)?correct\b", re.IGNORECASE),
    re.compile(r"\bcorrect\s+what\s+you\s+remember\b", re.IGNORECASE),
    re.compile(r"\bupdate\s+what\s+you\s+remember\b", re.IGNORECASE),
)
_URL = re.compile(r"https?://\S+")
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d .()\-]{7,}\d)(?!\d)")
_DISCORD_MENTION = re.compile(r"<[@#!&]?\d{5,}>")
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")


class UserMemorySemanticUnavailableError(RuntimeError):
    """Semantic lookup failed; this is not the same as empty memory."""

    code = "user_memory_semantic_unavailable"

    def __init__(self, reason: str = "generic user-memory embeddings are unavailable") -> None:
        self.reason = reason
        super().__init__(reason)


class UserMemoryStore(Protocol):
    """Persistence adapter contract for owner-scoped durable memory."""

    def create_memory(
        self,
        memory: UserMemoryCreate,
    ) -> UserMemoryRecord | Awaitable[UserMemoryRecord]: ...

    def list_memories(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        statuses: Sequence[UserMemoryStatus],
        kinds: Sequence[UserMemoryKind] | None,
        limit: int,
    ) -> Sequence[UserMemoryRecord] | Awaitable[Sequence[UserMemoryRecord]]: ...

    def search_exact_memories(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        query: str,
        statuses: Sequence[UserMemoryStatus],
        kinds: Sequence[UserMemoryKind] | None,
        limit: int,
    ) -> Sequence[UserMemoryRecord] | Awaitable[Sequence[UserMemoryRecord]]: ...

    def search_semantic_memories(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        query_embedding: Sequence[float],
        embedding_model: str,
        statuses: Sequence[UserMemoryStatus],
        kinds: Sequence[UserMemoryKind] | None,
        limit: int,
    ) -> Sequence[UserMemoryRecord] | Awaitable[Sequence[UserMemoryRecord]]: ...

    def correct_memory(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        replacement: UserMemoryCreate,
        memory_id: str | None,
        query: str | None,
    ) -> UserMemoryRecord | Awaitable[UserMemoryRecord | None] | None: ...

    def forget_memories(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        memory_id: str | None,
        query: str | None,
        source_external_event_id: str | None,
    ) -> Sequence[UserMemoryRecord] | Awaitable[Sequence[UserMemoryRecord]]: ...


class UserMemoryService:
    """Apply explicit owner memory actions and run bounded hybrid retrieval."""

    def __init__(
        self,
        *,
        store: UserMemoryStore,
        embedding_gateway: Any | None = None,
        retrieval_limit: int = 8,
    ) -> None:
        if retrieval_limit < 1 or retrieval_limit > 50:
            raise ValueError("retrieval_limit must be between 1 and 50")
        self._store = store
        self._embedding_gateway = embedding_gateway
        self._retrieval_limit = retrieval_limit

    async def handle_command(
        self,
        *,
        host_context: UserMemoryHostContext,
        command: UserMemoryToolCommand,
    ) -> UserMemoryActionResult:
        """Apply one host-scoped memory management command.

        The command intentionally contains no owner fields. The trusted owner
        scope always comes from host_context.
        """

        if command.action is UserMemoryAction.REVIEW:
            return await self._review(host_context=host_context)
        if command.action is UserMemoryAction.REMEMBER:
            return await self._remember(host_context=host_context, command=command)
        if command.action is UserMemoryAction.CORRECT:
            return await self._correct(host_context=host_context, command=command)
        if command.action is UserMemoryAction.FORGET:
            return await self._forget(host_context=host_context, command=command)
        raise ValueError("unsupported user memory action")

    async def manage(
        self,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        action: UserMemoryAction | str,
        raw_text: str,
        received_at: datetime,
        content: str | None = None,
        replacement_content: str | None = None,
        memory_id: str | None = None,
        kind: UserMemoryKind | str = UserMemoryKind.PERSONAL_FACT,
        normalized_subject: str | None = None,
        explicit_confirmation: bool = False,
        source_conversation_id: str | None = None,
        source_external_event_id: str | None = None,
    ) -> UserMemoryActionResult:
        """Convenience host API for integration code.

        The host supplies owner_user_id/owner_channel_id directly; model-visible
        command payloads still have no owner fields.
        """

        host_context = UserMemoryHostContext(
            owner_scope=UserMemoryOwnerScope(
                owner_user_id=owner_user_id,
                owner_channel_id=owner_channel_id,
            ),
            received_at=received_at,
            source_conversation_id=source_conversation_id,
            source_external_event_id=source_external_event_id,
        )
        command = UserMemoryToolCommand(
            action=UserMemoryAction(action),
            raw_text=raw_text,
            content=content,
            replacement_content=replacement_content,
            memory_id=memory_id,
            kind=UserMemoryKind(kind),
            normalized_subject=normalized_subject,
            explicit_confirmation=explicit_confirmation,
        )
        return await self.handle_command(host_context=host_context, command=command)

    async def retrieve_context(
        self,
        *,
        owner_user_id: str,
        owner_channel_id: str,
        query: str,
        limit: int | None = None,
        max_chars: int = 3_000,
        kinds: Sequence[UserMemoryKind] | None = None,
    ) -> UserMemoryContextResult:
        """Convenience API returning active items plus rendered untrusted context."""

        retrieval = await self.retrieve(
            owner_scope=UserMemoryOwnerScope(
                owner_user_id=owner_user_id,
                owner_channel_id=owner_channel_id,
            ),
            query=query,
            limit=limit,
            kinds=kinds,
        )
        block = render_user_memory_context(retrieval, max_chars=max_chars)
        included = set(block.included_memory_ids)
        return UserMemoryContextResult(
            items=tuple(item for item in retrieval.items if item.id in included),
            rendered_text=block.text,
            included_memory_ids=block.included_memory_ids,
            semantic_status=block.semantic_status,
            semantic_available=block.semantic_status is UserMemorySemanticStatus.AVAILABLE,
            omitted_count=block.omitted_count,
        )

    async def retrieve(
        self,
        *,
        owner_scope: UserMemoryOwnerScope,
        query: str,
        limit: int | None = None,
        kinds: Sequence[UserMemoryKind] | None = None,
    ) -> UserMemoryRetrievalResult:
        """Retrieve active memories using exact first, semantic second."""

        bounded_limit = limit or self._retrieval_limit
        if bounded_limit < 1 or bounded_limit > 50:
            raise ValueError("limit must be between 1 and 50")

        exact_records = await _maybe_await(
            self._store.search_exact_memories(
                owner_scope=owner_scope,
                query=query,
                statuses=(UserMemoryStatus.ACTIVE,),
                kinds=kinds,
                limit=bounded_limit,
            )
        )
        semantic_records: Sequence[UserMemoryRecord] = ()
        semantic_status = UserMemorySemanticStatus.NOT_REQUESTED
        semantic_error_code: str | None = None
        remaining = max(0, bounded_limit - len(exact_records))
        if remaining:
            try:
                embedding = await self._embed_text(query)
                if embedding is None:
                    raise UserMemorySemanticUnavailableError
                semantic_records = await _maybe_await(
                    self._store.search_semantic_memories(
                        owner_scope=owner_scope,
                        query_embedding=embedding.vector,
                        embedding_model=embedding.model_identity,
                        statuses=(UserMemoryStatus.ACTIVE,),
                        kinds=kinds,
                        limit=remaining,
                    )
                )
                semantic_status = UserMemorySemanticStatus.AVAILABLE
            except UserMemorySemanticUnavailableError as exc:
                semantic_status = UserMemorySemanticStatus.UNAVAILABLE
                semantic_error_code = exc.code

        records = _dedupe_records((*exact_records, *semantic_records))
        active = [record for record in records if record.status is UserMemoryStatus.ACTIVE]
        items = tuple(_context_item(record) for record in active[:bounded_limit] if record.content)
        omitted_count = max(0, len(active) - len(items))
        return UserMemoryRetrievalResult(
            items=items,
            semantic_status=semantic_status,
            semantic_error_code=semantic_error_code,
            exact_count=len(exact_records),
            semantic_count=len(semantic_records),
            omitted_count=omitted_count,
        )

    async def _remember(
        self,
        *,
        host_context: UserMemoryHostContext,
        command: UserMemoryToolCommand,
    ) -> UserMemoryActionResult:
        content = _memory_content(command.content, command.raw_text)
        status = (
            UserMemoryStatus.ACTIVE
            if command.explicit_confirmation or _has_explicit_remember_language(command.raw_text)
            else UserMemoryStatus.PENDING_CONFIRMATION
        )
        memory = await self._create_payload(
            host_context=host_context,
            command=command,
            content=content,
            status=status,
        )
        record = await _maybe_await(self._store.create_memory(memory))
        if status is UserMemoryStatus.ACTIVE:
            return UserMemoryActionResult(
                status="applied",
                response=f"Remembered: {record.redacted_preview}",
                memory_ids=(record.id,),
                previews=(record.redacted_preview,),
            )
        return UserMemoryActionResult(
            status="pending_confirmation",
            response=(
                "I saved this as pending confirmation, not active memory: "
                f"{record.redacted_preview}"
            ),
            memory_ids=(record.id,),
            previews=(record.redacted_preview,),
        )

    async def _correct(
        self,
        *,
        host_context: UserMemoryHostContext,
        command: UserMemoryToolCommand,
    ) -> UserMemoryActionResult:
        if not (command.explicit_confirmation or _has_explicit_correct_language(command.raw_text)):
            return UserMemoryActionResult(
                status="pending_confirmation",
                response="I need explicit confirmation before correcting durable memory.",
                previews=(),
            )
        replacement_content = _memory_content(command.replacement_content, command.raw_text)
        replacement = await self._create_payload(
            host_context=host_context,
            command=command,
            content=replacement_content,
            status=UserMemoryStatus.ACTIVE,
        )
        record = await _maybe_await(
            self._store.correct_memory(
                owner_scope=host_context.owner_scope,
                replacement=replacement,
                memory_id=command.memory_id,
                query=command.content,
            )
        )
        if record is None:
            return UserMemoryActionResult(
                status="not_found",
                response="I could not find an active memory in this owner scope to correct.",
            )
        return UserMemoryActionResult(
            status="applied",
            response=f"Updated memory: {record.redacted_preview}",
            memory_ids=(record.id,),
            previews=(record.redacted_preview,),
        )

    async def _forget(
        self,
        *,
        host_context: UserMemoryHostContext,
        command: UserMemoryToolCommand,
    ) -> UserMemoryActionResult:
        if not (command.explicit_confirmation or _has_explicit_forget_language(command.raw_text)):
            return UserMemoryActionResult(
                status="no_change",
                response="I need explicit forget language before deleting durable memory.",
            )
        records = await _maybe_await(
            self._store.forget_memories(
                owner_scope=host_context.owner_scope,
                memory_id=command.memory_id,
                query=command.content,
                source_external_event_id=host_context.source_external_event_id,
            )
        )
        if not records:
            return UserMemoryActionResult(
                status="not_found",
                response="I could not find an active memory in this owner scope to forget.",
            )
        previews = tuple(record.redacted_preview for record in records[:20])
        return UserMemoryActionResult(
            status="deleted",
            response=f"Forgot {len(records)} memory item(s).",
            memory_ids=tuple(record.id for record in records[:20]),
            previews=previews,
        )

    async def _review(self, *, host_context: UserMemoryHostContext) -> UserMemoryActionResult:
        records = await _maybe_await(
            self._store.list_memories(
                owner_scope=host_context.owner_scope,
                statuses=(UserMemoryStatus.ACTIVE, UserMemoryStatus.PENDING_CONFIRMATION),
                kinds=None,
                limit=20,
            )
        )
        if not records:
            return UserMemoryActionResult(
                status="review",
                response="I do not currently have any generic user memory for this owner scope.",
            )
        previews = tuple(record.redacted_preview for record in records[:20])
        return UserMemoryActionResult(
            status="review",
            response="Here is the generic user memory I can show safely.",
            memory_ids=tuple(record.id for record in records[:20]),
            previews=previews,
        )

    async def _create_payload(
        self,
        *,
        host_context: UserMemoryHostContext,
        command: UserMemoryToolCommand,
        content: str,
        status: UserMemoryStatus,
    ) -> UserMemoryCreate:
        embedding = await self._embed_text(content) if status is UserMemoryStatus.ACTIVE else None
        return UserMemoryCreate(
            owner_scope=host_context.owner_scope,
            kind=command.kind,
            status=status,
            content=content,
            redacted_preview=redact_memory_preview(content),
            normalized_subject=command.normalized_subject,
            sensitivity=command.sensitivity,
            source_conversation_id=host_context.source_conversation_id,
            source_external_event_id=host_context.source_external_event_id,
            embedding=embedding,
            created_at=host_context.received_at,
        )

    async def _embed_text(self, text: str) -> UserMemoryEmbeddingPayload | None:
        gateway = self._embedding_gateway
        if gateway is None:
            return None
        embedder = getattr(gateway, "embed_private_text", None)
        if embedder is None or not callable(embedder):
            embedder = getattr(gateway, "embed_academic_text", None)
        if embedder is None or not callable(embedder):
            return None
        try:
            result = await _maybe_await(embedder(text))
        except Exception:
            return None
        result_status = getattr(result, "status", None)
        status_value = getattr(result_status, "value", result_status)
        vector_object = getattr(result, "embedding", None)
        vector = getattr(vector_object, "vector", None)
        model_identity = getattr(result, "model_identity", None)
        if status_value != "valid" or vector is None or not isinstance(model_identity, str):
            return None
        try:
            values = tuple(float(item) for item in vector)
        except (TypeError, ValueError):
            return None
        return UserMemoryEmbeddingPayload(
            vector=values,
            model_identity=model_identity,
            dimensions=len(values),
        )


def redact_memory_preview(text: str, *, max_chars: int = 160) -> str:
    """Return a safe bounded operations preview without secrets or identifiers."""

    preview = text.strip()
    for pattern, replacement in (
        (_URL, "[url]"),
        (_EMAIL, "[email]"),
        (_PHONE, "[phone]"),
        (_DISCORD_MENTION, "[mention]"),
        (_UUID, "[id]"),
        (_LONG_TOKEN, "[token]"),
    ):
        preview = pattern.sub(replacement, preview)
    preview = re.sub(r"\s+", " ", preview).strip()
    if not preview:
        preview = "[redacted]"
    if len(preview) > max_chars:
        preview = preview[: max_chars - 1].rstrip() + "…"
    return preview


def _memory_content(value: str | None, fallback: str) -> str:
    content = (value or fallback).strip()
    if not content:
        raise ValueError("memory content must not be empty")
    return content


def _has_explicit_remember_language(text: str) -> bool:
    return any(pattern.search(text) for pattern in _EXPLICIT_REMEMBER_PATTERNS)


def _has_explicit_forget_language(text: str) -> bool:
    return any(pattern.search(text) for pattern in _EXPLICIT_FORGET_PATTERNS)


def _has_explicit_correct_language(text: str) -> bool:
    return any(pattern.search(text) for pattern in _EXPLICIT_CORRECT_PATTERNS)


def _dedupe_records(records: Sequence[UserMemoryRecord]) -> tuple[UserMemoryRecord, ...]:
    seen: set[str] = set()
    unique: list[UserMemoryRecord] = []
    for record in records:
        if record.id in seen:
            continue
        seen.add(record.id)
        unique.append(record)
    return tuple(unique)


def _context_item(record: UserMemoryRecord) -> UserMemoryContextItem:
    if not record.content:
        raise ValueError("active context memory requires content")
    return UserMemoryContextItem(
        id=record.id,
        kind=record.kind,
        content=record.content,
        redacted_preview=record.redacted_preview,
        normalized_subject=record.normalized_subject,
        revision=record.revision,
    )


async def _maybe_await[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return cast(T, await value)
    return value


__all__ = [
    "UserMemorySemanticUnavailableError",
    "UserMemoryService",
    "UserMemoryStore",
    "redact_memory_preview",
]
