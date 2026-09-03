# Phase 2 — shared durable core

## Contract and acceptance criteria

Phase 2 makes PostgreSQL the durable boundary for runs, steps, deliveries,
evidence, health, audit, UI acknowledgements, approvals, queue state, and
LangGraph checkpoints. All timestamps are aware UTC values in storage;
Toronto time is used only to resolve schedules and period keys. Side effects
must have an idempotent durable intent, retries must be deterministic by error
category, model-consuming jobs must not overlap across worker processes, and
stored artifacts must be redacted before their content hash is calculated.

The gate requires proof that:

1. duplicate run and delivery keys create one durable row;
2. transient failures retry with every attempt recorded while invalid
   authorization stops without a loop;
3. an approval pause survives a new PostgreSQL saver/graph instance and
   resumes under the original run ID; and
4. healthy, attention, and failed states come from ordered record rules rather
   than model output.

## Implemented

- Added Alembic revisions `0002_phase2_shared_core` and
  `0003_phase2_integration` for the eight shared UUID-keyed tables, lifecycle
  fields, indexes, foreign keys, check/unique constraints, delivery-uncertain
  state, and approval/run/delivery idempotency.
- Protected `audit_events` with both an insert/read-only repository and a
  PostgreSQL trigger that rejects updates and deletes. Approval transitions
  lock the row, permit one terminal decision, and replay the same decision
  without adding another audit event.
- Added transaction-composable repositories for run/step attempts, delivery
  intents/receipts, approvals, audit, health, and presentation-only
  acknowledgements. Queue attempts store safe codes and lifecycle diagnostics,
  never arbitrary exception text.
- Added `RunContext` and standardized safe error records. Transient,
  authorization, and permanent categories are shared with the queue strategy;
  401/403/invalid-token failures are never retried.
- Added a local `ArtifactStore` with SHA-256 keys, atomic writes, path
  containment, typed sidecars, configurable per-class retention, and
  retain-forever audit/run-summary classes. Text is redacted before hashing;
  binary data requires an explicit already-redacted attestation.
- Added a real Procrastinate app and three queue-specific workers. Successful
  queue rows are pruned after durable LifeAgent records are written; failed and
  stalled jobs remain queryable without returning task arguments.
- Added per-item queueing locks and one shared `ollama:exclusive` execution lock
  for model-consuming tasks. This enforces physical model concurrency one
  across the three processes in addition to Phase 1's per-process semaphore.
- Added minute-level Procrastinate deferrers that load configured Toronto wall
  times, skip nonexistent spring-forward periods, collapse fall-back periods,
  and create stable versioned period keys. Agent work remains disabled until a
  phase-specific handler is registered.
- Added strict PostgreSQL LangGraph checkpoint setup and a minimal side-effect-
  free approval interrupt/resume graph. Pickle fallback and permissive msgpack
  deserialization are disabled.
- Added deterministic operational-health rules, persistence, safe
  failed/stalled queue projections, and a Discord adapter that accepts only
  allowlisted attention/failure alerts. It uses a persisted delivery UUID as
  Discord's enforced nonce and rejects normal-status messages.
- Extended readiness to verify the shared and checkpoint schemas. Workers wait
  for API readiness before starting, then listen only to their assigned queue
  with configurable concurrency (default one).

## Validation

- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed.
- `uv run pyright app scripts/phase1_benchmark.py`: passed with zero errors or
  warnings.
- Unit suite: 51 passed.
- PostgreSQL integration suite: 9 passed against clean disposable PostgreSQL
  16 containers. This includes clean Alembic migration, duplicate suppression,
  append-only audit enforcement, durable lifecycle and health records,
  transient/auth attempt behavior, failed/stalled queue visibility without job
  arguments, and fresh-process approval resume.
- The rebuilt Compose stack reached migration head
  `0003_phase2_integration`; readiness verified all shared/checkpoint tables
  and the exact Phase 1 Ollama identity. All three Procrastinate workers stayed
  up, registered their periodic deferrers, processed disabled-handler ticks,
  and left zero successful tick rows behind.

## Decisions and risks

- Raw application summaries remain relational only when bounded; long output,
  model transcripts, source extracts, PDFs, and receipts use artifact keys.
- The artifact default retention is 30 days to match application settings;
  callers may override by data class. Immutable audit metadata and run
  summaries are retained unless explicitly configured otherwise.
- Queue task results are reduced to safe run/status metadata because
  Procrastinate logs task results. Workflow handlers must persist useful output
  before returning and must never return raw private/licensed content.
- The Discord adapter is implemented and mock-tested but has not sent an
  external message. Real credentials and irreversible external delivery remain
  deferred until a workflow requires them.
- Scheduled finance work has no registered handler and no source connector.
  The exact Phase 6 source/licence gate remains closed.

## Next phase

Phase 3 implements one repository end to end: pinned checkout, normalized
change packet, deterministic scanners, model finding proposal/validation,
report artifact, and idempotent Discord summary. GitHub inline comments remain
disabled until their explicit quality gate passes.
