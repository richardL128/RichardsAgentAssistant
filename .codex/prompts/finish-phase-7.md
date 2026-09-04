# Codex orchestration — LifeAgent Phase 7 (read-only operations console)

Read `CLAUDE.md`, `AGENTS.md`, `IMPLEMENTATION_PLAN.md` §Phase 7, and
`frontend/AGENTS.md` first — the last one is the actual product/IA/security
spec (routes, screens, health rules, data contract) and takes precedence over
anything below where they conflict. `ARCHITECTURE MAIN.md` §4 just points at
`frontend/AGENTS.md`, it has no additional detail.

```bash
cd /Users/richardliu/Desktop/LifeAgent
codex
# paste the prompt below
```

Same roles as the Phase 6 prompts (`.codex/prompts/finish-phase-6*.md`): you
are **Sol**, delegate bounded file-disjoint work to **Luna** executors, never
commit — write `.claude/commit-message.txt`, print it, ask Richard.

---

## 0. First, check where Phase 6 actually landed

As of this prompt, `git log` shows `093fe2b Add gated finance source adapters
and allowlist` committed, and the working tree has further **uncommitted**
changes wiring `run_finance` into `app/queue/worker.py`, `app.state.finance_store`
+ the finance router into `app/main.py`, a Discord finance-briefing delivery
adapter in `app/connectors/discord.py`, and a `compose.yaml` update — i.e.
Phase 6 Wave B (worker/API/delivery wiring) looks substantially done but
**not yet verified or committed**. Confirm it, run full validation
(`ruff check`, `ruff format --check`, `pyright`, `pytest`), and get it
committed before or in parallel with Phase 7 — Phase 7's finance health card
needs finance runs to actually exist as `AgentRun`/`Delivery` rows, and its
`/settings/sources` page needs the finance allowlist decision from
`.codex/prompts/finish-phase-6-wave-b.md` §3 to be settled. Don't block
*starting* Phase 7 on this, but don't call Phase 7 done while it's still open.

---

## 1. What already exists for Phase 7 (verify, don't rebuild)

This is further along than `IMPLEMENTATION_PLAN.md`'s phase ordering implies —
most of `frontend/AGENTS.md`'s "Implementation order" step 1 (shared schemas)
and a good chunk of step 2 (health evaluator) were apparently built during
Phase 2 groundwork rather than deferred to Phase 7. Read all of this before
planning any executor task, so nothing gets rebuilt.

- **`app/db/models.py`** already has every table `frontend/AGENTS.md` §"Data
  contract" asks for: `AgentRun`, `RunStep`, `Delivery`, `EvidenceRef`,
  `HealthCheck`, `AuditEvent`, `UIAcknowledgement`, plus `ApprovalRequest`.
  Schema work is done.
- **`app/operations/contracts.py`** (156 lines) — `HealthCard`, `ActivityItem`,
  `ActivityPage`, `ActivityDetail`, `TimelineStep`, `DeliveryReceipt`,
  `EvidenceLink`, `ExternalLink`, `AcknowledgementCreate/Result`,
  `ApprovedSource`, `SourceSettings` (the last one already has a validator
  refusing `schedule_enabled=True` without `approval_complete=True` — the
  Phase 6 gate, modeled again at the UI layer).
- **`app/operations/repository.py`** (358 lines) — `OperationsRepository`
  with `health_cards()`, `activity(... filters, pagination)`, `detail(run_id)`,
  and `acknowledge(...)` (the one write path, strictly `UIAcknowledgement`,
  matching `frontend/AGENTS.md`'s "must not trigger an agent, modify
  Discord/GitHub/Notion, or suppress future alerts"). It already redacts text
  via `app.core.redaction.redact_text`, validates external links are
  `https://` with no embedded credentials before rendering them, and matches
  `/activity` filters (`agent`, `date_from/to`, `attention_only`, plus
  `repository`/`ticker_theme`/`course` via an `AuditEvent.target_type` search)
  from the spec almost verbatim.
- **`app/health/evaluator.py`** + **`app/health/service.py`** — a pure,
  deterministic `evaluate_operational_health(OperationalFacts) ->
  OperationalHealth` rule engine plus `evaluate_and_persist(session, facts)`
  which upserts it into `HealthCheck` via `HealthRepository.upsert`. This is
  exactly the "health evaluator" `frontend/AGENTS.md` step 2 asks for.
- **Tailwind is already wired into the Docker build.**
  `infra/Dockerfile` has a `node:22.14.0-alpine3.21` stage that runs
  `tailwindcss@3.4.17` against `frontend/tailwind.config.js` +
  `frontend/src/input.css`, content-scanning `app/templates/**/*.html` +
  `app/**/*.py`, and copies the compiled, minified `app/static/app.css` into
  the runtime image. **There are no templates yet for it to scan**, but the
  pipeline itself needs no new infra work.
- **`jinja2` is already a `pyproject.toml` dependency** — nothing to add there.

## 2. What's actually missing (verified against the tree just now)

- **`app/operations/repository.py` has no `source_settings()` method** —
  `SourceSettings`/`ApprovedSource` are modeled in contracts but nothing
  builds them from `FinanceRepository.list_source_records()`
  (`app/db/finance.py`). `/settings/sources` has nothing to read from yet.
- **No HTTP surface for any of this.** `app/api/` has `academic.py`,
  `finance.py`, `github.py`, `health.py` — no `operations.py` (or whatever you
  name it) exposing `OperationsRepository` over REST/HTMX endpoints, and no
  `POST` for `AcknowledgementCreate`.
- **`evaluate_and_persist` is never called anywhere.** Grepped the whole
  tree — zero callers outside `app/health/service.py` itself. No workflow
  (`code_review`, `academic_planner`, `finance`) and no periodic task feeds it
  `OperationalFacts`, so `HealthCheck` rows are never written in a real run.
  `OperationsRepository.health_cards()` will render every card as "attention
  — No deterministic health record has been evaluated yet" (its own
  documented fallback in `_card()`) until this is wired up. This is the
  single biggest gap — the whole health page is a facade without it.
- **Finance has its own, separate health table** — `finance_source_health` in
  `app/db/finance.py` (per-source: did *this vendor's API call* succeed),
  distinct from the shared `health_checks` table `OperationsRepository`
  reads (per-agent: did *this run* process and deliver). Both are legitimate
  and answer different questions; `frontend/AGENTS.md`'s finance card wants
  *both* ("last successful run... number of trusted sources checked") — don't
  collapse one into the other, feed the agent-level `HealthCheck` from the
  finance workflow's overall run outcome and let `/activity` detail surface
  the per-source `finance_source_health` rows as evidence/diagnostics.
- **No `app/templates/` directory at all.** Nothing server-renders yet.
- **`app/static/` doesn't exist** (only created by the Docker build stage) —
  fine for the container, but local `uv run` dev/test won't have `app.css`
  unless something else generates or fakes it for tests.
- **No HTMX asset anywhere.** Vendor a pinned copy into `frontend/src/` (built
  into `app/static/` alongside `app.css`) or load it from a CDN in the base
  template — pick one and be consistent; a personal ops console that should
  work without an internet-connected browser probably wants it vendored.
- **No auth of any kind.** `frontend/AGENTS.md` says "Require the user's
  existing authentication and least-privilege, read-only UI credentials" but
  there is no session/basic-auth/cookie mechanism anywhere in `app/` today,
  and `OperationsRepository.activity/detail/acknowledge` already take a
  `user_id` parameter with nothing upstream supplying one. This is a decision
  for Richard (§3), not something to invent silently — LifeAgent is a
  single-user personal system, so this is likely "simple," but "likely
  simple" is still Richard's call given it gates write access to
  acknowledgements and read access to redacted run detail.
- **No browser-test tooling.** `pyproject.toml` has no Playwright/Selenium
  dependency; existing tests use `fastapi.testclient.TestClient` only.
  `IMPLEMENTATION_PLAN.md`'s Phase 7 acceptance criteria explicitly say
  "Browser tests show a card..." and "keyboard navigation and narrow viewport
  smoke tests pass" — see §3.
- **No `app/operations` tests at all.** `tests/unit/test_code_review_operations.py`
  tests a *different* `operations.py` (inside `app/agents/code_review/`) —
  unrelated namesake, not this module.

---

## 3. Decisions for Richard — ask, don't assume

1. **Auth for the console.** Options: (a) a single shared HTTP Basic Auth
   credential from a new `Settings` field (`ops_console_username` /
   `ops_console_password` as `SecretValue`), enforced by FastAPI dependency
   on every `app/api/operations.py` route and template render; (b) a signed
   session cookie set by a simple login form; (c) no auth, rely entirely on
   network placement (e.g. only reachable over Tailscale/VPN/localhost) —
   `compose.yaml` doesn't currently publish the API port outside the host
   network, worth checking before assuming this is safe. Given "least-
   privilege, read-only UI credentials" is explicit in the spec, (c) alone
   is probably not sufficient, but confirm rather than build (a) unasked.
2. **Browser/acceptance testing.** `IMPLEMENTATION_PLAN.md` wants real
   browser tests (keyboard nav, narrow viewport, HTML-escaping proof). Add
   Playwright (`uv add --group dev pytest-playwright`, new browser binaries
   in CI/Docker) to satisfy that literally, or interpret it as
   `TestClient`-rendered HTML assertions (parse with `bs4`/regex for ARIA
   roles, tab order via `tabindex`, and a `<meta name="viewport">` tag) and
   document that substitution in `docs/implementation/phase-7.md`. This is a
   scope/dependency decision, not a style choice — ask.
3. **HTMX sourcing** — vendor a pinned `htmx.min.js` into `frontend/src/`
   (built alongside `app.css`, works offline) vs. a `<script src=https://...>`
   CDN tag in the base template (simpler, needs network). Recommend vendoring
   given this runs on a home Compose stack, but it's Richard's call.
4. **`/settings/sources` data source** — build `OperationsRepository.source_settings()`
   reading `FinanceRepository.list_source_records()` directly (keeps the
   console's read path independent of `/finance/sources`), or have the UI
   route simply proxy/render `GET /finance/sources` (already built in Phase
   6, already returns `SourceRecordResponse` with `license_allows_excerpt`
   etc. from `app/api/finance.py`). The `ApprovedSource`/`SourceSettings`
   contracts already in `app/operations/contracts.py` suggest the former was
   intended, but confirm before duplicating logic that already exists and is
   tested (`tests/unit/test_finance_api.py`).

---

## 4. Waves

### Wave A — health evaluator wiring + auth + source projection (Sol, tightly coupled)

Do this yourself: it touches the shared workflow entry points across all
three agents plus a new cross-cutting auth dependency, not independent
file-scoped work.

- Call `evaluate_and_persist` (or extend it) from each of
  `run_code_review`/`run_code_review_daily` (`app/agents/code_review/workflow.py`,
  `operations.py`), `run_academic_planner`
  (`app/agents/academic_planner/workflow.py`), and `run_finance`
  (`app/agents/finance/workflow.py`) after each run completes — derive
  `OperationalFacts` from the run's actual `RunStatus`/`DeliveryStatus`/retry
  state, `check_name` matching `_AGENT_CHECK_NAMES` in
  `app/operations/repository.py` (`"code_review"`, `"academic_planner"`,
  `"finance"` or their listed aliases). Also add a `shared_services` fact
  source (DB/queue/connector-token checks — reuse `app/health/checks.py`'s
  existing `check_database`/`check_ollama`/`check_artifact_root` as the input
  facts) on a periodic tick, since `_card("shared_services", ...)` currently
  has nothing feeding it either.
- Land whichever auth mechanism Richard picked in §3.1 as a FastAPI
  dependency, applied to the new operations router and template routes.
- Add `OperationsRepository.source_settings(session) -> SourceSettings` (or
  wire the proxy approach — whichever §3.4 lands on).

### Wave B — API + templates + base layout (executors, file-disjoint)

**Executor B1 — `app/api/operations.py` (new) + `tests/unit/test_operations_api.py`**
- Read-only routes for `GET /` data, `GET /activity` (filters + pagination
  passthrough to `OperationsRepository.activity`), `GET /activity/{run_id}`,
  `GET /settings/sources`, plus `POST /activity/{run_id}/ack` (or under
  whatever path scheme you pick) calling `OperationsRepository.acknowledge`.
  Follow the `app/api/finance.py` pattern: a `Protocol` for the store,
  `request.app.state.operations_store` (or reuse `request.app.state.database`
  directly — your call), Pydantic response models, 503 when unavailable.
  Own this file + its test only.

**Executor B2 — `app/templates/base.html` + layout partials + `app/main.py` wiring**
- `Jinja2Templates` + `StaticFiles` mount in `app/main.py` (coordinate with Sol
  on exact insertion point since `main.py` is shared — small diff, get it
  reviewed before other executors build on it). Base template: nav, the
  three-state health-badge component (healthy/attention/failed, label +
  icon, never colour-only per spec), Toronto-timezone timestamp helper (a
  Jinja filter, not client-side JS, so it's testable), skip-to-content link
  and visible focus states for keyboard nav.

### Wave C — the four pages (executors, file-disjoint, depend on B1+B2 landing)

**Executor C1 — `app/templates/health.html` (the `/` page) + route**
- One compact card per `HealthCard` (finance, code review, academic planner,
  shared services): state, exact last-success timestamp (Toronto time, not
  "recently"), next expected run, diagnostic, deep link to filtered
  `/activity`. Global warning banner when any card is `failed`.

**Executor C2 — `app/templates/activity.html` + `activity_detail.html` + routes**
- `/activity`: searchable timeline per `frontend/AGENTS.md` §"Unified
  activity and decision log" — timestamp/agent/run/status, one-line summary +
  severity, delivery + evidence deep links, unresolved-item flag, "mark
  acknowledged locally" control (HTMX POST, swaps in place, no page reload),
  filters for agent/date range/attention-only/repository/ticker-theme/course.
  `/activity/:runId`: summary, timeline, evidence, delivery receipts,
  warnings, and the **redacted** raw record — never render raw HTML/Markdown
  from agent output as HTML (escape everything; this is the "never execute
  content received from outputs, logs, titles, or source extracts" rule).

**Executor C3 — `app/templates/settings_sources.html` + route + `docs/implementation/phase-7.md`**
- Read-only allowlist view (`SourceSettings`/`ApprovedSource` or the
  finance-API proxy per §3.4) — version, per-source enabled/entitlement,
  explicit "approval happens outside this UI" note. No enable/disable
  control here — this page is visibility only, per spec.
- `docs/implementation/phase-7.md` in the format of `phase-5.md`/`phase-6.md`:
  contract & acceptance criteria, "Implemented" bullets, the auth/browser-test/
  HTMX decisions from §3 and what was chosen, what's deferred.

**Executor C4 — `tests/acceptance/test_phase7_operations_console.py` (new)**
Cover `IMPLEMENTATION_PLAN.md` Phase 7's acceptance list:
1. a card renders for each agent/service with state, exact last success,
   next expected run, diagnostic, and a working filtered activity link;
2. activity filters work by agent/date/attention/repository/ticker-theme/
   course and never execute output HTML/Markdown (assert a `<script>` or
   raw-HTML payload in a fixture run's summary renders as inert escaped
   text, not markup);
3. the acknowledgement endpoint only ever writes `ui_acknowledgements` —
   assert it makes no Discord/GitHub/Notion call and enqueues no job;
4. secrets/private source bodies are absent from every operations API
   response and every rendered page (grep rendered HTML/JSON for API keys,
   Discord tokens, full licensed article text);
5. keyboard navigation and narrow-viewport behavior per whatever §3.2 landed
   on (Playwright, or the `TestClient` HTML-assertion substitute).

---

## 5. Validation (repo level, before each "wave done")

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
docker compose up -d --build && curl -f http://localhost:8000/health/ready
```

Confirm the Tailwind build stage actually picks up the new templates (rebuild
the image, don't just trust `docker compose up` layer caching) and that
`app/static/app.css` isn't checked into git (it's a build artifact — verify
`.dockerignore`/`.gitignore` already exclude `app/static/`, add if not).
Integration tests need Docker; run them if the stack is up, otherwise say so
explicitly. Report honestly — an unmet acceptance criterion or a skipped
check gets stated with its output. After each wave: read executor diffs
yourself, run the full validation above, write `.claude/commit-message.txt`,
print it, and ask Richard to commit before starting the next wave.
