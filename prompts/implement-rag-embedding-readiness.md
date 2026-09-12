# Implement RAG embedding readiness with qwen3-embedding:4b

## Objective and outcome

Make the academic RAG/memory path use a dedicated embedding model, `qwen3-embedding:4b`, as the sole configured default. The user-visible outcome is that LifeAgent's academic memory and assessment-material retrieval are fast semantic retrieval paths when deployed, and service readiness visibly fails or degrades when the embedding model is missing instead of silently falling back to empty semantic results.

This plan intentionally keeps the chat/planning model as `qwen3-32gb:latest`. Do not try to make `qwen3-32gb:latest` serve embeddings unless the user explicitly changes this decision; the accepted correction is a dedicated embedding model plus first-class readiness/backfill.

## Requirements

- Replace all active defaults for the legacy `qwen3-embedding:0.6b` with `qwen3-embedding:4b`.
- Keep `OLLAMA_MODEL=qwen3-32gb:latest` as the agent's reasoning/chat model.
- Wire the same `AcademicEmbeddingGateway` into API and worker construction paths.
- Treat embedding availability as a first-class runtime/deployment requirement for academic RAG and memory.
- Add a re-embedding/backfill path for rows with no embedding, legacy embedding model identity, or stale embedding config version.
- Preserve existing privacy boundaries: no raw private text in logs, queue arguments, health diagnostics, or model telemetry beyond the already-stored private database rows.
- Do not retain the legacy `qwen3-embedding:0.6b` as a fallback or alternate runtime path.

## Non-goals

- Do not replace the main Qwen chat model.
- Do not add cloud embeddings.
- Do not broaden academic memory to finance, code-review, job-interview, or general assistant surfaces.
- Do not use legacy Markdown/docs as evidence of current runtime behavior.
- Do not add lexical fallback as the primary answer to missing embeddings; readiness/backfill should make the semantic path operational.

## Current repository context

- Active settings currently default `ollama_model` to `qwen3-32gb:latest` and `embedding_model` to `qwen3-embedding:0.6b` in `app/core/config.py`.
- Compose and `.env.example` mirror the legacy embedding default in `compose.yaml` and `.env.example`.
- `AcademicEmbeddingGateway` already centralizes embedding through Ollama and records model identity/config version without retaining raw text in telemetry: `app/llm/embeddings.py`.
- API construction already creates one `AcademicEmbeddingGateway` and passes it into `SQLAlchemyAcademicPlannerStore` and material ingestion: `app/main.py`.
- Worker-side Discord construction currently builds `SQLAlchemyAcademicPlannerStore` without an embedding gateway, so semantic focus/material search is unavailable in that worker path: `app/agents/academic_planner/discord_service.py`.
- Semantic learning-focus search returns no candidates if no gateway is configured or embedding fails: `app/db/academic.py`.
- Assessment material ingestion fails a material indexing job if chunk embedding fails, and material semantic search uses the stored embedding model identity: `app/agents/academic_planner/material_ingestion.py` and `app/db/academic.py`.
- `/health/ready` includes the generic Ollama check but does not separately require the configured embedding model: `app/api/health.py` and `app/health/checks.py`.
- Host Ollama start/status scripts only parse, pull, and verify `OLLAMA_MODEL`/`OLLAMA_MODEL_DIGEST`: `scripts/ollama_qwen_start.sh` and `scripts/ollama_qwen_status.sh`.

## Files expected to change

- `app/core/config.py`
- `.env.example`
- `compose.yaml`
- `app/llm/ollama_runtime.py`
- `app/llm/embeddings.py` if a small probe helper fits better there than in `ollama_runtime.py`
- `app/health/checks.py`
- `app/api/health.py` only if the readiness call signature needs the app-state embedding gateway/runtime
- `app/main.py`
- `app/agents/academic_planner/discord_service.py`
- `scripts/lifeagent_launchd_common.sh`
- `scripts/ollama_qwen_start.sh`
- `scripts/ollama_qwen_status.sh`
- A new focused backfill/reindex entrypoint, preferably under `scripts/` or an existing app-admin pattern if one exists after fresh inspection
- Focused tests in `tests/unit/test_llm_embeddings.py`, `tests/unit/test_ollama_runtime.py`, `tests/unit/test_ollama_qwen_scripts.py`, `tests/unit/test_academic_native_discord_harness.py`, `tests/unit/test_academic_memory_review.py`, `tests/unit/test_academic_material_ingestion.py`, `tests/unit/test_academic_repository.py`, and/or `tests/unit/test_academic_main.py`

Do not edit unrelated frontend files. If concurrent work has changed any listed file, inspect it first and preserve unrelated changes.

## Implementation steps

1. Update configuration defaults.
   - Change the `Settings.embedding_model` default from `qwen3-embedding:0.6b` to `qwen3-embedding:4b`.
   - Update `.env.example` and `compose.yaml` to the same default.
   - Leave `EMBEDDING_MODEL_DIGEST` empty by default until a local pull verifies the digest; pin it only if the operator intentionally records the installed digest.

2. Add embedding-model runtime verification.
   - Extend the existing Ollama model verification logic so it can verify both the chat model and embedding model against `/api/tags`.
   - Keep diagnostics redacted and non-secret.
   - Distinguish failure codes enough for operators/tests, for example `model_missing` versus `embedding_model_missing` and digest mismatch variants.
   - Ensure the readiness check validates `settings.embedding_model` and `settings.embedding_model_digest` in addition to `settings.ollama_model`.

3. Make `/health/ready` expose embedding readiness.
   - Add a health check named clearly, such as `ollama_embedding` or `academic_embeddings`.
   - Missing or digest-mismatched embedding model should make readiness failed if academic memory/RAG is enabled, not merely invisible.
   - Keep generic liveness `/health/live` unchanged.

4. Wire embeddings into the Discord worker store.
   - In `create_academic_discord_service`, instantiate `AcademicEmbeddingGateway(app_settings)` once and pass it into `SQLAlchemyAcademicPlannerStore`.
   - Reuse that store for `agent_catalog` so `search_semantic_focuses` and `search_semantic_assessment_materials` work in the native Discord path.
   - Do not create a second embedding client per tool call.

5. Extend host Ollama scripts.
   - Add defaults for `EMBEDDING_MODEL=qwen3-embedding:4b` and optional `EMBEDDING_MODEL_DIGEST`.
   - Make `scripts/ollama_qwen_start.sh --pull` pull whichever configured models are missing: chat model and embedding model.
   - Make `scripts/ollama_qwen_status.sh` report both model install/digest states and, if checking residency, avoid naming everything `qwen_resident` when the checked model may be the embedding model.
   - Keep compatibility with `.env` overrides and existing tests' fake tool environment.

6. Add explicit backfill/reindex support.
   - Provide a safe operator command that can re-embed:
     - `academic_reflection_memory` rows with null embeddings or model identity not equal to the current `AcademicEmbeddingGateway.model_identity`.
     - `academic_document_chunks` rows with null embeddings or model identity not equal to the current gateway identity.
   - Prefer batching with bounded counts and dry-run output first.
   - Do not put raw text in command output.
   - For assessment material chunks, either update chunks in place when the source text is present and unchanged, or requeue existing material ingestion jobs by stable page/fingerprint if that better fits repository patterns after inspection.
   - Ensure the command can be run after deployment to migrate existing stored memory/materials to `qwen3-embedding:4b`.

7. Add tests.
   - Settings/default tests proving the new embedding default is `qwen3-embedding:4b`.
   - Runtime tests proving Ollama readiness verifies both configured models and reports typed failures.
   - Health tests proving `/health/ready` includes embedding readiness and fails when required embeddings are missing.
   - Worker construction test proving `create_academic_discord_service` passes an embedding gateway into the academic store.
   - Script tests proving start/status parse `EMBEDDING_MODEL`, pull missing embeddings under `--pull`, and report embedding model status.
   - Backfill tests proving stale/null embedding rows are selected and updated/requeued without leaking raw text.

8. Update operational docs only after the behavior is implemented.
   - Document `EMBEDDING_MODEL=qwen3-embedding:4b`, optional digest pinning, `scripts/ollama_qwen_start.sh --pull`, readiness expectations, and the one-time backfill command.
   - Avoid documenting `qwen3-embedding:0.6b` except as historical migration context if necessary.

## Validation

Run focused validation first:

```bash
uv run pytest tests/unit/test_llm_embeddings.py tests/unit/test_ollama_runtime.py tests/unit/test_ollama_qwen_scripts.py
uv run pytest tests/unit/test_academic_native_discord_harness.py tests/unit/test_academic_memory_review.py tests/unit/test_academic_material_ingestion.py tests/unit/test_academic_repository.py tests/unit/test_academic_main.py
bash -n scripts/lifeagent_launchd_common.sh scripts/ollama_qwen_start.sh scripts/ollama_qwen_status.sh
```

Then run broader validation if the focused suite passes:

```bash
uv run pytest
```

For local runtime validation after code changes:

```bash
scripts/ollama_qwen_start.sh --pull
scripts/ollama_qwen_status.sh
docker compose up -d --build api worker-academic-planner
curl -fsS http://127.0.0.1:8000/health/ready
```

Acceptance criteria:

- No active default references `qwen3-embedding:0.6b`.
- Startup/status scripts verify or pull `qwen3-embedding:4b`.
- `/health/ready` exposes embedding readiness and fails when the configured embedding model is unavailable while academic memory/RAG is enabled.
- API and Discord worker stores both have the embedding gateway.
- Semantic memory/material search returns candidates when rows have current embeddings.
- Existing stale or missing embeddings can be backfilled in a bounded, non-leaky way.
- Tests cover the new readiness, wiring, scripts, and backfill behavior.

## Migration and compatibility considerations

- Existing rows embedded with `qwen3-embedding:0.6b` should be treated as stale, because vector dimensions/model identity may differ from `qwen3-embedding:4b`.
- Model identity checks already scope assessment material retrieval by `embedding_model`, so old material chunks will not satisfy new-model searches until re-embedded.
- Learning-focus semantic search currently filters by dimensions but not model identity in the observed code path; the implementation should tighten this if the schema has model identity available, or explicitly justify why dimensions are sufficient. Prefer matching the current `AcademicEmbeddingGateway.model_identity` for memory too.
- If `EMBEDDING_MODEL_DIGEST` is pinned, the operator must refresh it after pulling a new local model build.
- Backfill should be idempotent and resumable; interruption should not corrupt existing memory/material records.

## Known risks and unresolved decisions

- The exact digest for `qwen3-embedding:4b` is deployment-local until the model is pulled and `/api/tags` reports it.
- Backfilling private reflection memory requires reading raw private text already stored in the database; the command must keep it local and never print it.
- If concurrent work has changed the academic memory, material ingestion, or launchd scripts, re-read those files before implementation and narrow edits accordingly.
- If the user later insists on modifying `qwen3-32gb:latest` to emit embeddings, escalate before implementing; that is a different architecture than the accepted correction.
