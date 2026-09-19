"""Host-scoped native memory tool for the active academic Discord handler."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.agents.harness import NativeTool, ToolExecutionError, ToolExecutionResult
from app.connectors.discord_gateway import DiscordAcademicMessageCreate


class _MemoryToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["reflection", "review"]


class NativeAcademicMemoryTool:
    """Expose durable memory without accepting model-controlled owner scope."""

    def __init__(self, memory_service: Any, message: DiscordAcademicMessageCreate) -> None:
        self._memory_service = memory_service
        self._message = message
        self.last_response: str | None = None
        self.last_status: str | None = None

    def tool(self) -> NativeTool:
        return NativeTool(
            schema={
                "type": "function",
                "function": {
                    "name": "manage_academic_memory",
                    "description": (
                        "Apply or review the owner's durable local academic learning memory. "
                        "Use review for show/review/correct/snooze/forget follow-up requests, "
                        "and reflection for new study-struggle reflections. The host supplies "
                        "the authenticated message, owner, channel, timestamp, and event id."
                    ),
                    "parameters": _MemoryToolArgs.model_json_schema(),
                },
            },
            handler=self,
            name="manage_academic_memory",
            side_effect_class="durable_local_write",
            activity="memory_data",
        )

    async def __call__(self, arguments: Mapping[str, object]) -> object:
        args = _MemoryToolArgs.model_validate(arguments)
        try:
            if args.action == "review":
                result = await self._memory_service.handle_memory_review(
                    external_event_id=self._message.message_id,
                    channel_id=self._message.channel_id,
                    user_id=self._message.author_id,
                    raw_text=self._message.content.get_secret_value(),
                    received_at=self._message.timestamp,
                )
            else:
                result = await self._memory_service.handle_reflection(
                    external_event_id=self._message.message_id,
                    channel_id=self._message.channel_id,
                    user_id=self._message.author_id,
                    raw_text=self._message.content.get_secret_value(),
                    received_at=self._message.timestamp,
                )
        except Exception as exc:
            if getattr(exc, "code", None) == "embeddings_unavailable":
                self.last_status = "failed"
                self.last_response = (
                    "Semantic academic memory is unavailable right now, so I did not store or "
                    "change any learning memory. Please try again after embeddings are healthy."
                )
                return ToolExecutionResult(
                    content={
                        "local_memory": {
                            "status": self.last_status,
                            "response": self.last_response,
                        },
                        "notion_change": "none",
                    }
                )
            raise
        status = str(getattr(result, "status", ""))
        response = getattr(result, "response", None)
        self.last_status = status
        self.last_response = response if isinstance(response, str) and response.strip() else None
        if status == "not_applicable":
            raise ToolExecutionError(
                "That message was not a supported academic memory request. "
                "Answer normally if no local memory action is needed."
            )
        return ToolExecutionResult(
            content={
                "local_memory": {
                    "status": status,
                    "response": self.last_response,
                },
                "notion_change": "none",
            }
        )


async def resume_open_academic_memory_session(
    memory_service: Any,
    message: DiscordAcademicMessageCreate,
) -> object | None:
    """Resume only an already-open owner/channel memory session, if one exists."""

    finder = getattr(memory_service, "open_memory_session_kind", None)
    if not callable(finder):
        return None
    kind = finder(
        channel_id=message.channel_id,
        user_id=message.author_id,
        now=message.timestamp,
    )
    if kind == "memory_review":
        return await memory_service.handle_memory_review(
            external_event_id=message.message_id,
            channel_id=message.channel_id,
            user_id=message.author_id,
            raw_text=message.content.get_secret_value(),
            received_at=message.timestamp,
        )
    if kind == "learning_focus":
        return await memory_service.handle_reflection(
            external_event_id=message.message_id,
            channel_id=message.channel_id,
            user_id=message.author_id,
            raw_text=message.content.get_secret_value(),
            received_at=message.timestamp,
        )
    return None


__all__ = ["NativeAcademicMemoryTool", "resume_open_academic_memory_session"]
