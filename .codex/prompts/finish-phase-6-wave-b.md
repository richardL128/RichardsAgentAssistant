# Codex orchestration — Phase 6 finance, continued (post Wave A)

This supersedes the "Remaining work" section of `.codex/prompts/finish-phase-6.md`
for everything **after** Wave A. Read that file first for the original gate
context and the eight-source rationale; this file records what actually
landed, what is broken, one decision that needs to go back to Richard before
continuing, and the still-open Wave B/C scope. Verify everything below
yourself — it was checked directly against the tree just now, but re-confirm.

```bash
cd /Users/richardliu/Desktop/LifeAgent
codex
# paste the prompt below
```

Same roles as before: you are **Sol**, delegate bounded file-disjoint work to
**Luna** executors, never commit — write `.claude/commit-message.txt`, print
it, ask Richard.

---

## 1. What Wave A actually delivered (good — verified against the tree)

- `app/connectors/finance_sources/http.py` (new) — a generic
  `HttpFinanceSourceAdapter` + `AuthStyle`/`HttpAuthConfig`/`HttpRequestConfig`,
  per-source parser functions, excerpt gating + clamping from the approval
  record, and `_failure(...)`-everywhere error handling (timeout, HTTP status,
  transient, malformed JSON, parser failure) — no adapter raises, matching the
  no-fallback contract.
- `app/agents/finance/sources.py` (new) — `build_finance_adapter_registry`
  wiring approvals + settings secrets + parsers into the adapter map, plus
  `_CREDENTIALS` describing each source's auth style and request shape.
- `app/agents/finance/normalization.py` — reworked so a stale, future, or
  version-mismatched document is now **dropped with a captured diagnostic**
  (`NormalizationDiagnostic` / `NormalizationResult`, `include_diagnostics=`
  overload) instead of raising and killing the whole run. Only a truly
  unapproved `source_id` still raises.
- `app/agents/finance/contracts.py` / `app/db/finance.py` / `app/api/finance.py`
  — added `classification`, `license_allows_excerpt`, `excerpt_max_chars`,
  `excerpt_max_words` end to end (contract, ORM columns + check constraints,
  repository, read API response).
- `app/core/config.py` — `finance_source_allowlist_version` now defaults to
  `"finance-sources-2026.09"`; added `dvids_api_key`, `eia_api_key`,
  `alpha_vantage_api_key`, `benzinga_api_token`, `fmp_api_key`,
  `discord_finance_channel_id`; wired into secret redaction and
  `safe_diagnostics()`.
- `app/db/migrations/versions/0008_phase6_finance_allowlist.py` (new) — adds
  the three excerpt columns + two check constraints, and seeds eight
  `finance_approved_sources` rows, all `enabled=false`.
- Tests added/updated: `tests/unit/test_finance_http_adapters.py` (new, 481
  lines), plus updates to `test_finance_api.py`, `test_finance_repository.py`,
  `test_phase0_core.py`, and `tests/integration/test_phase2_persistence.py`.

This is real progress and the design (generic config-driven adapter + per-source
parser + `_CREDENTIALS` table) is a good shape — keep it. But it does not pass
its own checks yet. Fix Section 2 before anything else.

---

## 2. P0 — fix before doing anything else (small, coupled — do it yourself, no executors)

Verified just now with `.venv/bin/python -m {ruff check,ruff format --check,pyright,pytest}`
directly (no `uv` on PATH in this shell — use whichever the project normally
uses, e.g. `uv run pytest`; same interpreter either way).

1. **Runtime bug that breaks every parser.** `app/connectors/finance_sources/http.py:392`
   calls `_approval_classification(approval)`, which is never defined —
   `pyright` flags it `reportUndefinedVariable` and `pytest` proves it: all 8
   parametrized cases of
   `test_each_supported_source_parser_maps_one_payload`, both auth tests, and
   3 more in `test_finance_http_adapters.py` fail with `NameError`. Because
   `HttpFinanceSourceAdapter.fetch()` catches `(TypeError, ValueError,
   ValidationError)` around the parser call but `NameError` is none of those,
   it propagates out of `fetch()`, gets caught by `guarded_fetch()`'s bare
   `except Exception` in `app/connectors/finance_sources/adapters.py`, and
   silently becomes a generic `connector_transient` failure for every single
   source, every run. Add the missing helper, e.g.:
   ```python
   def _approval_classification(approval: SourceApproval) -> str:
       return approval.classification.value
   ```
2. **Unused imports** in `http.py`: `typing.Any` (line 12) and
   `SourceClassification` (line 21) — `ruff check` flags both `F401`.
3. **`RUF009`** in `app/agents/finance/sources.py:47` —
   `request: HttpRequestConfig = HttpRequestConfig()` as a dataclass default;
   use `field(default_factory=HttpRequestConfig)` (mirror how `http.py`
   already does `field(default=lambda: datetime.now(UTC), repr=False)` for
   its clock default).
4. **Formatting** — `ruff format --check .` reports 3 files need reformatting:
   `app/agents/finance/normalization.py`,
   `app/db/migrations/versions/0008_phase6_finance_allowlist.py`,
   `tests/integration/test_phase2_persistence.py`. Run `ruff format .`.
5. **Stale test, not updated for the new normalization behavior.**
   `tests/unit/test_finance_workflow.py::test_normalization_rejects_unlicensed_excerpt_and_stale_documents`
   still asserts `normalize_documents(...)` **raises** `ValueError` matching
   `"freshness"` for a 30-day-old document. That assertion is for the old
   behavior; `normalize_documents` now drops it silently (or, with
   `include_diagnostics=True`, returns it in `NormalizationResult.diagnostics`
   with `reason="stale_document"`). Update the test to assert the new
   contract — call with `include_diagnostics=True` and assert the diagnostic,
   not a raise. Keep a case that still asserts the raise path for a
   genuinely-unapproved `source_id` (that branch is unchanged).
6. **Migration 0008 disagrees with its own integration test.**
   `tests/integration/test_phase2_persistence.py::test_phase6_finance_allowlist_is_seeded_disabled_and_audited`
   expects the allowlist audit event to have
   `action == "record_finance_source_allowlist"` and
   `id == uuid.uuid5(uuid.NAMESPACE_URL, "lifeagent:finance-sources-2026.09:recorded")`
   (`2f233214-c789-52b2-8db4-7bd0ee341e2e`). The migration instead inserts
   `action="approve_finance_source_allowlist"` with a hardcoded
   `ALLOWLIST_AUDIT_ID = uuid.UUID("46c20d71-5ce5-43a3-a5b6-958462406904")` —
   these are two different UUIDs, so the test will fail as soon as it runs
   against a real Postgres (it didn't catch this yet because it needs Docker).
   Reconcile them — pick one source of truth and make the other match. Lean
   toward the test's naming (`record_`, not `approve_`): every row is seeded
   `enabled=false, approved_at=NULL, approval_audit_id=NULL`, i.e. nothing
   has actually been *approved* for live use yet, only the allowlist itself
   has been *recorded*. Using the deterministic `uuid5` form (rather than a
   hand-picked constant) also makes the migration idempotent-safe if it's
   ever regenerated. But this is your call to make coherently, not a forced
   answer — just make the migration and the test agree.

Re-run `ruff check . && ruff format --check . && pyright && pytest tests/unit -k finance`
until clean, then run the full suite per §6 before moving on.

---

## 3. Stop and ask Richard — the seed deviates from the approved list

`.codex/prompts/finish-phase-6.md` §1 recorded Richard's **exact eight**
approved sources as: `janes`, `breaking_defense`, `eia_open_data`,
`spglobal_commodity_insights`, `alpha_vantage_news`, `benzinga_news`,
`fmp_etf`, `etf_com_vettafi`.

Wave A instead built and seeded a **different** eight:
`dvids`, `breaking_defense`, `eia_open_data`, `federal_register_energy`,
`alpha_vantage_news`, `benzinga_news`, `fmp_etf`, `alpha_vantage_etf`.

Three substitutions happened without a recorded decision:

| Approved | Built instead | Likely reason |
|---|---|---|
| `janes` (paid, OAuth2, defense) | `dvids` (free, DoD public-affairs feed) | no path to a Janes contract/key |
| `spglobal_commodity_insights` (paid, editorial+price) | `federal_register_energy` (free, DOE regulatory filings) | no path to a Platts contract/key |
| `etf_com_vettafi` (paid, content licence) | `alpha_vantage_etf` (Alpha Vantage `ETF_PROFILE`, reusing an existing key) | no path to a VettaFi contract/key |

This is a reasonable engineering instinct — all three replacements are free
or already-keyed APIs, which is exactly what "gated until source/entitlement
approval" is supposed to reward — but `IMPLEMENTATION_PLAN.md` Phase 6's gate
is explicit that Richard approves the **exact eight** sources and their
licence terms before the code proceeds, and swapping 3 of 8 without asking is
the thing that rule exists to prevent. `alpha_vantage_etf` also needs its own
scrutiny: the seeded `license_note` in migration 0008 says *"Use for
Richard's private individual investment analysis and monitoring only; no
third-party access or redistribution"* — narrower than Alpha Vantage's
standard commercial ToS language used for `alpha_vantage_news` in the same
migration, worth Richard's eyes specifically.

**Do not silently keep or revert this substitution.** Present it to Richard
as an explicit choice:

- **(a)** Ratify the built set (`dvids`, `federal_register_energy`,
  `alpha_vantage_etf`) as the new approved eight, superseding the original
  list — free/low-friction, ready to enable as soon as keys are registered;
- **(b)** Go back to the original set and hold `janes` /
  `spglobal_commodity_insights` / `etf_com_vettafi` disabled until those
  contracts exist, i.e. Phase 6 stays fully gated for defense/ETF coverage
  until then;
- **(c)** A mixed set Richard specifies.

Whichever he picks, the allowlist version, the seed migration, and
`docs/implementation/phase-6.md` (§5 below) must say the same eight sources
and be internally consistent. Update the license/entitlement seed text if the
answer is (c).

---

## 4. Wave B — worker + delivery + API wiring (unchanged scope, still not started)

Confirmed via grep: `app/queue/worker.py` and `app/main.py` have zero
references to `finance` — this wave has not started.

**You (Sol):**
- `app/agents/finance/workflow.py` — add `run_finance(run_id, idempotency_key)
  -> dict[str, object]` matching `TaskHandler`
  (`app/queue/tasks.py:TaskHandler`), plus a `_load_default_runtime` /
  `configure_finance_runtime` pair mirroring
  `app/agents/academic_planner/workflow.py:353` (`_load_default_runtime`):
  build `SQLAlchemyFinanceStore(engine, allowlist_version=settings.finance_source_allowlist_version)`,
  a shared `httpx.AsyncClient`, `build_finance_adapter_registry(settings,
  approvals, client=client)` from `app/agents/finance/sources.py`, optional
  model gateway (see the open decision in the original prompt — deterministic
  cards are fine to keep for now), optional Discord delivery (built in this
  wave, see below). Keep `run_finance_briefing` as the pure inner function
  Wave A already covers with tests.
- `app/queue/worker.py` — `register_task_handler("finance", run_finance)`.
- `app/main.py` — `app.state.finance_store = SQLAlchemyFinanceStore(...)` and
  `app.include_router(finance_router)` (import from `app.api.finance`).
- `compose.yaml` — the `&app-environment` anchor (line 28) is an explicit
  allowlist, not a passthrough: it still has none of
  `FINANCE_SOURCE_ALLOWLIST_VERSION`, `DVIDS_API_KEY`, `EIA_API_KEY`,
  `ALPHA_VANTAGE_API_KEY`, `BENZINGA_API_TOKEN`, `FMP_API_KEY`, or
  `DISCORD_FINANCE_CHANNEL_ID`. Add all of them in the same
  `${VAR:-default}` style as the neighboring Discord/Notion entries — every
  service that shares the anchor (`api`, `worker-finance`, and whichever
  others reference `*app-environment`) picks them up automatically once
  added there once.

**Executor B1 — `app/connectors/discord.py` finance delivery + test**
- A `DiscordFinanceBriefingAdapter` / `DiscordFinanceBriefingDelivery` pair
  following the academic pattern at `app/connectors/discord.py:458`
  (`DiscordAcademicPlannerAdapter` / `DiscordAcademicPlannerDelivery`),
  consuming `render_discord_briefing(payload)` from
  `app/agents/finance/delivery.py`, an allowlisted channel
  (`discord_finance_channel_id`), returning `DiscordDeliveryReceipt`,
  idempotency key `f"finance:{date.isoformat()}:market-open:v1"` (matches
  what `run_finance_briefing` already builds). Owns its new classes +
  `tests/unit/test_finance_delivery_discord.py` only — do not touch the rest
  of `discord.py`.

---

## 5. Wave C — health, docs, acceptance (seed migration already exists; fix don't duplicate)

The original prompt's "Executor C1 — seed migration" is done (module 0008
exists) modulo the §2.6 fix above — don't re-create it.

**Executor C2 — health + `docs/implementation/phase-6.md`**
- `app/health/checks.py` — currently has zero finance references. Add a
  finance source-health check reading `finance_source_health` / the
  allowlist and reporting healthy/attention/failed without calling vendor
  APIs directly; have the workflow (Wave B) call
  `FinanceRepository.record_source_health(...)` from each adapter result
  (`SourceFetchResult.failure` present ⇒ attention/failed; absent ⇒ healthy).
- `docs/implementation/phase-6.md`, in the exact format of `phase-5.md`:
  contract & acceptance criteria, "Implemented" bullets, the **actual**
  eight-source table (whatever Richard confirms in §3 — do not write this
  until that's settled, it would need rewriting), the gate status
  (`enabled=false`, awaiting keys — list which keys are still needed for
  which source), and what's deferred (LLM event-card enrichment, since the
  deterministic card builder is what ships).

**Executor C3 — `tests/acceptance/test_phase6_finance.py` (new)**
Cover the `IMPLEMENTATION_PLAN.md` Phase 6 acceptance list end-to-end through
`run_finance_briefing` (or `run_finance` once Wave B exists):
1. a run issues exactly eight source calls; a failing source is reported and
   **no** search/fallback request occurs;
2. every generated number on a fixture card has unit/date/source, every
   derived number has formula + source list;
3. cards separate verified facts / uncertainty / counter-case / impact label;
   a trade directive fails schema or content validation;
4. no raw licensed article body appears in the persisted payload or the
   Discord summary beyond approved excerpts/links;
5. gate closed (fewer than eight enabled) ⇒ `approval_required`, zero adapter
   calls, no delivery.
Also add one case using the real `HttpFinanceSourceAdapter` (mocked
transport) for at least one source per §2's fixed parsers, so the acceptance
suite would have caught the §2.1 `NameError` itself.

---

## 6. Validation (repo level, before each "wave done")

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
```

Integration tests need Docker (`tests/integration/test_phase2_persistence.py`
now includes the migration-0008 check from §2.6 — this is the test that will
actually prove the audit-event fix); run them if the stack is up, otherwise
say so explicitly rather than skipping silently. Report honestly — an unmet
acceptance criterion or a skipped check gets stated with its output, not
smoothed over. After §2 is clean and §3 is answered: read executor diffs
yourself, run the full validation above, write
`.claude/commit-message.txt`, print it, and ask Richard to commit before
starting Wave C.
