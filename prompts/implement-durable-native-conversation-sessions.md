# Implement durable native multi-turn conversation sessions

## Objective and user-visible outcome

Fix the Discord agent clarification loop by making one user request and all of
its follow-up messages a single logical model conversation. If LifeAgent asks a
clarifying question, the next owner message must resume with the exact active
conversation context that produced that question, including:

- the original and subsequent user messages;
- assistant messages and native tool calls;
- tool-result messages, including failures;
- private provider-native reasoning/thinking blocks when the configured model
  emits them; and
- the clarification question itself.

The model must semantically declare whether its outward response completes the
conversation or requests another owner reply. The host must persist that
declaration and must not infer it from punctuation, keywords, or whether the
assistant text happens to contain a question mark.

Keep the model gateway and Discord service alive at worker scope instead of
constructing and closing them for every Discord wake. Also increase the native
context window. Neither of those changes is sufficient on its own: correctness
must come from a durable, replayable transcript so a worker or Ollama restart
cannot erase an open conversation.

## Diagnosis grounded in the current runtime

The active worker path loses the transcript at two separate boundaries:

1. `app/agents/academic_planner/discord_wake_job.py` creates a new
   `AcademicDiscordService` in `_run_message` and closes it after handling every
   inbound Discord message. This recreates `LLMGateway`, the native handler, and
   all per-turn tool state for each wake.
2. `app/agents/academic_planner/discord_harness.py` calls
   `run_native_tool_loop` with only the current message's text.
   `app/agents/harness.py` then always constructs a new transcript containing
   only a new `SystemMessage` and `HumanMessage`. Assistant tool-call messages
   and `ToolMessage` results survive only inside that one function call.

There is already a bounded `AcademicAgentClarificationService` in
`app/agents/academic_planner/agent_clarification.py`, but the active native
Discord service does not construct or pass it to `NativeAcademicDiscordHandler`.
Its persisted context also contains only the original request plus clarification
question/answer strings. It cannot reproduce the native assistant/tool transcript
or restore trusted host-side tool capabilities.

The model settings amplify the problem:

- `OLLAMA_NUM_CTX` defaults to 2,048;
- `OLLAMA_MAX_INPUT_TOKENS` defaults to 6,000, which is larger than the runtime
  context window before output and safety headroom are considered; and
- `OLLAMA_MODEL_KEEP_ALIVE_SECONDS` is already passed to native calls with a
  300-second default. Ollama weight residency therefore is not the same thing as
  conversation continuity and does not retain the message transcript for this
  application.

`app/llm/gateway.py` invokes native chat with a supplied message list and does
not use a provider conversation identifier. Treat that interface as stateless:
every resumed invocation must receive the complete active transcript.

## Required architecture

### 1. Make conversation continuity durable, not process-local

Add a generic native-agent conversation session rather than another
academic-specific question/answer summary. Prefer focused new modules such as:

- `app/agents/conversation/contracts.py` for versioned transcript and lifecycle
  contracts;
- `app/agents/conversation/service.py` for session locking, append, resume, and
  completion; and
- repository methods in a focused database module rather than continuing to
  expand the academic planner repository.

Persist relational metadata for lookup and concurrency, while keeping raw
message/tool/reasoning content in the existing private content-addressed
artifact store. A conversation row should minimally contain:

- conversation ID and immutable root Discord event ID;
- owner Discord user and channel IDs;
- state: `processing`, `awaiting_user`, `completed`, `failed`, or `expired`;
- monotonically increasing revision and next event sequence;
- current transcript-manifest artifact key;
- current trusted tool-checkpoint artifact key, if any;
- model identity and prompt/config version;
- started, last-turn, completed, and expiry timestamps; and
- the last lifecycle disposition without raw private response text.

Record each inbound Discord event idempotently against exactly one conversation.
Use a partial unique constraint so an owner/channel has at most one open native
conversation. Lock the open row before accepting a continuation, and reject or
coalesce concurrent turns while its state is `processing`.

The artifact manifest must be versioned and ordered. It must round-trip the
native message semantics needed by the model rather than flattening everything
to prose. Define typed block variants for at least:

- user message;
- assistant visible content;
- assistant native tool call with call ID, tool name, and structured arguments;
- tool result with the matching call ID, tool name, status, and bounded content;
- private assistant reasoning/thinking data; and
- host lifecycle response metadata.

Persist a transcript checkpoint after every assistant and tool-result block, not
only after the final Discord delivery. This permits crash recovery after a slow
tool call without losing which results the model has already seen. Artifacts are
immutable; update only the relational pointer to the newest valid manifest.

Do not put raw transcript text, tool output, or private reasoning in relational
JSON, queue arguments, telemetry, health diagnostics, or logs. Apply the
existing artifact expiry and owner/channel access boundaries.

### 2. Round-trip native messages losslessly

Change `run_native_tool_loop` so callers can provide an existing ordered native
message transcript. On a new conversation it starts from the current system
policy and the first user message. On a resumed conversation it uses:

1. the current trusted system policy and current-time host context;
2. every persisted message/block in the still-open conversation, in its original
   order; and
3. the new owner message as the final `HumanMessage`.

Do not convert prior tool calls or tool results into a generated summary while
the conversation is open. Tool-call IDs must still match their corresponding
tool-result IDs after serialization and deserialization.

Extend the LLM gateway result contract so it retains provider-native reasoning
metadata when present. Verify how `langchain-ollama` 1.1 and the installed
Ollama client represent thinking content. If LangChain's `AIMessage` does not
round-trip that field on input, introduce a small native Ollama adapter or a
versioned provider envelope at the gateway boundary. Do not silently copy
thinking text into visible assistant content.

Reasoning blocks are private model context. Store and replay them only when the
provider supports doing so, never render them to Discord, and never expose them
through operations pages or exception diagnostics. `OLLAMA_REASONING=false` may
remain the default; the transcript contract must still preserve reasoning when
it is enabled or returned.

### 3. Restore trusted tool state separately from model-visible history

Replaying tool-result text is necessary but not sufficient. The current
academic and career tool-state objects keep verified opaque IDs, discovered
options, inbound-material references, and pending proposed mutations in memory.
Those objects are recreated for the next Discord message.

Define a versioned, host-trusted tool checkpoint containing only the minimum
state required to resume safely, including verified option/capability maps and
pending proposal inputs. Each tool handler emits both:

- a bounded model-visible tool result for the transcript; and
- a typed host-only state delta for the trusted checkpoint.

Rehydrate `_AcademicToolState`, `CareerAgentToolState`, material intake state,
and any other enabled tool collection from the trusted checkpoint before model
replay. Never rebuild authorization maps by parsing model-visible tool-result
JSON. Revalidate stale or write-sensitive targets at mutation/confirmation time
using the existing optimistic and authorization checks.

If a tool has already completed and its result is in the transcript, resumption
must not execute it again merely to reconstruct context. Side-effecting tools
and deliveries must retain their existing idempotency keys so crash replay is
safe.

### 4. Give the model an explicit semantic conversation lifecycle

Add one host-internal terminal native tool, for example
`emit_conversation_response`, with a strict schema:

```json
{
  "disposition": "awaiting_user | completed",
  "content": "the exact owner-visible response"
}
```

The system policy must require this terminal tool after the model has finished
all ordinary tool work for the current turn:

- use `awaiting_user` only when information from the owner is genuinely needed
  before the request can be completed;
- ask one concise, answerable clarification in `content`;
- use `completed` for a final answer, refusal, or terminal explanation;
- consider the entire replayed transcript before deciding, including facts
  already returned by tools and clarification answers already supplied;
- never ask the same semantic clarification after it has been answered; and
- if the owner clearly changes topics while a clarification is open, handle the
  pivot using the full context and choose the appropriate terminal disposition
  rather than blindly treating the new text as a missing slot.

The terminal response tool must be the only tool call in its assistant message.
The harness treats it as a terminal control event and does not send a synthetic
tool result back to the model. It persists the assistant message and lifecycle
event, delivers `content`, and either leaves the session `awaiting_user` or
closes it as `completed`.

Do not use a question-mark heuristic, a keyword classifier, Discord reply shape,
or absence of ordinary tool calls to decide whether a conversation remains
open. If the model omits the terminal tool or mixes it with another tool call,
perform one bounded correction turn using the same transcript. Fail closed with
an honest response if the contract remains invalid.

Exact owner commands such as cancel/start over may remain deterministic host
controls. Expiry, unrecoverable corruption, and operational failure are also
host-owned terminal outcomes. All normal clarify-versus-complete decisions
belong to the model through the typed lifecycle tool.

Retain a bounded clarification safeguard, but do not use it as the continuity
mechanism. If the model produces a normalized duplicate of a previously asked
and answered question, reject that lifecycle output and allow one correction
turn. If it repeats again, close as failed with a concise non-looping message.

### 5. Keep the worker service alive across Discord wakes

Replace per-message construction in `discord_wake_job.py` with a worker-scoped,
lazy-initialized `AcademicDiscordService`. Construct one database/gateway/runtime,
handler, and connector graph per worker process and close it at worker shutdown.
Protect first initialization with an async lock. Tests must be able to inject and
close the service explicitly; do not hide unresettable global state from tests.

Continue passing Ollama `keep_alive` on every request. This avoids needless
model-weight reloads during an active exchange, but do not claim it preserves a
conversation KV cache or make correctness depend on it. A physical model unload,
container restart, or worker restart must still resume from the durable
transcript and trusted checkpoint.

Do not keep an inference coroutine or database transaction open while waiting
minutes or hours for a person to reply. “Do not spin down between messages” means
the application maintains one logical session and reuses its worker-scoped model
client; it does not mean holding a generation request open across human think
time.

### 6. Increase and enforce the native context budget

Raise the configured default native context from 2,048 to 16,384 tokens. Set a
coherent initial budget such as:

- `OLLAMA_NUM_CTX=16384`;
- `OLLAMA_MAX_INPUT_TOKENS=13824`;
- `OLLAMA_MAX_OUTPUT_TOKENS=1024`; and
- a reserved margin of at least 1,536 tokens for tokenizer estimation error and
  native/tool formatting.

Benchmark the pinned Qwen model on the deployment hardware before considering a
32,768-token default. A larger value is acceptable if latency and memory checks
pass, but 16,384 is the minimum configured target for this change.

Add settings validation enforcing:

`max_input_tokens + max_output_tokens + reserve_tokens <= num_ctx`

Count the current system prompt, full active transcript, all native tool schemas,
and the new user input in the preflight estimate. The current native fingerprint
already includes messages and tools and can be evolved into this budget check.
Record estimated and provider-reported token counts without recording content.

Never truncate the active conversation segment while it is waiting for a
clarification. The root request, every clarification turn, and all assistant,
tool, result, and reasoning blocks from that active segment are pinned. Keep
tool outputs bounded at their sources. If the exact active transcript cannot
fit despite those bounds, stop before invoking the model, preserve the session,
and send an actionable context-capacity failure instead of silently dropping
the evidence that would cause another clarification loop.

Completed conversations may later be summarized for optional long-term memory,
but that is outside this active-session fix. A summary is never a fallback for
an open conversation.

## Conversation sequence

```text
owner message
  -> lock/create conversation; append user block
  -> load exact transcript + trusted tool checkpoint
  -> invoke Qwen and append every assistant/tool/result block durably
  -> Qwen calls emit_conversation_response
       awaiting_user -> deliver question; commit state; release all locks
       completed     -> deliver answer; close session; release all locks

owner follow-up
  -> lock same awaiting_user conversation; append user block
  -> rebuild native messages from the complete active transcript
  -> rehydrate trusted tool state
  -> invoke Qwen with prior tool results and reasoning available
  -> clarify again only if new information is genuinely missing, otherwise complete
```

## Architecture replacement and migration

Make the durable native conversation service the sole configured path for
ordinary authorized Discord agent messages. Do not retain the current fresh
`SystemMessage + HumanMessage` path or the reduced
`AcademicAgentClarificationService` summary as a fallback, rollback path, or
alternate runtime.

Preserve existing immutable artifacts/audit records through their retention
period. Existing open legacy clarification rows may be expired during migration
with a safe owner-facing “please resend the complete request” response; do not
pretend that their reduced state can reconstruct missing tool/reasoning blocks.

Keep exact `confirm <proposal-id>` / `reject <proposal-id>` commands and the
existing memory-specific conversation flow working, but route any native model
conversation through the new generic session boundary. Avoid two competing open
session owners for the same Discord user/channel.

## Expected implementation areas

- `app/agents/harness.py`
  - accept a restored transcript, persist events as they occur, and support the
    terminal lifecycle tool;
  - preserve assistant/tool message identity and ordering.
- `app/llm/contracts.py` and `app/llm/gateway.py`
  - add a lossless native message envelope, provider reasoning preservation, and
    coherent context-budget enforcement.
- `app/agents/conversation/` (new)
  - add transcript contracts, lifecycle validation, session/checkpoint service,
    and native message serialization.
- `app/db/models.py`, a focused repository module, and a new Alembic migration
  - add generic conversation metadata, event idempotency, revisions, and open
    owner/channel uniqueness.
- `app/agents/academic_planner/discord_harness.py`
  - load/resume the conversation, pass restored messages into the harness,
    persist each block, and apply the lifecycle disposition.
- `app/agents/academic_planner/discord_service.py`
  - construct the conversation service with the artifact store and database.
- `app/agents/academic_planner/discord_wake_job.py` and worker startup/shutdown
  - replace the per-wake service lifecycle with an injectable worker-scoped
    service provider.
- academic, career, memory, and material tool-state modules
  - add typed trusted checkpoint export/import where state must cross owner
    messages.
- `app/core/config.py`, `.env.example`, `compose.yaml`, scripts, and health checks
  - raise the context defaults, enforce the budget invariant, and expose redacted
    readiness diagnostics.
- `ARCHITECTURE MAIN.md` and the relevant operations runbook
  - document the active session lifecycle, durability boundary, model residency
    distinction, expiry, and recovery behavior.

## Ordered implementation plan

1. Add settings invariants and benchmark the pinned model at a 16,384-token
   context with the production native tool schema set.
2. Add the generic conversation tables, migration, versioned transcript
   manifest, artifact access rules, and repository locking/idempotency methods.
3. Add lossless native message serialization tests, including assistant tool
   calls, matching tool results, failures, multipart visible content, and an
   emitted reasoning/thinking field.
4. Extend the LLM gateway and harness to accept restored messages and checkpoint
   every model/tool block.
5. Add and enforce the terminal `emit_conversation_response` contract and update
   the native system policy.
6. Add trusted checkpoint export/import for every stateful tool collection used
   by the native Discord harness.
7. Wire the conversation service into `NativeAcademicDiscordHandler`, making
   exact transcript replay the only native Discord path.
8. Reuse one `AcademicDiscordService` per worker process and add explicit worker
   shutdown cleanup.
9. Remove/deconfigure the reduced agent-clarification runtime path after any
   required historical-row migration.
10. Add regression, restart, concurrency, context-capacity, and real-interface
    validation; then update architecture and operations documentation.

## Validation requirements

### Unit and contract tests

- A transcript containing user text, assistant content, multiple tool calls,
  tool successes/failures, and provider reasoning round-trips without changing
  order, IDs, arguments, or content.
- Tool schemas plus input/output reserve cannot exceed `num_ctx`; invalid
  combinations fail settings validation.
- `emit_conversation_response(awaiting_user, ...)` leaves one open session;
  `completed` closes it.
- Mixed ordinary and terminal tool calls, a missing terminal call, and duplicate
  already-answered clarification questions fail closed after one correction.
- Raw messages, tool outputs, and reasoning never appear in relational state,
  telemetry, diagnostics, or rendered progress events.

### Active Discord boundary regression

Use a scripted native gateway for this exact two-wake scenario:

1. The owner asks for an operation that causes the model to call at least one
   search tool.
2. The first model turn receives the tool result and emits one clarification.
3. Simulate disposal of all in-memory handler/tool objects or a worker restart.
4. The owner supplies only the missing value.
5. Assert the second model input contains the original user message, prior
   assistant tool call, exact prior tool result, private reasoning metadata,
   clarification response, and new owner answer in order.
6. Assert the model does not repeat the search or clarification and completes
   the intended operation with the same verified target context.

Also cover two sequential clarifications, a user topic pivot, explicit cancel,
duplicate Discord delivery, simultaneous follow-ups, session expiry, transcript
corruption, and context-capacity exhaustion.

### Tool and crash recovery

- Restart after a read-only tool result was checkpointed; resume without
  re-executing the tool.
- Restart after a side-effect/proposal tool completed but before Discord
  delivery; recover idempotently without a duplicate proposal or write.
- Restore verified host capability maps from the trusted checkpoint, never from
  model-visible output.
- Revalidate stale write targets and preserve existing exact-confirmation and
  optimistic-write boundaries.

### Service lifecycle and real interface

- Two separate Discord wakes in the same worker reuse the same service/gateway
  instance.
- A worker restart still resumes from database metadata and artifacts.
- The owner sees the existing acknowledgement before slow work, accurate tool
  progress, one concise clarification, and one final completion message; no turn
  remains indefinitely in `processing`.
- If a configured Discord/Ollama environment is available, run the scenario
  through the real Discord ingress and pinned Qwen model. If it is unavailable,
  report that deployment-level behavior remains unverified.

Run focused Ruff, Pyright, migration, unit, and integration tests, followed by
the full repository suite if the environment supports it.

## Acceptance criteria

- A clarification answer resumes the same logical conversation even after the
  per-message handler returns or the worker restarts.
- The complete active transcript given to the model includes all prior native
  assistant/tool/result blocks and any provider reasoning blocks that were
  returned.
- Host-trusted tool capabilities survive the message boundary without trusting
  model-authored IDs or re-running completed tools solely for reconstruction.
- The model explicitly and semantically selects `awaiting_user` versus
  `completed`; the host does not guess from response text.
- An answered clarification is not asked again, and the bot cannot enter an
  unbounded clarification loop.
- The configured native context is at least 16,384 tokens and all input/output
  budget settings are internally consistent.
- Model/gateway/service objects are worker-scoped, while correctness remains
  restart-safe through durable replay.
- The new conversation architecture is the only configured ordinary native
  Discord path; the lossy summary/fresh-transcript path is not retained as a
  fallback.

## Explicit non-goals

- Do not keep an Ollama HTTP generation request open while waiting for a human.
- Do not rely on Ollama `keep_alive`, an in-memory chat object, or a larger
  context window as the sole continuity mechanism.
- Do not expose or render private chain-of-thought/reasoning content.
- Do not infer clarification state from keywords or punctuation.
- Do not silently truncate, summarize, or omit blocks from an open
  clarification conversation.
- Do not weaken existing Discord authorization, tool validation, proposal
  confirmation, optimistic concurrency, artifact privacy, or delivery
  idempotency controls.
- Do not retain the superseded lossy clarification architecture as an alternate
  runtime path.

## Main implementation risk

The highest-risk mistake is persisting only the visible chat transcript while
forgetting trusted in-memory tool state. That would make the model appear to
remember a prior search result while the host rejects or, worse, improperly
trusts the opaque target IDs on the resumed turn. Transcript replay and trusted
tool-state checkpointing must ship and be validated together.
