# Codex orchestration — finish LifeAgent Phase 6 (finance briefing)

Read `CLAUDE.md`, `AGENTS.md`, and `IMPLEMENTATION_PLAN.md` first. This prompt
assumes the Phase 5 baseline is committed and the tree is green
(`ruff check`, `ruff format --check`, `pyright`, `pytest` on
`tests/unit` + `tests/acceptance`). Verify that yourself before starting.

```bash
cd /Users/richardliu/Desktop/LifeAgent
codex                       # fresh session, picks up .codex/config.toml
# then paste the prompt below
```

You are the **Sol orchestrator**. Delegate bounded, file-disjoint work to
**Luna executors** (`luna_worker`, up to three at once); you own the shared
contracts (`app/core/config.py`, `app/queue/tasks.py`, migrations,
`app/main.py`, `app/queue/worker.py`) and all integration. Never run
`git commit` / `git push` / history rewrites — write the message to
`.claude/commit-message.txt`, print it, and ask Richard, per `CLAUDE.md`.

---

## 1. The gate is now satisfied

`IMPLEMENTATION_PLAN.md` §"Phase 6 — Gate": *"record the approved sources,
licences/subscriptions, and compliance boundaries before enabling the
schedule."*

Richard has approved the **exact eight** sources below on **2026-09-04**.

- Allowlist version: **`finance-sources-2026.09`** — set this as the default
  for `finance_source_allowlist_version` in `app/core/config.py` and seed it
  as the versioned/audited allowlist record.
- All eight ship **`enabled = false`** until each vendor contract/API key is
  actually in hand. `FinanceRepository.source_approval_gate()` stays closed
  (it needs `enabled`, `approved_at`, and `approval_audit_id` on all eight),
  so `run_finance_briefing` returns `approval_required` and the periodic
  schedule is a no-op until Richard flips the flags. That is intended.
- The `license_note` / `entitlement` strings below are Richard's approved
  compliance text. Do not paraphrase them in the seed — store verbatim
  (trim to the column limits in `FinanceApprovedSource`: `license_note` ≤1000,
  `entitlement` ≤255).

### The eight approved source records

Coverage mix: 2 military/defense, 2 energy, 2 tech/markets news, 2 ETF.

| # | `source_id` | `name` | `base_url` (API method) | `source_version` | `classification` | `entitlement` | `license_note` | excerpt allowed? |
|---|---|---|---|---|---|---|---|---|
| 1 | `janes` | Janes Defence Intelligence API | `https://api.janes.com/` — REST GET, OAuth2 bearer | `janes-api-v1` | `primary` | Janes commercial subscription + API add-on; named-user seats | Proprietary. Internal analytical use only; no external redistribution or republication of Janes content or derived intelligence. Deep-link to source. | **No** — headline + link only |
| 2 | `breaking_defense` | Breaking Defense | `https://breakingdefense.com/wp-json/wp/v2/posts` — REST GET, no auth (also `/feed/` RSS) | `wp-rest-v2` | `reported` | Public feed (Breaking Media); no key | All rights reserved. Headline + short snippet + attribution + canonical link only (fair use); no full-text republication. | **Yes** — ≤200 chars + attribution + link |
| 3 | `eia_open_data` | U.S. EIA Open Data API v2 | `https://api.eia.gov/v2/` — REST GET, `api_key` query param | `eia-api-v2` | `primary` | Free registered API key | U.S. Government work, public domain, no copyright. Attribution requested: "Source: U.S. Energy Information Administration". | **Yes** — unrestricted |
| 4 | `spglobal_commodity_insights` | S&P Global Commodity Insights (Platts) API | `https://api.platts.com/` — REST GET, API key + OAuth | `commodity-insights-2024` | `primary` | Paid Platts / Commodity Insights data licence; per-dataset entitlements; no onward distribution | Proprietary. Platts price assessments/symbols must NOT be redistributed or displayed externally. Editorial news excerpts ≤100 words with attribution, internal briefings only. | **Yes** — editorial only, ≤100 words, no assessment/price data |
| 5 | `alpha_vantage_news` | Alpha Vantage News & Sentiment API | `https://www.alphavantage.co/query?function=NEWS_SENTIMENT` — REST GET, `apikey` query param | `news-sentiment-v1` | `reported` | Alpha Vantage premium API plan | API ToS. Third-party publisher headlines/summaries for internal app display to entitled users; no bulk redistribution or resale. Attribute to originating publisher. | **Yes** — provider `summary` field only + publisher attribution + link |
| 6 | `benzinga_news` | Benzinga News API | `https://api.benzinga.com/api/v2/news` — REST GET, `token` query param | `benzinga-news-v2` | `reported` | Benzinga licensed newswire feed; contracted display seats | Commercial redistribution licence. Display headlines/body to entitled end users per contract; no public archive or resale; Benzinga attribution required. | **Yes** — headline + teaser/body snippet for entitled users |
| 7 | `fmp_etf` | Financial Modeling Prep ETF Holdings API | `https://financialmodelingprep.com/api/v3/etf-holder/` — REST GET, `apikey` query param (also `/api/v4/etf-holdings`) | `fmp-api-v3` | `secondary` | FMP paid plan (Starter+ for ETF endpoints); commercial licence, internal use | Commercial data licence. Derived/aggregated figures may be shown to entitled users; raw dataset redistribution or resale prohibited. | **No** — numeric holdings data; cite as derived-number source, not prose |
| 8 | `etf_com_vettafi` | VettaFi / ETF.com Content API | `https://api.etf.com/` — REST GET, API key | `vettafi-content-2024` | `reported` | VettaFi content-licensing agreement; per-title entitlement | Proprietary. Article excerpts ≤150 words with author + "Courtesy of ETF.com / VettaFi" attribution and canonical link; no full reproduction. | **Yes** — ≤150 words + attribution |

**"excerpt allowed?"** maps to `SourceDocument.license_allows_excerpt`
(`app/agents/finance/contracts.py:140`). The `excerpt_obeys_license` validator
hard-fails if an adapter emits an `excerpt` while that flag is false, so each
adapter must set the flag from the record and clamp excerpt length to the
per-source limit above.

Caveat for Codex: these `base_url`/method/version values are Richard's best
public-docs reconstruction, not signed contract facts. Where a vendor's real
API path or current version differs, correct the seed and note it in
`docs/implementation/phase-6.md` — do not silently invent endpoints in adapter
code.

---

## 2. State of Phase 6 in the tree (verify, don't trust)

**Already built and tested:**

- `app/agents/finance/contracts.py` — full strict Pydantic contract set
  (source approval, documents, normalized events, exposure, event cards,
  thesis journal, briefing payload). Trade-directive and citation validators
  in place.
- `app/connectors/finance_sources/adapters.py` — `check_source_gate`,
  `build_source_queries`, `fetch_exactly_eight_sources` (exactly-eight
  enforcement, per-source failure capture, **no fallback**). Only the
  `FinanceSourceAdapter` **Protocol** exists — there are **no concrete
  adapters**.
- `app/agents/finance/normalization.py` — version check, 14-day freshness,
  evidence/event IDs, cross-source dedupe. Note: currently **raises** on a
  stale or wrong-version document.
- `app/agents/finance/exposure.py` — holdings/watchlist/ETF look-through
  mapping with derived `QuantValue`s.
- `app/agents/finance/delivery.py` — `render_discord_briefing`, raw-marker
  guard.
- `app/agents/finance/workflow.py` — `run_finance_briefing(...)` with the hard
  approval gate and deterministic-card fallback. **Signature does not match
  `TaskHandler`** and there is **no worker entry point / runtime injection**
  (contrast `run_academic_planner` in `app/agents/academic_planner/workflow.py:312`
  and its `_load_default_runtime`).
- `app/db/finance.py` — ORM (`FinanceApprovedSource`, health, holdings,
  watchlist, ETF exposure, theses, thesis events, briefings),
  `FinanceRepository`, `SQLAlchemyFinanceStore`, `source_approval_gate`.
- `app/db/migrations/versions/0007_phase6_finance.py` — all tables. **No seed
  data.**
- `app/api/finance.py` — read-only `/finance/sources` and
  `/finance/runs/filters`. **Not wired into `app/main.py`** (no
  `include_router`, no `app.state.finance_store`).
- `app/queue/tasks.py` — `finance_task` + `finance_periodic` (weekday
  market-open, `finance_market_open_schedule` default 09:00 Toronto) exist.
  **No handler is registered** — `app/queue/worker.py` never calls
  `register_task_handler("finance", ...)`, so `finance_periodic` short-circuits
  as `disabled_no_handler`.
- `compose.yaml` — `worker-finance` service already present.
- Tests: `tests/unit/test_finance_{contracts-ish,sources,workflow,repository,api}.py`
  (~4 files). No adapter tests, no worker-entry test, no acceptance file.
- `docs/implementation/phase-6.md` — **missing**.

---

## 3. Remaining work

Propose these as waves and get Richard's approval before delegating. Suggested
ownership is disjoint; adjust as you see fit but keep executors off each
other's files.

### Wave A — source adapters + config (the core gap)

**Executor A1 — `app/connectors/finance_sources/http.py` (new), adapter tests**
- One generic, config-driven `HttpFinanceSourceAdapter` implementing the
  `FinanceSourceAdapter` Protocol: injected `httpx.AsyncClient`, per-source
  auth style (query-param key / bearer / OAuth token / none), request build
  from `SourceQuery` (window, tickers, themes), response→`SourceDocument`
  mapping via a per-source parser, `license_allows_excerpt` + excerpt-length
  clamp taken from the approval record, `connector_timeout_seconds` timeout,
  and **every** failure path returning `SourceFetchResult(failure=...)` (never
  raising, never a fallback fetch).
- Per-source parser functions for all eight (pure `dict -> tuple[SourceDocument, ...]`),
  each dropping documents outside the freshness window / wrong version so
  normalization never rejects a whole run. Coordinate with A2 on whether the
  drop happens here or in `normalization.py`.
- `tests/unit/test_finance_http_adapters.py` — mocked `httpx` transport per
  source: auth header/param shape, excerpt gating, timeout→failure,
  malformed-JSON→failure, freshness filter.

**Executor A2 — normalization resilience + `app/agents/finance/sources.py` (new)**
- Change `normalize_documents` to **skip** a stale/wrong-version document with
  a captured diagnostic instead of raising (keep the raise only for a truly
  unapproved `source_id`). Update `tests/unit/test_finance_workflow.py`
  expectations. Owns `normalization.py` + its test only.
- `build_finance_adapter_registry(settings, approvals, *, client)` — maps the
  eight `source_id`s to configured `HttpFinanceSourceAdapter` instances; raises
  a clear error if an approved `source_id` has no parser or no key when
  `enabled`.

**You (Sol) — `app/core/config.py`**
- `finance_source_allowlist_version: str = "finance-sources-2026.09"`.
- Eight `SecretValue` keys: `janes_api_token`, `spglobal_commodity_insights_key`,
  `alpha_vantage_api_key`, `benzinga_api_token`, `fmp_api_key`,
  `etf_com_api_key` (EIA key too: `eia_api_key`; `breaking_defense` needs
  none). Add them to the `empty_secret_is_none` validator and to every
  redaction/`safe_config` list.
- `discord_finance_channel_id: str | None = None`.

### Wave B — worker + delivery + API wiring (mostly you)

**You (Sol):**
- `app/agents/finance/workflow.py` — add `run_finance(run_id, idempotency_key)
  -> dict` matching `TaskHandler`, plus a `_load_default_runtime` /
  `configure_finance_runtime` pair mirroring the academic planner: build
  `SQLAlchemyFinanceStore`, the adapter registry (shared `httpx.AsyncClient`),
  optional model gateway, optional Discord delivery from
  `discord_finance_channel_id` + `discord_bot_token`. Keep `run_finance_briefing`
  as the pure inner function.
- `app/queue/worker.py` — `register_task_handler("finance", run_finance)`.
- `app/main.py` — `app.state.finance_store = SQLAlchemyFinanceStore(engine,
  allowlist_version=settings.finance_source_allowlist_version)` and
  `app.include_router(finance_router)`.
- `compose.yaml` — pass the new finance env vars to `worker-finance` (and
  `api` for the read store).

**Executor B1 — `app/connectors/discord.py` finance delivery + test**
- A `DiscordFinanceBriefingAdapter` / `DiscordFinanceBriefingDelivery` pair
  following the academic pattern (`app/connectors/discord.py:458`), consuming
  `render_discord_briefing` output, allowlisted channel, `DiscordDeliveryReceipt`,
  idempotency key `finance:<date>:market-open:v1`. Owns its new classes +
  `tests/unit/test_finance_delivery_discord.py`.

### Wave C — seed migration, health, docs, acceptance

**Executor C1 — `app/db/migrations/versions/0008_phase6_finance_allowlist.py` (new)**
- Data migration: insert the allowlist-version audit record + the eight
  `finance_approved_sources` rows from §1, `enabled=false`,
  `approved_at=NULL`, `approval_audit_id=NULL`, classification per table.
  Idempotent, with a real `downgrade`. No schema change.
- `scripts/seed_finance_sources.py` (optional convenience wrapper over
  `FinanceRepository.upsert_approved_source`).

**Executor C2 — health + `docs/implementation/phase-6.md`**
- `app/health/checks.py` — a finance source-health check that reads
  `finance_source_health` / the allowlist and reports healthy/attention/failed
  without hitting vendor APIs; feed `FinanceRepository.record_source_health`
  from the adapter results in the workflow.
- `docs/implementation/phase-6.md` in the exact format of `phase-5.md`:
  contract & acceptance criteria, "Implemented" bullets, the approved-source
  table, the gate status (enabled=false, awaiting keys), and what is
  deferred (LLM event-card enrichment if you keep deterministic cards).

**Executor C3 — `tests/acceptance/test_phase6_finance.py` (new)**
Cover the `IMPLEMENTATION_PLAN.md` Phase 6 acceptance list:
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

---

## 4. Open decisions for Richard (ask, don't assume)

- **LLM event cards:** ship with the deterministic card builder and defer
  `FinanceModelGateway.event_cards` to a later pass, or implement an
  advisory-only Qwen enrichment now (still subject to `_reject_trade_directive`)?
- **Seed now vs. later:** land the disabled seed migration in this phase, or
  keep the eight records as a doc-only appendix until keys exist?
- **`spglobal_commodity_insights` / `janes` / `etf_com_vettafi`** are paid and
  may not get keys soon — still seed them disabled, or ship 5 now and add
  these 3 in a `finance-sources-2026.10` allowlist bump?

---

## 5. Validation (repo level, before each "wave done")

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
```

Integration tests need Docker; run them if the stack is up, otherwise say so.
Report honestly — unmet acceptance criterion or skipped check gets stated with
its output. After each wave: read executor diffs, run the above, write
`.claude/commit-message.txt`, print it, ask Richard to commit. Do not start the
next wave until he confirms.
