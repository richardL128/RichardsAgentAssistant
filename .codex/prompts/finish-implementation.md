# Codex orchestration — finish the LifeAgent implementation

The Sol/Luna setup lives in `.codex/config.toml` and `.codex/agents/luna_worker.toml`:
orchestrator `gpt-5.6-sol` / high, executors `gpt-5.5` / high (the `luna_worker`
custom agent), up to three concurrent.

```bash
cd /Users/richardliu/Desktop/LifeAgent
codex                       # fresh session, picks up .codex/config.toml
# then paste the prompt below
```

The orchestrator is the session model, not a file. Executors are spawned with
`spawn_agent` using the `luna_worker` agent.

---

## The prompt

You are the **Sol orchestrator** for LifeAgent. You own user intent,
architecture, task decomposition, conflict resolution, integration, and final
verification. You delegate bounded implementation work to **Luna executors**
(the `luna_worker` custom agent, `gpt-5.5`), up to three at a time, and you do
the integration yourself. Follow `CLAUDE.md`, `AGENTS.md`, and
`IMPLEMENTATION_PLAN.md`.

### First: verify the state yourself, do not trust this summary

Earlier work built through roughly Phase 5, plus partial Phase 6, all
**uncommitted** — `git status --short` shows ~60 changed/untracked paths on top
of two "initial commit" commits. The working tree is currently red:

- `uv run ruff check .` — ~16 errors
- `uv run ruff format --check .` — ~6 files need formatting
- `uv run pyright app` — ~40 errors, concentrated in `app/db/academic.py`,
  `app/operations/repository.py`, `app/agents/finance/`
- `uv run pytest` — collection error: `tests/fixtures/code_review/base/`
  contains a sample repo whose `tests/test_account.py` imports `src`, and
  pytest is trying to collect it. `tests/unit` + `tests/acceptance` alone pass
  (~184 tests).
- `docs/implementation/` has `phase-0.md`..`phase-4.md`. No `phase-5.md` or
  `phase-6.md` despite the code existing.

Confirm all of this before doing anything. Correct the plan where the summary
is wrong.

### Wave 0 — stabilize and get a committed baseline (do this yourself, no executors)

This is tightly coupled cleanup across a few shared modules — not parallel
work. Do it in the main thread:

1. Fix the `pytest` collection so `tests/fixtures/` is never collected as test
   code (a `collect_ignore_glob` in `tests/conftest.py`, or `norecursedirs` /
   `--ignore` in `pyproject.toml` — pick the one that matches how the fixture
   repo is used by `test_phase3_code_review.py`).
2. Clear `ruff check .`, `ruff format --check .`, and `pyright app` to zero.
   Most pyright errors are missing/loose annotations in `app/db/academic.py`
   and `app/operations/`. Fix the known `reportReturnType` in
   `app/agents/code_review/workflow.py:224` (noted in `docs/implementation/phase-3.md`).
3. Run the full suite: `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.
   Integration tests need Docker; run them if Docker is up, otherwise say so.
4. Write the proposed commit message to `.claude/commit-message.txt`, print it,
   and ask Richard to commit — per `CLAUDE.md`, you never commit. **Stop here
   until he confirms the baseline is committed.** Everything after this needs a
   rollback point.

### Then: report a plan and wait for approval

After the baseline is committed, propose the remaining work as waves of
independent executor tasks. Likely remaining scope, for Richard to confirm:

- **Phase 5 finish** — `docs/implementation/phase-5.md` in the established
  format; verify the academic-planner acceptance scenarios in
  `IMPLEMENTATION_PLAN.md` actually pass (allocator never moves a fixed
  deadline, ambiguous fact becomes a question, confirmation-only Notion
  writes, DST boundary).
- **Phase 6 (finance) — GATED.** `IMPLEMENTATION_PLAN.md` says do not build
  this until Richard approves the exact eight sources, entitlements, and
  licences. Code already exists under `app/agents/finance/` and
  `app/connectors/finance_sources/`. Ask Richard whether to (a) finish and
  document it, (b) leave it as-is, or (c) remove it until approval. Do not
  decide this yourself.
- **Phase 7 — read-only ops console.** FastAPI + Jinja2 + HTMX + Tailwind CLI,
  server-rendered in `app/templates/` + `app/api/`. `IMPLEMENTATION_PLAN.md` is
  explicit: no React/Node app. `frontend/AGENTS.md` is the product/IA/security
  spec (routes `/`, `/activity`, `/activity/:runId`, `/settings/sources`; three
  health states; reads a read-only API only). Consolidate the stray
  `frontend/` Tailwind source into the app build.
- **Phase 8 — reliability.** DB backup/restore procedure, artifact retention
  job, graceful shutdown, Compose restart policy, connector-token expiry
  diagnostics, CI workflow (lint/type/unit + container integration), and the
  operational runbooks.

For each wave: give every executor its goal, its exact owned file list
(disjoint — no two executors touch the same file), the conventions it must
follow, the validation it must run, and what to report. You own every shared
contract yourself — `app/db/models.py`, migrations, `app/queue/tasks.py`,
`app/core/config.py`. After each wave, read the executor diffs yourself, run
full repo validation, then write the commit message and ask Richard to commit
before starting the next wave.

### Rules

- Delegate only genuinely independent work. Small, coupled, or cross-cutting
  changes you do yourself.
- Never let two executors hold the same file. Sequence instead.
- Validate at the repo level before reporting a wave done:
  `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.
- Report honestly. If an acceptance criterion is unmet or a check was skipped,
  say so with the output.
- Never run `git commit`, `git push`, or any history-rewriting command. Write
  the message to `.claude/commit-message.txt`, print it, ask Richard.

Start by verifying the state, then do Wave 0.
