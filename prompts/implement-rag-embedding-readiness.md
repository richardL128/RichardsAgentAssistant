# Implement the academic RAG and durable-memory correction

Status: implemented and validated in the checkout. Host model pull, live
migration/backfill, canonical deployment, and real Discord acceptance remain.

## Objective and user-visible outcome

Make LifeAgent's academic “brain” an operational local vector-RAG system:

- assessment pages, PDFs, and learning reflections are embedded once and retained in PostgreSQL/pgvector;
- a question is embedded once, matched against a bounded scoped vector index, and only the best cited memories are sent to the main agent;
- `qwen3-32gb:latest` remains the reasoning/chat model and is never used for bulk indexing or similarity search;
- `qwen3-embedding:4b` becomes the sole embedding default;
- missing, incompatible, or stale embeddings are reported clearly instead of being silently treated as “no memories found”;
- learning-focus/reflection memory works through the active native Discord handler, including durable clarification, review, correction, snooze, and deletion.

The result must preserve the two reasons for RAG: fast retrieval over a growing corpus and a small, bounded context sent to the expensive reasoning model.

## Fixed architecture decisions

1. Keep `OLLAMA_MODEL=qwen3-32gb:latest` as the agent model.
2. Use `EMBEDDING_MODEL=qwen3-embedding:4b` as the only embedding default. Do not retain `qwen3-embedding:0.6b` as a fallback or alternate path.
3. Request 1,024-dimensional embeddings from the 4B model. Qwen3-Embedding supports dimension reduction, 1,024 dimensions fit pgvector's indexed `vector` limit, and this bounds storage/index memory.
4. Use the same embedding tag, digest, dimension, normalization, and input-policy version for indexing and querying.
5. Use fixed-dimension pgvector columns and HNSW cosine indexes for assessment chunks and reflection memories. Exact/SQLite cosine remains test-only.
6. Vector retrieval is the production default. Do not silently fall back to lexical-only retrieval when embeddings fail. Existing lexical APIs may remain for explicit keyword search or tests, but not as a hidden semantic-RAG replacement.
7. Integrate memory into `NativeAcademicDiscordHandler`, the sole active Discord architecture. Do not restore the superseded `AcademicDiscordCheckinHandler` as an alternate path.
8. Reuse `AcademicMemoryService` and its durable persistence/idempotency rules through a narrow native adapter.
9. General non-memory requests may continue when the embedder is unavailable, but memory writes and semantic searches fail explicitly. `/health/ready` fails while required academic embeddings are unavailable; `/health/live` remains unchanged.

## Requirements

### Embedding and retrieval

- Configure the 4B embedder, 1,024 dimensions, optional pinned digest, timeout, and bounded keep-alive.
- Verify the installed tag, optional digest, embedding capability, and a bounded probe returning exactly 1,024 finite values.
- Batch document embeddings during ingestion rather than issuing one request per chunk.
- Store model identity and dimension with every vector; queries require the current identity and dimension.
- Use HNSW cosine ordering while enforcing assessment/activity/privacy or owner/channel/status filters in the host/database.
- Keep retrieval to the configured top eight results with source citations.
- Never send the complete corpus to the 32B model.

### Durable learning memory

- Support reflection creation, reinforcement, review, replacement, snoozing, and deletion through the active native Discord path.
- Preserve owner-user and owner-channel scope, external-event idempotency, revision checks, and durable multi-turn clarification.
- New, replacement, or reinforced memory must not commit if its embedding fails; do not partially store new raw reflection text without its searchable vector.
- Read/review may display already persisted memory when no new semantic query is needed. Destructive deletion retains explicit-intent, scope, and revision checks.
- Mixed requests clearly distinguish an applied local memory action from a Notion proposal awaiting confirmation.

### Conversational behavior

- Acknowledge memory/material work before slow calls begin.
- Update the existing progress message for embedding, retrieval, indexing, or model work without flooding Discord.
- Finish with a specific success, clarification, or failure; never leave a progress message indefinitely working.
- On embedding failure, say semantic memory is unavailable and no new memory was stored; never claim the owner has no memories.
- Keep raw private text, vectors, credentials, prompts, and stack traces out of logs, queue arguments, health output, and progress messages.

### Operations and migration

- Host start/status verifies both the reasoning model and embedder.
- `--pull` may install missing configured models; normal start/status never pulls unexpectedly.
- Provide a bounded, idempotent, resumable, dry-run-capable re-embedding command for stale/missing vectors.
- Provide corpus bootstrap/recovery for synchronized assessments with no active material documents.
- Deploy only through the canonical installed-runtime workflow.

## Explicit non-goals

- Do not fine-tune or modify `qwen3-32gb:latest` into an embedder.
- Do not replace the reasoning model or add cloud embeddings.
- Do not broaden memory to finance, code review, career, or arbitrary conversations.
- Do not add a second Discord handler, rollback runtime, or legacy-model fallback.
- Do not expose unrestricted memory mutation tools or model-controlled owner IDs.
- Do not redesign unrelated calendar, finance, career, frontend, or Notion proposal behavior.

## Current repository context

The executing session must re-read the worktree before editing. Current active evidence:

- Discord ingress follows `app/api/discord_handoff.py` → `app/queue/tasks.py` → `app/agents/academic_planner/discord_wake_job.py` → `create_academic_discord_service()` → `NativeAcademicDiscordHandler`.
- `app/main.py` gives the API-side store an `AcademicEmbeddingGateway`; `app/agents/academic_planner/discord_service.py` currently omits it from the worker-side store.
- `app/llm/embeddings.py` owns local Ollama embedding calls and safe telemetry.
- `app/agents/academic_planner/material_ingestion.py` extracts, chunks, embeds, versions, and activates material but embeds chunks sequentially.
- `app/db/academic.py` contains assessment- and owner-scoped cosine retrieval, currently exact over variable-dimension columns.
- `app/db/models.py` stores variable-dimension vectors for `AcademicDocumentChunk` and `AcademicReflectionMemory`.
- `app/agents/academic_planner/memory_workflow.py` already implements durable focus/reflection behavior, scoping, clarification, revisions, and idempotency.
- The native handler has material tools but no learning-memory integration. `discord_checkin.py` is not the active worker and must not become a fallback.
- Health/runtime scripts verify only the reasoning model today.
- `app/core/config.py`, `.env.example`, and `compose.yaml` still default to the 0.6B embedder.

## Expected files and components

Reconfirm this list and preserve unrelated user changes.

- Configuration/runtime: `app/core/config.py`, `.env.example`, `compose.yaml`, `app/llm/embeddings.py`, `app/llm/ollama_runtime.py` or a focused new embedding runtime, `app/main.py`, `app/health/checks.py`, `app/api/health.py` if needed, and `app/host/settings.py`.
- Persistence/retrieval: `app/db/models.py`, `app/db/academic.py`, a new migration after the actual current head (expected `0024_academic_embedding_hnsw.py` if `0023` remains head), `material_ingestion.py`, and a focused backfill service such as `embedding_backfill.py`.
- Active Discord memory: `discord_service.py`, `discord_harness.py`, `memory_workflow.py`, and optionally a small `discord_memory.py` adapter plus existing Discord progress rendering.
- Queue/operations: `app/queue/tasks.py` and `worker.py` only if backfill is durable-queued; `lifeagent_launchd_common.sh`, `ollama_qwen_start.sh`, `ollama_qwen_status.sh`, `lifeagent_host_runtime.sh`, and a safe operator script.
- Tests: embedding/runtime/host-script/health tests; material ingestion/retrieval/repository tests; memory workflow/review/native Discord/wake tests; and PostgreSQL integration coverage.
- Update structural and operational documentation only after implementation and validation.

## Ordered implementation plan

### 1. Establish a baseline

- Inspect `git status`, the actual migration head, and overlapping user changes.
- Run focused existing embedding, retrieval, memory, Discord, health, script, and persistence tests.
- Record baseline failures separately; never rewrite unrelated changes.

### 2. Replace and fully specify embedding configuration

- Set `qwen3-embedding:4b` in settings, Compose, `.env.example`, host defaults, and tests.
- Add validated `EMBEDDING_DIMENSIONS=1024` and a bounded embedding keep-alive setting.
- Keep the example digest optional; pin the installed digest in deployment `.env` after pulling.
- Include tag, digest, dimension, normalization/input-policy version, timeout, and keep-alive in the embedding configuration identity/fingerprint.

### 3. Harden and batch the embedding boundary

- Configure `OllamaEmbeddings` with dimensions, timeout, URL, and keep-alive.
- Add a coalesced/cached readiness method verifying tag/digest, embedding capability, and one bounded dimension/finite-value probe.
- Add a batch method around `aembed_documents()` that atomically validates count, dimensions, numeric types, and finite values.
- Keep one-query methods for semantic searches.
- Return typed errors for missing model, digest mismatch, unsupported capability, wrong dimension, timeout, invalid vector, and model failure.

### 4. Make the host lifecycle own both models

- Extend host environment parsing/defaults for the embedding tag, digest, dimensions, and keep-alive.
- Make `ollama_qwen_start.sh` verify both models. With `--pull`, pull only missing tags and re-verify; without it, fail actionably without mutation.
- Make `ollama_qwen_status.sh` report the two model roles separately.
- Compose or extend `OllamaRuntime` so reasoning readiness and embedding readiness remain distinguishable.

### 5. Make readiness truthful

- Add `academic_embeddings` to `/health/ready`.
- Missing tag, wrong digest, missing capability, bad probe/dimension, or required stale backfill sets this check to failed and readiness to HTTP 503.
- Leave `/health/live` unchanged.
- Do not call embeddings healthy merely because `/api/tags` responds; emit only safe error codes/identities/counts.

### 6. Add a fixed, indexed vector schema

- Create a migration after the actual head.
- Convert both PostgreSQL embedding columns to `vector(1024)` and update ORM models to `Vector(1024)`, retaining SQLite JSON variants.
- Before conversion, clear only incompatible derived vectors while preserving document/reflection text, focuses, citations, and audit history; null rows become backfill candidates.
- Add conservative HNSW cosine indexes for chunks and reflection memories.
- Make PostgreSQL queries use the cosine operator/expression matching those indexes while retaining every scope filter.
- Require current embedding model identity in learning-focus search, not merely matching dimensions.
- Add an `EXPLAIN` integration check/benchmark showing HNSW is selected on a sufficiently large fixture.

### 7. Make assessment-material RAG operational

- Pass one shared `AcademicEmbeddingGateway` into the canonical Discord worker store.
- Embed bounded batches and activate a document version only when every chunk embedding validates.
- Preserve source versioning, privacy, activity rules, and citations.
- Remove the native tool's silent semantic-to-lexical fallback. A typed failure means unavailable; an empty success means no semantic candidates.
- Preserve top-eight and per-chunk bounds before context reaches the reasoning model.

### 8. Wire durable learning memory into the native handler

- Construct one `AcademicMemoryService` in `create_academic_discord_service()` using the shared store, reasoning gateway, embedding gateway, timezone, and practice defaults.
- Add a narrow native memory adapter/tool. The model may choose `reflection` or `review`, but the host supplies the authenticated original message, event ID, user ID, channel ID, and timestamp. Never accept scope or raw replacement text from model arguments.
- Let the adapter call existing `AcademicMemoryService` methods and return only bounded safe statuses/responses.
- Before the general loop, directly resume any open owner/channel memory session so a short clarification answer cannot be misrouted.
- For new requests, the native agent invokes the adapter for explicit academic remember/review/correct/snooze/forget/reflection intent. `AcademicMemoryService` remains the final applicability/mutation validator.
- Deliver a memory result exactly once; avoid both direct and native-final duplicate responses.
- Add memory lookup/update progress phases.
- Preserve immediate local-memory behavior and separate it clearly from human-confirmed Notion writes in mixed turns.
- Roll back create/reinforce/replace when embedding fails; preserve safe review/deletion and durable idempotency.

### 9. Add bounded backfill and empty-corpus recovery

- Add a dry-run-first command reporting counts only for stale/null/wrong-dimension document and reflection vectors plus synchronized assessments lacking active indexed material.
- Re-embed stored text locally in bounded batches without printing text, vectors, URLs, or owner IDs.
- Make updates resumable with short transactions and safe row locking; write identity/dimension only after a batch validates.
- For empty assessments, enqueue normal stable-fingerprint ingestion after sync instead of inventing documents in backfill.
- Do not automatically replay historical failures until embedding readiness is healthy.

### 10. Update preflight and operations

- Add focused embedding, native-memory, schema/index, and backfill tests to canonical deploy preflight.
- Document both model roles, pull/digest pinning, readiness, backfill/bootstrap, and recovery only after behavior works.
- Never document the 0.6B model as a runtime alternative.

### 11. Deploy canonically

- Pass focused and full validation.
- Run `scripts/ollama_qwen_start.sh --pull`, capture the 4B embedder digest, and pin it in deployment `.env`.
- Run migration and backfill/bootstrap in the order enforced by deploy tooling.
- Deploy with `scripts/lifeagent_host_runtime.sh deploy`, not an ad hoc checkout-only Compose command.
- Verify API, worker, Postgres, Ollama, LaunchAgent ingress, migration head, queue state, and embedding readiness.
- Exercise real Discord reflection, semantic recall, cited material, review/correction, and deletion flows.

## Migration and compatibility rules

- The new embedding architecture is the sole configured/documented default.
- Preserve raw content, learning focuses, citations, and audit/lifecycle events.
- Embeddings are derived data: incompatible vectors may be cleared/regenerated but never interpreted under the new identity.
- Existing 0.6B vectors remain stale even if they are also 1,024-dimensional.
- Queries never mix model identities; a digest change also marks vectors stale.
- Interrupted backfill leaves completed rows valid and remaining rows visibly stale.
- Do not retain the older Discord handler as rollback or backup.

## Validation plan

### Automated validation

```bash
bash -n scripts/lifeagent_launchd_common.sh scripts/ollama_qwen_start.sh scripts/ollama_qwen_status.sh scripts/lifeagent_host_runtime.sh
.venv/bin/python -m pytest \
  tests/unit/test_llm_embeddings.py \
  tests/unit/test_ollama_runtime.py \
  tests/unit/test_ollama_qwen_scripts.py \
  tests/unit/test_host_settings.py \
  tests/unit/test_academic_material_ingestion.py \
  tests/unit/test_academic_retrieval.py \
  tests/unit/test_academic_repository.py \
  tests/unit/test_academic_memory_workflow.py \
  tests/unit/test_academic_memory_review.py \
  tests/unit/test_academic_native_discord_harness.py \
  tests/unit/test_discord_wake_job.py
.venv/bin/python -m pytest
```

### PostgreSQL integration validation

- Migrate a database containing null, old-model/1,024-dimensional, and incompatible vectors; prove raw records/history survive.
- Prove stale vectors are excluded before backfill and eligible rows are current afterward.
- Test assessment/privacy/activity and owner/channel boundaries adversarially.
- Compare HNSW top-eight results with exact cosine and record recall/agreement.
- Use `EXPLAIN (ANALYZE, BUFFERS)` on a large non-production fixture to confirm indexed retrieval.

### Real local-model validation

- `/api/show` advertises embedding capability and a probe returns 1,024 finite values.
- Indexing uses batches and each semantic query uses one query embedding.
- The 32B model is never invoked for ingestion, backfill, or similarity ranking.
- Record cold/warm embedding latency, batch throughput, DB retrieval time, total memory-turn time, and model residency on the target Mac.
- Use five seconds as the initial warm query-embedding ceiling, then tighten it if the measured baseline supports it.

### Real Discord validation

1. A new learning reflection is acknowledged, embedded, saved once, and confirmed naturally.
2. A differently worded later request retrieves it semantically without exposing IDs.
3. “Show me what you remember about my studying” enters durable review.
4. Correction replaces stale text/vector atomically.
5. Explicit deletion removes only the owned focus/reflection while preserving unrelated history as designed.
6. A material question returns bounded citations from the correct assessment.
7. A mixed memory/Notion request distinguishes applied local memory from a pending proposal.
8. Duplicate delivery creates no duplicate memory, response, or proposal.
9. Missing embeddings produce acknowledgement, honest terminal failure, and no partial write.
10. Slow calls update the existing progress message and always terminate.

## Acceptance criteria

- No active config, script, or documentation defaults to the 0.6B embedder.
- Reasoning stays on `qwen3-32gb:latest`; embedding uses `qwen3-embedding:4b` at 1,024 dimensions.
- Startup/status verifies both models/capabilities/dimensions/digests and normal start never silently pulls.
- Missing or stale required embeddings make `/health/ready` return 503 while liveness remains intact.
- API and worker share the correctly configured embedding boundary.
- Both corpora have HNSW cosine indexes and current-identity scoped production searches.
- Semantic failure never silently becomes lexical-only.
- Ingestion/backfill is bounded, resumable, and non-leaky.
- The active native handler supports durable owner-scoped memory without enabling the old handler.
- New/reinforced/replaced memory is atomic with embedding success.
- The agent receives at most eight bounded retrieved chunks.
- Focused/full/migration/real-model/real-Discord validation passes.

## Known risks and resolved choices

- **Model residency:** the reasoning model is about 18 GB and the 4B embedder about 2.5 GB locally. Benchmark keep-alive pressure; do not switch to 8B without a separate decision.
- **Approximate recall:** HNSW trades exactness for speed. Tune it against exact cosine rather than removing vector retrieval.
- **Dimension reduction:** 1,024 is intentional. Any later dimension change requires versioned re-embedding.
- **Routing:** the native adapter avoids an extra classifier call on every message, but tool-selection errors remain possible. Authenticated original input and `AcademicMemoryService` remain host-side safeguards.
- **Mixed effects:** local memory may commit while a Notion proposal remains pending; responses/tests must make this unmistakable.
- **Dirty worktree:** inspect and preserve current user changes and select the actual next migration revision.

There are no unresolved product decisions required to begin after explicit approval.
