"""Host-bound native tool for explicit generic user-memory management."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.agents.harness import NativeTool
from app.agents.memory.contracts import UserMemoryAction, UserMemoryKind
from app.agents.memory.service import UserMemoryService


class UserMemoryToolArguments(BaseModel):
    """Model-visible arguments; authenticated scope is intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    action: UserMemoryAction
    content: str | None = Field(default=None, min_length=1, max_length=4_000)
    replacement_content: str | None = Field(default=None, min_length=1, max_length=4_000)
    memory_id: str | None = Field(default=None, min_length=1, max_length=255)
    kind: UserMemoryKind = UserMemoryKind.PERSONAL_FACT
    normalized_subject: str | None = Field(default=None, min_length=1, max_length=255)


class NativeUserMemoryTool:
    """Bind generic memory actions to trusted owner/channel/event metadata."""

    def __init__(
        self,
        *,
        service: UserMemoryService,
        owner_user_id: str,
        owner_channel_id: str,
        raw_owner_text: str,
        received_at: datetime,
        source_conversation_id: uuid.UUID | None,
        source_external_event_id: str,
        invalidate_context_cache: Callable[[], None] | None = None,
    ) -> None:
        self._service = service
        self._owner_user_id = owner_user_id
        self._owner_channel_id = owner_channel_id
        self._raw_owner_text = raw_owner_text
        self._received_at = received_at
        self._source_conversation_id = source_conversation_id
        self._source_external_event_id = source_external_event_id
        self._invalidate_context_cache = invalidate_context_cache

    def tool(self) -> NativeTool:
        return NativeTool(
            schema={
                "type": "function",
                "function": {
                    "name": "manage_user_memory",
                    "description": (
                        "Manage generic durable owner memory for stable personal facts, "
                        "preferences, constraints, and standing instructions. Use only when the "
                        "owner explicitly asks to remember, review, correct, or forget. Do not "
                        "use for course learning-focus memory. Owner scope is supplied by the host."
                    ),
                    "parameters": UserMemoryToolArguments.model_json_schema(),
                },
            },
            handler=self._handle,
            name="manage_user_memory",
            side_effect_class="durable_local_write",
            activity="memory_data",
        )

    async def _handle(self, arguments: Mapping[str, object]) -> object:
        parsed = UserMemoryToolArguments.model_validate(arguments)
        result = await self._service.manage(
            owner_user_id=self._owner_user_id,
            owner_channel_id=self._owner_channel_id,
            action=parsed.action,
            raw_text=self._raw_owner_text,
            received_at=self._received_at,
            content=parsed.content,
            replacement_content=parsed.replacement_content,
            memory_id=parsed.memory_id,
            kind=parsed.kind,
            normalized_subject=parsed.normalized_subject,
            source_conversation_id=(
                str(self._source_conversation_id) if self._source_conversation_id else None
            ),
            source_external_event_id=self._source_external_event_id,
        )
        if result.status in {"applied", "deleted"} and self._invalidate_context_cache is not None:
            self._invalidate_context_cache()
        return result.model_dump(mode="json")


__all__ = ["NativeUserMemoryTool", "UserMemoryToolArguments"]
