from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.agents.memory.contracts import UserMemoryActionResult
from app.agents.memory.native_tool import NativeUserMemoryTool


@pytest.mark.asyncio
async def test_native_memory_tool_uses_host_scope_not_model_arguments() -> None:
    captured = {}

    class Service:
        async def manage(self, **values):
            captured.update(values)
            return UserMemoryActionResult(
                status="applied",
                response="Remembered: concise replies",
                memory_ids=(str(uuid.uuid4()),),
                previews=("concise replies",),
            )

    invalidated = False

    def invalidate() -> None:
        nonlocal invalidated
        invalidated = True

    bound = NativeUserMemoryTool(
        service=Service(),  # type: ignore[arg-type]
        owner_user_id="12345",
        owner_channel_id="67890",
        raw_owner_text="Remember that I prefer concise replies.",
        received_at=datetime(2026, 9, 15, tzinfo=UTC),
        source_conversation_id=uuid.uuid4(),
        source_external_event_id="event-1",
        invalidate_context_cache=invalidate,
    )
    tool = bound.tool()

    schema_properties = tool.schema["function"]["parameters"]["properties"]
    assert "owner_user_id" not in schema_properties
    assert "owner_channel_id" not in schema_properties
    result = await tool.handler(
        {
            "action": "remember",
            "content": "I prefer concise replies.",
            "kind": "preference",
        }
    )

    assert captured["owner_user_id"] == "12345"
    assert captured["owner_channel_id"] == "67890"
    assert result["status"] == "applied"
    assert invalidated is True
