# Phase 4 — Code review: account-scale ingestion and daily operation

## Contract and acceptance criteria

Phase 4 expands the Phase 3 exact-SHA review pipeline across every explicitly
allowlisted repository in one GitHub App installation. Discovery and project
profiling are durable and resumable, daily consolidation uses Toronto calendar
boundaries, and every report contains only actionable, non-dismissed findings
plus an artifact reference and durable delivery receipt. Inline GitHub comments
remain disabled.

## Implemented

- Added installation-scoped GitHub discovery, exact default-branch SHA
  resolution, paginated durable discovery state, `FOR UPDATE SKIP LOCKED`
  one-at-a-time profile claims, interruption recovery, and serial profile
  ingestion through `code_review_ingest`.
- Added repository discovery/profile lifecycle columns, finding dismissals,
  daily reports, review triggers, and history indexes in Alembic revision
  `0005_phase4_operations` and the SQLAlchemy model/repository layer.
- Added human profile-review state and configurable stale-profile refresh.
- Added DST-aware Toronto midnight-to-now and bounded catch-up windows, nightly
  consolidation at the configured 18:00 local schedule, actionable finding
  filtering, and stable Markdown/JSON reports aimed at a coding harness.
- Added signed-webhook path risk classification. High-risk pushes are durably
  marked as `quick_scan` while retaining the repository/SHA idempotency key.
- Added daily and ingestion queue job kinds and worker-only handler
  registration. Queue return values contain identifiers, counts, statuses, and
  artifact keys only.
- Added idempotent Discord daily-report delivery. The persisted delivery UUID
  is sent as the enforced nonce; sent, failed, and uncertain receipts are
  recorded before the operation returns.
- Closed a Phase 3 security gap found by the real-repository acceptance test:
  credential-like source assignments are redacted before JSON serialization
  into the model prompt.

## Acceptance evidence

- The real local Git fixture covers regression, leaked-secret, documentation-
  only, replay, exact-SHA, finding de-duplication, report-only delivery, and
  secret-redaction behavior.
- Discovery tests prove page resume, one-at-a-time profiles, reclaimed
  interrupted claims, and no duplicate profile/archive work on replay.
- Operations tests cover Toronto spring/fall DST boundaries, daily selection,
  bounded catch-up, non-dismissed actionable reports, delivery receipts, and
  high-risk quick-scan identity.
- Repository tests cover dismissal reasons, profile review/refresh state, daily
  report idempotency, and terminal run persistence.
- `ruff check`, `ruff format --check`, strict `pyright app`, and the combined
  unit/acceptance suite passed. PostgreSQL integration tests upgraded Alembic
  through revision `0005_phase4_operations` and passed.

## Deferred external verification

- Live GitHub App discovery/webhook calls and live Discord delivery require the
  user's scoped credentials; automated tests use injected HTTP/provider fakes.
- The Phase 3 20-commit manual inline-comment gate remains deliberately closed.
  No inline comment capability or schema permission has been enabled.

## Next phase

Phase 5 adds the scoped Notion academic planner, cited PDF/full-text retrieval,
deterministic 7–14 day allocation, morning/EOD Discord interaction, and
confirmation-only Notion writes. Embeddings and `pgvector` remain deferred.
