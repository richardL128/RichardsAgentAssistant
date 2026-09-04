# Phase 3 orchestration kickoff prompt

Launch the orchestrator on Opus, then paste the prompt below.

```bash
scripts/claude-sandbox.sh --model opus      # sandboxed
# or, on the host:
claude --model opus
```

The orchestrator is the session model — it is not a file. The executors are
`.claude/agents/Sonnet-executor.md`, which defines the `sonnet-executor`
subagent (`model: claude-sonnet-5`), spawned four at a time by the
orchestrator.

---

## The prompt

You are the **Opus orchestrator** for LifeAgent. You own user intent,
architecture, task decomposition, conflict resolution, and final verification.
You delegate bounded implementation work to **Sonnet executors** (the
`sonnet-executor` subagent, Sonnet 5), running **four at a time**, and you do
the integration yourself.

A Codex agent built this repository through Phase 2 and stopped partway into
Phase 3. Pick up where it left off.

### First: verify the handoff state yourself

The summary below is my reading of the repository — treat it as a starting map,
not as truth. Confirm it before you delegate anything, and correct the plan if
it is wrong.

**Complete and documented:** Phases 0–2, with acceptance notes in
`docs/implementation/phase-0.md`, `phase-1.md`, and `phase-2.md`. Migrations run
to `0003_phase2_integration`. Read `phase-2.md` first — it states the durable
contracts everything in Phase 3 must respect.

**Phase 3, built but not wired together.** These modules exist and look
substantially complete:

- `app/agents/code_review/` — `contracts.py`, `repository.py` (pinned
  checkout), `packet.py`, `risk.py`, `scanners.py`, `findings.py`,
  `report.py`, `profile.py`
- `app/connectors/github.py`, `app/api/github.py` (signed push webhook)
- `app/db/code_review.py` and migration `0004_phase3_code_review`
- Unit tests: `test_code_review_findings.py`, `test_code_review_repository.py`,
  `test_github.py`, `test_github_webhook.py`

**The gap.** Nothing composes those parts into a run. `register_task_handler`
in `app/queue/tasks.py` is defined and exported but never called, so
`code_review` work still returns `disabled_no_handler` and the periodic
deferrer enqueues nothing. Concretely, still missing against the Phase 3 build
list in `IMPLEMENTATION_PLAN.md`:

1. The workflow spine that runs checkout → packet → risk → scanners → model
   proposal → validation/dedup → report artifact → delivery, plus handler
   registration.
2. Idempotent Discord **review summary** delivery. `app/connectors/discord.py`
   currently has only `DiscordFailureAlertAdapter`, which rejects
   normal-status messages by design.
3. The rate-limited one-repository project-profile ingestion job.
4. Unit coverage for `packet`, `risk`, `report`, `scanners`, `profile` — all
   currently untested.
5. The Phase 3 acceptance suite: fixture repository with a seeded regression, a
   leaked test secret, and a documentation-only commit; push replay
   deduplicated by repository/SHA; exact-SHA checkout rather than branch tip;
   scanner timeout and clone failure producing attention/failed records with
   usable diagnostics.
6. `docs/implementation/phase-3.md`, in the established format of the earlier
   phase docs.

**One architectural decision is yours, not an executor's.** Workers start via
`python -m procrastinate --app app.queue.app.procrastinate_app`
(`infra/idle-worker.sh`), so only that module is imported. But
`register_task_handler` is documented as "during worker startup, not module
import". Decide how the code-review handler gets registered in a real worker
process — a dedicated worker entrypoint module, a lazy registration hook in
`app/queue/app.py`, or something better — and settle it before wave 1 starts,
because the spine executor builds against it.

### How to run the executors

Spawn four `sonnet-executor` agents **in a single message** so they run
concurrently. Give each one: its goal, its exact owned file list, the
conventions it must follow, the validation it must run, and what to report.
Ownership must be disjoint — no two executors may edit the same file. You own
every shared contract (`app/db/models.py`, migrations, `app/queue/tasks.py`,
`app/core/config.py`) and make those edits yourself.

**Wave 1 — four in parallel:**

| Executor | Goal | Owns |
| --- | --- | --- |
| `spine` | Workflow composing the existing Phase 3 modules end to end, plus handler registration per your decision above | new `app/agents/code_review/workflow.py`, new worker entrypoint if you chose one, `infra/idle-worker.sh` |
| `delivery` | Idempotent code-review summary delivery reusing the Phase 2 delivery-intent/receipt tables and persisted delivery UUID as nonce | `app/connectors/discord.py`, new `tests/unit/test_review_delivery.py` |
| `ingestion` | Rate-limited, resumable single-repository project-profile ingestion job | new `app/agents/code_review/ingestion.py`, new `tests/unit/test_code_review_ingestion.py` |
| `unit-gaps` | Unit tests for the five untested Phase 3 modules; report defects rather than fixing them | `tests/unit/test_code_review_packet.py`, `_risk.py`, `_report.py`, `_scanners.py`, `_profile.py` — read-only everywhere else |

Wait for all four. Read their diffs yourself rather than trusting their
reports, resolve conflicts, make the shared-contract edits, and run full
repository validation before wave 2.

**Wave 2** builds the acceptance suite on top of wave 1: split the fixture
repository and findings-quality tests from the failure-path and replay-dedup
tests, add `docs/implementation/phase-3.md`, and use a spare executor for an
adversarial review of the wave 1 diff.

### Rules

- Delegate only genuinely independent work. Do small, tightly coupled, or
  cross-cutting changes yourself in the main thread.
- Never let two executors hold the same file. Sequence them instead.
- Validate at the repository level before reporting completion:
  `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.
  Integration tests need Docker.
- Report honestly. If an acceptance criterion is unmet or a test was not run,
  say so with the output — do not smooth it over.
- Follow `CLAUDE.md`: at the end of the goal, write the commit message to
  `.claude/commit-message.txt`, print it, and ask me to commit. Never run
  `git commit` yourself.

Start by verifying the state above and reporting your plan — the registration
decision, the four wave 1 assignments with exact file ownership, and anything
in my map that turned out to be wrong. Wait for my approval before spawning.
