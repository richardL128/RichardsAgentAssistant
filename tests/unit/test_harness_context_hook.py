from __future__ import annotations

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from app.agents.harness import NativeTool, PreModelContext, run_native_tool_loop


async def test_pre_model_hook_runs_each_turn_without_changing_canonical_checkpoints() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.calls = 0
            self.messages: list[tuple[object, ...]] = []

        async def invoke_tools(self, messages, _tools):
            self.calls += 1
            self.messages.append(tuple(messages))
            if self.calls == 1:
                return AIMessage(
                    content="Checking.",
                    tool_calls=[{"id": "call-1", "name": "lookup", "args": {}}],
                )
            return AIMessage(content="Done.")

    gateway = Gateway()
    hooks: list[PreModelContext] = []
    checkpoints = []

    async def hook(context: PreModelContext):
        hooks.append(context)
        return (context.canonical_messages[0], *context.canonical_messages[-2:])

    async def checkpoint(value):
        checkpoints.append(value)

    async def lookup(_args):
        return {"ok": True}

    result = await run_native_tool_loop(
        gateway=gateway,
        user_input="Please look this up.",
        tools=(
            NativeTool(
                schema={
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                handler=lookup,
            ),
        ),
        pre_model_context_hook=hook,
        checkpoint_sink=checkpoint,
    )

    assert result.status == "completed"
    assert len(hooks) == 2
    assert len(gateway.messages) == 2
    assert all(isinstance(call[0], SystemMessage) for call in gateway.messages)
    assert any(isinstance(message, ToolMessage) for message in hooks[1].canonical_messages)
    assert len(checkpoints) == 3
    assert all(
        checkpoint.messages[checkpoint.message_index] is checkpoint.messages[-1]
        for checkpoint in checkpoints
    )
