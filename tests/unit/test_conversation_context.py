from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.conversation.context import (
    ConversationContextAssembler,
    estimate_native_input_tokens,
)
from app.agents.harness import PreModelContext
from app.artifacts.store import ArtifactStore
from app.llm.contracts import InvocationStatus


def _settings(**overrides):
    values = {
        "conversation_compaction_trigger_tokens": 10_000,
        "conversation_compaction_target_tokens": 8_000,
        "conversation_recent_tail_max_tokens": 2_000,
        "conversation_summary_enabled": True,
        "ollama_max_input_tokens": 12_000,
        "user_memory_enabled": True,
        "user_memory_retrieval_limit": 8,
        "user_memory_context_max_chars": 3_000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Conversations:
    def __init__(self) -> None:
        self.compaction = None
        self.published = []

    def latest_valid_compaction(self, *, session_id):
        return self.compaction

    def transcript_artifact_key(self, *, session_id):
        return "a" * 64

    def publish_compaction(self, **values):
        self.published.append(values)
        row = SimpleNamespace(
            id=uuid.uuid4(),
            summary_artifact_key=values["summary_artifact_key"],
            covered_through_message_index=values["covered_through_message_index"],
        )
        self.compaction = row
        return row


class _Gateway:
    model_identity = "model@test"

    def __init__(self) -> None:
        self.prompts = []

    async def invoke_structured(self, *, prompt, response_model):
        self.prompts.append(prompt)
        return SimpleNamespace(
            status=InvocationStatus.VALID,
            output=response_model(
                conversation_state="The owner is completing a request.",
                answered_questions=(),
                open_threads=("Finish the request",),
                tool_outcomes=(),
                user_statements=(),
            ),
        )


class _CapturingArtifactStore(ArtifactStore):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.writes = []

    def put(self, *args, **kwargs):
        metadata = super().put(*args, **kwargs)
        self.writes.append(metadata)
        return metadata


def _canonical_context_near_budget(*, minimum_tokens: int, maximum_tokens: int):
    messages = [SystemMessage(content="Policy")]
    index = 0
    while True:
        candidate = (
            *messages,
            HumanMessage(
                content="CURRENT_OWNER_MARKER continue from the recent context.",
            ),
        )
        estimate = estimate_native_input_tokens(candidate, ())
        if estimate >= minimum_tokens:
            assert estimate <= maximum_tokens
            return candidate, estimate
        messages.extend(
            (
                HumanMessage(
                    content=(f"Historic request {index} OLD_REPLAY_MARKER_{index} " + "x" * 320),
                ),
                AIMessage(content=f"Historic answer {index} " + "y" * 160),
            )
        )
        index += 1


@pytest.mark.asyncio
async def test_context_assembler_renders_memory_as_untrusted_bounded_context(tmp_path) -> None:
    memory_id = uuid.uuid4()

    class Memory:
        async def retrieve_context(self, **_kwargs):
            return SimpleNamespace(
                items=(SimpleNamespace(id=memory_id, content="Prefers concise replies."),),
                semantic_available=False,
            )

    conversations = _Conversations()
    assembler = ConversationContextAssembler(
        settings=_settings(),
        gateway=_Gateway(),
        conversation_service=conversations,
        artifact_store=ArtifactStore(tmp_path),
        user_memory_service=Memory(),
    )
    session_id = uuid.uuid4()
    hook = assembler.bind(
        conversation_id=session_id,
        owner_user_id="12345",
        owner_channel_id="67890",
        current_owner_message="Help me plan today.",
        event_id="event-1",
    )
    assembled = await hook(
        PreModelContext(
            canonical_messages=(
                SystemMessage(content="Policy"),
                HumanMessage(content="Help me plan today."),
            ),
            tools=(),
            turn=1,
            turn_limit=10,
        )
    )

    assert len(assembled) == 3
    assert "<untrusted_owner_memory>" in str(assembled[1].content)
    assert "Prefers concise replies" in str(assembled[1].content)
    assert isinstance(assembled[-1], HumanMessage)
    assert conversations.published == []


@pytest.mark.asyncio
async def test_compaction_excludes_reasoning_and_keeps_full_canonical_input(tmp_path) -> None:
    conversations = _Conversations()
    gateway = _Gateway()
    assembler = ConversationContextAssembler(
        settings=_settings(
            conversation_compaction_trigger_tokens=180,
            conversation_recent_tail_max_tokens=80,
            conversation_compaction_target_tokens=1_500,
            ollama_max_input_tokens=2_000,
            user_memory_enabled=False,
        ),
        gateway=gateway,
        conversation_service=conversations,
        artifact_store=ArtifactStore(tmp_path),
    )
    messages = [SystemMessage(content="Policy")]
    for index in range(5):
        messages.extend(
            (
                HumanMessage(content=f"Owner request {index}: " + "x" * 180),
                AIMessage(
                    content=f"Visible answer {index}",
                    additional_kwargs={"reasoning_content": "private scratch must not persist"},
                    tool_calls=[{"id": f"call-{index}", "name": "lookup", "args": {"i": index}}],
                ),
                ToolMessage(content=f"result {index}", tool_call_id=f"call-{index}"),
                AIMessage(content=f"Finished {index}"),
            )
        )
    canonical = tuple(messages)

    assembled = await assembler.assemble(
        PreModelContext(
            canonical_messages=canonical,
            tools=(),
            turn=6,
            turn_limit=20,
        ),
        conversation_id=uuid.uuid4(),
    )

    assert conversations.published
    assert gateway.prompts
    assert "private scratch must not persist" not in gateway.prompts[0]
    assert "<untrusted_session_summary>" in str(assembled[1].content)
    assert len(assembled) < len(canonical)
    assert canonical[-1] == assembled[-1]
    assert any(
        isinstance(item, HumanMessage) and str(item.content).startswith("Owner request 4")
        for item in assembled
    )
    for index, item in enumerate(assembled):
        if not isinstance(item, ToolMessage):
            continue
        assert any(
            isinstance(prior, AIMessage)
            and any(call.get("id") == item.tool_call_id for call in prior.tool_calls)
            for prior in assembled[:index]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("minimum_tokens", "maximum_tokens"),
    [
        pytest.param(19_968, 26_624, id="near-trigger"),
        pytest.param(26_200, 26_624, id="near-max"),
    ],
)
async def test_promoted_32k_profile_compacts_large_context_without_replay(
    tmp_path,
    minimum_tokens: int,
    maximum_tokens: int,
) -> None:
    conversations = _Conversations()
    gateway = _Gateway()
    artifacts = _CapturingArtifactStore(tmp_path)
    assembler = ConversationContextAssembler(
        settings=_settings(
            conversation_compaction_trigger_tokens=19_968,
            conversation_compaction_target_tokens=14_336,
            conversation_recent_tail_max_tokens=8_192,
            conversation_compaction_max_output_tokens=2_048,
            ollama_max_input_tokens=26_624,
            user_memory_enabled=False,
        ),
        gateway=gateway,
        conversation_service=conversations,
        artifact_store=artifacts,
    )
    canonical, original_estimate = _canonical_context_near_budget(
        minimum_tokens=minimum_tokens,
        maximum_tokens=maximum_tokens,
    )

    assembled = await assembler.assemble(
        PreModelContext(
            canonical_messages=canonical,
            tools=(),
            turn=10,
            turn_limit=20,
        ),
        conversation_id=uuid.uuid4(),
        event_id=f"profile-{minimum_tokens}",
    )

    assembled_text = "\n".join(str(message.content) for message in assembled)
    assembled_estimate = estimate_native_input_tokens(assembled, ())
    manifest_metadata = [
        metadata
        for metadata in artifacts.writes
        if metadata.data_class == "native_context_manifest"
    ][-1]
    manifest = json.loads(artifacts.get(manifest_metadata.key))

    assert original_estimate >= minimum_tokens
    assert conversations.published
    assert len(gateway.prompts) == 1
    assert len(assembled) < len(canonical)
    assert "OLD_REPLAY_MARKER_0" not in assembled_text
    assert "CURRENT_OWNER_MARKER" in assembled_text
    assert assembled_estimate <= 14_336
    assert manifest["compacted"] is True
    assert manifest["omitted_message_count"] > 0
    assert manifest["estimated_input_tokens"] == assembled_estimate
    assert manifest["estimated_input_tokens"] <= 14_336
