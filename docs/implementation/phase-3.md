# Phase 3 — Code review: one repository end to end

> Historical phase note: this describes the original Phase 3 implementation.
> The current configured runtime does not mount the GitHub model-queue webhook,
> register code-review tasks, or provide a code-review worker. The sole model
> queue ingress is the authorized academic Discord handoff.

## Contract and acceptance criteria

Phase 3 builds one complete code-review pipeline for a single allowlisted
GitHub repository: signed webhook intake, an exact-SHA checkout, a bounded
diff/context packet, isolated deterministic scanners, a model finding
proposal validated strictly against packet-owned facts, a redacted report
artifact, and an idempotent Discord summary. GitHub inline comments are not
built in this phase; only Discord/report-only delivery exists.

The plan's acceptance tests require that:

1. a fixture repository with a seeded regression, a leaked test secret, and a
   documentation-only commit produces attributable, de-duplicated findings
   and suppresses vague/document-only speculation;
2. a push replay is deduplicated by repository/SHA, and an exact SHA is
   checked out rather than the branch tip;
3. scanner/checkout timeout and clone failure create attention/failed
   records with usable diagnostics and do not block other jobs; and
4. Discord delivery is idempotent, and no GitHub comment is created in this
   phase.

The plan's gate calls for evaluating at least 20 representative commits
manually and enabling a narrow, high-confidence inline GitHub-comment policy
only if false positives and incorrect line references meet an agreed
threshold; otherwise Discord/report-only delivery is retained.

## Implemented

- Added `app/agents/code_review/contracts.py`: strict Pydantic contracts for
  every stage boundary (`PushEvent`, `ChangedFile`, `ScannerFinding`,
  `ScannerRun`, `ReviewPacket`, `FindingProposal`, `FindingBatch`,
  `ValidatedFinding`, `ProjectProfile`). Repository names, SHAs, and
  repository-relative paths are validated at every boundary; `ReviewPacket`
  enforces a bounded total character budget across patches and scanner
  evidence.
- Added `app/agents/code_review/repository.py`: shell-free, credential-free
  Git checkout into an ephemeral temporary directory. It validates the
  repository against an explicit allowlist and the requested value against
  the SHA pattern before any process is spawned, clones with
  `--no-checkout --filter=blob:none`, checks out `--detach <sha>`, then
  verifies `rev-parse HEAD` equals the requested SHA before yielding the
  checkout path. Output is bounded, timeouts kill the whole process group,
  and the directory is always removed on the way out, including on failure.
- Added `app/agents/code_review/packet.py`: assembles a `ReviewPacket` from
  `git diff --name-status -z --find-renames` and per-file
  `git diff --no-ext-diff --binary --full-index`, deriving changed-line
  numbers from unified-diff hunk headers, truncating patches at a character
  limit, and rejecting unsafe or control-character paths. It calls
  `risk.classify_risk` to attach a deterministic risk level and reasons.
- Added `app/agents/code_review/risk.py`: a pure, path-token-based classifier
  that marks auth/payment/migration/infra/lockfile-style paths high risk,
  documentation/format/generated-only changes low risk, and everything else
  medium risk, with deterministic, sorted, deduplicated reasons.
- Added `app/agents/code_review/scanners.py`: bounded, no-shell subprocess
  runners for Semgrep, Gitleaks, Trivy, and an allowlisted set of native
  commands (`cargo`, `go`, `make`, `npm`, `pnpm`, `pytest`, `ruff`). Each
  scanner fails independently (`succeeded` / `findings` / `timed_out` /
  `unavailable` / `failed`) with bounded output and a stable diagnostic
  code, and one scanner's timeout or failure never blocks the others.
  Gitleaks output is deliberately never carried into title/evidence/
  fingerprint fields — only rule/path/line survive, with a fixed
  "Secret content redacted" message — and process output is passed through
  `redact_text` plus any caller-supplied secret substitution before JSON
  parsing.
- Added `app/agents/code_review/findings.py`: deterministic validation and
  merge logic. `validate_finding` rejects any model proposal whose path/line
  is not a changed line in the packet, whose `evidence_refs` do not resolve
  to packet-owned evidence IDs, whose confidence is below
  `MIN_FINDING_CONFIDENCE` (0.60), or whose text is vague/non-actionable (an
  action-verb regex gate) or speculative on a documentation-only path.
  `merge_findings` deduplicates by fingerprint plus a text-similarity/word-
  overlap threshold, keeping the strongest evidence. `scanner_findings`
  normalizes scanner output into the same `ValidatedFinding` shape so
  scanner- and model-sourced findings share one downstream path.
- Added `app/agents/code_review/report.py`: renders a redacted, stable-order
  Markdown or JSON report from `ValidatedFinding` records only — it never
  reads a checkout or includes patch/source bodies.
- Added `app/agents/code_review/profile.py`: bounded, deterministic
  project-profile extraction (purpose, languages, commands, conventions,
  high-risk paths, instruction-file names, citations) from a caller-supplied
  file inventory. Instruction-file (`AGENTS.md`, `SKILLS.md`, `CLAUDE.md`,
  `CONTRIBUTING.md`) *contents* are never copied into the profile — only
  their file names are recorded as provenance — and `render_skills` marks
  the rendered artifact as generated, non-executable context.
- Added `app/agents/code_review/ingestion.py`: the rate-limited, resumable,
  one-repository project-profile ingestion job. `GitHubRateLimiter` enforces
  a minimum interval between GitHub calls with an injectable clock/sleep.
  `find_profile_checkpoint` reads the persisted `RepositoryProfile` row for
  `(repository_id, commit_sha, profile_version)` as the explicit resume
  point; `ingest_repository_profile` returns that checkpoint without any
  GitHub call when it already exists. `archive_to_files` expands a GitHub
  tarball with bounded member count, per-member size, and total size,
  rejects symlinks/hardlinks and path traversal, and strips the tarball's
  top-level prefix before validating every remaining path.
- Added `app/agents/code_review/workflow.py`: `run_code_review`, the thin
  orchestrator that composes every module above into one durable
  Procrastinate job: load the queued commit, checkout the exact head SHA,
  assemble the packet twice (once before scanners to fail fast on a
  malformed diff, once with scanner runs so the model prompt sees them),
  run scanners, call the LLM gateway for a structured `FindingBatch`,
  validate and merge findings, persist them, render and store the report
  artifact, deliver a Discord summary, and record a terminal review status.
  Only safe metadata is returned — the dict returned to Procrastinate never
  carries patch text, model text, scanner output, or raw exception strings.
- Added `app/connectors/github.py`: a least-privilege GitHub App connector
  with no general-purpose request method. It verifies webhook HMAC
  signatures against the raw body before any JSON parsing, validates
  clone URLs are uncredentialed `github.com` HTTPS, exchanges the App JWT
  for a cached installation token, and exposes only
  `get_repository_metadata`, `compare_commits`, and
  `download_repository_archive` (bounded by `max_bytes`), each scoped to an
  explicit repository allowlist.
- Added `app/api/github.py`: the signed webhook endpoint. It reads a
  bounded request body, verifies the signature and delivery-ID pattern
  before decoding JSON, requires the configured installation ID, ignores
  non-`push` events and branch-deletion pushes, and calls
  `CodeReviewRepository.accept_push` to durably record the
  repository/SHA identity before enqueueing. A queue failure after
  successful acceptance returns `503` without discarding the durable row,
  which remains replayable.
- Extended `app/connectors/discord.py` with `ReviewSummary`,
  `DiscordReviewSummaryAdapter`, and `deliver_review_summary`. The outbound
  message carries only repository, SHA, risk, status, an allowlisted
  finding-count mapping (`block`/`important`/`suggestion`), and an artifact
  key — never finding titles, explanations, patch text, or scanner output.
  `deliver_review_summary` is idempotent, keyed by
  `review_idempotency_key(repository, head_sha)`; a delivery already `SENT`
  or `ACKNOWLEDGED` short-circuits without posting. The persisted delivery
  UUID is passed as Discord's `nonce` with `enforce_nonce: true`. Transient/
  transport failures record `UNCERTAIN` (Discord may have accepted the
  message); authorization or permanent failures record `FAILED` with a safe
  error code; the originating error is always re-raised to the caller.
- Extended `app/db/models.py` with `CodeRepository`, `ReviewedCommit`,
  `ReviewFinding`, and `RepositoryProfile`, and added Alembic revision
  `0004_phase3_code_review`. `ReviewFinding` carries a database
  `CheckConstraint("published_inline = false", name="phase3_inline_disabled")`
  so the inline-comment gate is closed at the schema level, not only in
  application code. `ReviewedCommit` is unique on `(repository_id,
  head_sha)` and on `delivery_id`; `ReviewFinding` is unique on
  `(reviewed_commit_id, fingerprint)`; `RepositoryProfile` is unique on
  `(repository_id, commit_sha, profile_version)`.
- Added `app/db/code_review.py` (`CodeReviewRepository`): transaction-
  composable persistence that only ever stores normalized metadata,
  validated findings, and artifact references — never diffs, scanner
  output, or model text. `accept_push` upserts the repository row, then
  creates or replays a `ReviewedCommit` keyed by
  `review_idempotency_key(repository, head_sha)` through
  `RunRepository.create_or_get`; a `ReviewedCommit` insert race is resolved
  by re-reading the row inside a nested transaction rather than by locking.
  `persist_findings` inserts findings once per `(reviewed_commit_id,
  fingerprint)`. `finish` is a no-op once the commit is already terminal.
- Added `app/queue/worker.py`: the worker-only import boundary. Importing
  this module registers the code-review handler
  (`run_code_review`) with `app.queue.tasks.register_task_handler`; `app.
  main` (the API process) continues to import `app.queue.app.
  procrastinate_app` and `app.queue.tasks` directly and never imports `app.
  queue.worker`, so the API process registers no task handlers.
  `infra/idle-worker.sh` starts every named worker with
  `python -m procrastinate --app app.queue.worker.procrastinate_app worker
  ...`, so only worker processes trigger this registration. This was
  verified by reading both files directly (see excerpts below), not
  inferred.
- Added `tests/unit/test_code_review_ingestion.py`,
  `test_code_review_packet.py`, `test_code_review_report.py`,
  `test_code_review_risk.py`, `test_code_review_scanners.py`,
  `test_code_review_workflow.py`, and `test_review_delivery.py`, and
  extended `test_code_review_findings.py` and
  `test_code_review_repository.py`.

### Original worker registration boundary (historical)

`app/queue/worker.py`:

```python
from app.agents.code_review.workflow import run_code_review
from app.queue.app import procrastinate_app
from app.queue.tasks import register_task_handler

register_task_handler("code_review", run_code_review)

__all__ = ["procrastinate_app"]
```

`infra/idle-worker.sh` invokes workers with:

```sh
exec python -m procrastinate \
  --app app.queue.worker.procrastinate_app \
  worker \
  --name "lifeagent-$worker_name" \
  --queues "$worker_name" \
  --concurrency "${WORKER_CONCURRENCY:-1}" \
  --wait \
  --listen-notify \
  --delete-jobs successful
```

`app/main.py` imports `app.queue.app.procrastinate_app` and
`app.queue.tasks` directly and never imports `app.queue.worker`. Only the
`--app app.queue.worker.procrastinate_app` entry point used by the three
named workers therefore causes `register_task_handler("code_review", ...)`
to run; the API process never registers a handler and would return
`no handler registered for code_review` if a code-review job were ever
executed inside it (it is not — the API only enqueues).

## Durable contracts Phase 4 must respect

- **Idempotency keys.** `review_idempotency_key(repository, head_sha)` is
  the single stable identity shared by `AgentRun.idempotency_key`,
  `ReviewedCommit(repository_id, head_sha)`, and the Discord delivery
  intent. A webhook replay, a queue redelivery, or a re-run for the same
  commit must resolve to the same row, not a new one.
- **Delivery-intent nonce.** `deliver_review_summary` opens a `Delivery`
  intent before any HTTP call and passes the persisted delivery UUID as
  Discord's `nonce` with `enforce_nonce: true`. Any future delivery
  channel added in Phase 4 must follow the same open-intent-before-send,
  UUID-as-nonce pattern rather than inventing a new de-duplication
  mechanism.
- **Pinned-SHA checkout.** `checkout_repository` always checks out
  `--detach <exact sha>` and verifies `rev-parse HEAD` matches before
  yielding the path; it never checks out a branch tip. Phase 4's nightly
  consolidation and quick-scan jobs must keep resolving an exact SHA before
  checkout rather than trusting a branch reference.
- **Artifact-key indirection for reports.** Reports and rendered profiles
  are written through `ArtifactStore.put` and referenced everywhere else
  (durable rows, Discord messages, workflow return values) only by their
  content-addressed artifact key — never inlined. Phase 4's harness-
  oriented report format must keep this indirection.
- **Safe-return-value rule for queue handlers.** `run_code_review`'s
  returned dict is logged by Procrastinate and is restricted to identifiers,
  enum-like status strings, counts, and diagnostic codes. It never carries
  patch text, model output, scanner output, or raw exception text. Any new
  handler Phase 4 adds must be held to the same rule.
- **`published_inline = false` schema gate.** The `phase3_inline_disabled`
  check constraint on `review_findings` is a durable, not just an
  application-level, block on inline GitHub comments. Enabling inline
  comments in a later phase requires an explicit migration to relax this
  constraint, not an application-code change alone.

## Verification status

Automated coverage observed in this repository at the time of writing:

- `uv run ruff check .`: passed, no errors.
- `uv run ruff format --check .`: passed, 96 files already formatted.
- `uv run pyright app`: **one error**, unrelated to the modules listed as
  owned by this document but present in `app/agents/code_review/
  workflow.py:224` — `_finding_counts` returns
  `dict[Literal['block', 'important', 'suggestion'], int]` where the
  declared return type is `dict[str, int]` (`reportReturnType`). This is
  not fixed here because `workflow.py` is not a file this document is
  authorized to change; it is reported below as a risk.
- `uv run pytest tests/unit -q`: **144 passed**.

Unit coverage that maps to the plan's Phase 3 acceptance tests:

- **Push replay dedup / exact-SHA checkout.**
  `tests/unit/test_github_webhook.py::
  test_signed_push_is_durable_and_repository_sha_replay_is_deduplicated`
  and `tests/unit/test_code_review_repository.py::
  test_checkout_is_exact_sha_and_cleaned` cover replay deduplication and
  exact-SHA (not branch-tip) checkout at the unit level.
- **Scanner/checkout timeout and clone failure produce usable
  attention/failed records without blocking other jobs.**
  `tests/unit/test_code_review_workflow.py::
  test_scanner_timeout_yields_attention_and_does_not_abort_run` and
  `test_clone_failure_yields_failed_with_checkout_diagnostic_and_no_delivery`
  cover this at the workflow level; `tests/unit/test_code_review_scanners.py`
  covers per-scanner isolation
  (`test_one_scanner_failing_never_affects_another`,
  `test_timeout_is_timed_out_with_scanner_timeout_code`,
  `test_missing_executable_is_unavailable`).
- **Discord delivery is idempotent; no GitHub comment is created.**
  `tests/unit/test_review_delivery.py::
  test_first_delivery_posts_once_and_records_sent` and
  `test_replay_with_same_repo_and_sha_does_not_repost` cover delivery
  idempotency. There is no inline-GitHub-comment code path anywhere in
  `app/connectors/github.py` to test against — the capability was not
  built, matching the plan's instruction to keep this phase report/
  Discord-only.
- **Vague/document-only speculation is suppressed; findings are
  attributable and de-duplicated.** Covered at the unit level by
  `tests/unit/test_code_review_findings.py` (vagueness, documentation-
  speculation, and evidence/line-attribution rejection paths, plus
  fingerprint-based and near-duplicate merging).

Not verified as part of this phase's automated work, and not claimed:

- **The fixture-repository acceptance test** (a real repository with a
  seeded regression, a leaked test secret, and a documentation-only commit,
  run end to end through checkout, scanners, and the model) does not exist
  as a runnable fixture in this repository at the time of writing. The unit
  tests above exercise the same logic through synthetic/injected inputs at
  each module boundary, not through one real repository fixture.
  `tests/acceptance/` does not exist in this working tree at the time of
  writing this document, so no acceptance-suite result can be reported
  here.
- **The plan's 20-representative-commit manual gate** for the inline
  GitHub-comment policy has not been run; no inline-comment capability
  exists to evaluate, and the gate is correspondingly still closed.
- **A live Discord send.** No test in this repository or Phase 2's exercises
  a real Discord bot token or a real external message; all delivery tests
  use an injected fake adapter/client.
- **A live GitHub webhook/App round trip** (real signature, real
  installation token exchange, real tarball download) has not been run;
  `GitHubAppConnector` is exercised only against injected/mocked HTTP
  clients in the unit suite.
- PostgreSQL integration tests for this phase's migration and repository
  layer were not run as part of this report; they require Docker and a
  disposable PostgreSQL container, which was out of scope for this
  documentation task.

## Decisions and risks

- **The inline GitHub-comment gate remains closed**, both in application
  code (no inline-comment method exists anywhere in
  `app/connectors/github.py` or `app/api/github.py`) and in the database
  schema (`review_findings.published_inline` carries a
  `CHECK (published_inline = false)` constraint). Enabling it requires a
  deliberate, explicit change in a later phase, evaluated against the
  plan's 20-commit manual gate.
- **Discord delivery has still never sent a real external message.**
  `DiscordReviewSummaryAdapter` and `deliver_review_summary` are fully
  implemented and unit-tested against an injected `httpx.AsyncClient` or a
  fake sender, exactly as Phase 2 left the failure-alert adapter. Real
  credentials and irreversible external delivery remain deferred.
- **Scanner availability is treated as a normal, expected condition, not an
  error.** `run_scanner` returns `unavailable` (not `failed`) when the
  executable is missing or a native command is not allowlisted, and one
  scanner's `timed_out`/`unavailable`/`failed` status degrades the review to
  `attention` rather than aborting it — `_checkout_and_prepare` in
  `workflow.py` runs every configured `ScannerSpec` regardless of whether
  the underlying tool is actually installed on the host, so the pipeline
  works whether or not Semgrep/Gitleaks/Trivy binaries are present.
- **Model-output-invalid drives `attention`, not `failed`.** When the LLM
  gateway does not return a schema-valid `FindingBatch`, `run_code_review`
  still persists any scanner-derived findings, writes a report, attempts
  delivery, and finishes with `review_status = "attention"` and
  `error_code = "analysis_invalid_output"` — a degraded analysis is
  surfaced for human attention rather than discarded as a failure.
- **A known type-checking issue exists in `workflow.py` that this document
  does not own and did not fix**: `pyright` reports one `reportReturnType`
  error in `_finding_counts` (`app/agents/code_review/workflow.py:224`),
  where the return type is a `dict` keyed by a `Literal` union rather than
  `str`. This should be corrected by whichever owner is authoritative for
  `workflow.py` before Phase 4 builds on top of it.
- **No end-to-end fixture-repository acceptance test exists yet** in this
  working tree. The module-level unit tests give strong evidence that each
  stage behaves correctly in isolation, but the plan's specific acceptance
  scenario (one fixture repository, one seeded regression, one leaked test
  secret, one documentation-only commit, evaluated together) has not been
  assembled or run.
- **Rate-limited ingestion depends on an injectable clock/sleep for
  determinism**; the real GitHub interval is only as reliable as
  `GitHubRateLimiter`'s default `asyncio.sleep`-based wiring, which was not
  exercised against a live GitHub API in this phase.

## Next phase

Phase 4 moves from one repository to account scale: GitHub repository
discovery and one-at-a-time initial ingestion resumable from persisted
state, nightly Toronto-time consolidation (6–7 p.m.) with an optional
catch-up policy, a webhook-triggered high-risk quick scan, review history
and dismissed-finding reasons, project-profile review/refresh, and a report
format aimed at a coding harness proposing a fix. Its acceptance tests
require that interrupting initial ingestion resumes at the next unprofiled
repository without duplicating profiles, that "today's pushes" is proven to
mean Toronto midnight-to-now including DST fixtures, and that a daily report
includes only actionable records and delivery receipts.
