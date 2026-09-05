# Phase 6 — Finance briefing

## Contract and acceptance criteria

Phase 6 adds the gated finance briefing workflow: a versioned eight-source
allowlist, entitlement and licence metadata, exactly eight source calls with no
fallback search, normalization and freshness validation, exposure mapping
against holdings/watchlist/ETF look-through records, deterministic event-card
generation, thesis journal updates, source-health persistence, and an
evidence-linked Discord briefing.

The plan's acceptance tests require that:

1. a run issues exactly eight source calls, reports a failing source, and makes
   no search or fallback request;
2. every generated number in a fixture event card has unit/date/source and every
   derived number has a formula and source list;
3. cards distinguish verified facts, uncertainty, counter-case, and permitted
   impact labels, while prohibited trade directives fail schema/content
   validation; and
4. no raw licensed article appears in the persisted UI payload or Discord
   summary beyond approved excerpts and links.

The implementation remains behind the Phase 6 gate: the approved source
allowlist is recorded, but no live schedule should run until each source is
individually enabled with an approval timestamp and approval audit event.

## Implemented

- Added `app/agents/finance/contracts.py`: strict Pydantic contracts for source
  approval, source health, source queries, source documents, failures,
  normalized events, quantitative values, holdings, watchlist items, ETF
  exposure, event cards, thesis journal entries, and briefing payloads. The
  contracts enforce timezone-aware timestamps, source excerpt permission,
  numeric source metadata, derived-number formulas, permitted impact labels, and
  rejection of trade-directive language.
- Added `app/connectors/finance_sources/adapters.py`: exactly-eight source
  orchestration. The approval gate requires eight unique records for the active
  allowlist version and requires every record to be enabled, approved, and tied
  to an approval audit event. Source fetches are guarded so adapter exceptions
  become source failures rather than fallback calls.
- Added `app/connectors/finance_sources/http.py`: a generic configuration-driven
  HTTP source adapter, auth configuration, request shaping, per-source parsers,
  excerpt clamping from the approval record, freshness-window filtering, parser
  failure capture, timeout/status/malformed-JSON diagnostics, and no fallback
  retrieval path.
- Added `app/agents/finance/sources.py`: the active source registry for
  `finance-sources-2026.09`, credential mapping, request configuration, and
  parser binding for the ratified public-detail source set.
- Added `app/agents/finance/normalization.py`: source-version validation,
  14-day freshness validation, future-document rejection, evidence IDs,
  cross-source event deduplication, and UI-safe source metadata projection.
  Stale, future, and version-mismatched documents are dropped with
  `NormalizationDiagnostic` records when requested; only a truly unapproved
  source ID still raises.
- Added `app/agents/finance/exposure.py`: holdings/watchlist/ETF look-through
  matching and derived exposure notes/quantitative values.
- Added `app/agents/finance/delivery.py`: Discord briefing rendering with a
  raw-marker guard so persisted/source-only content is not sent as a raw
  licensed article body.
- Added `app/agents/finance/workflow.py`: `run_finance_briefing(...)` as the
  pure inner workflow, deterministic event-card fallback, source-health
  recording from adapter results, thesis journal persistence, optional Discord
  delivery, and `run_finance(run_id, idempotency_key)` with default runtime
  loading for the worker process.
- Added `app/db/finance.py` and migrations `0007_phase6_finance.py` /
  `0008_phase6_finance_allowlist.py`: finance allowlist, per-source health,
  holdings, watchlist, ETF exposure, investment theses, thesis events, and
  finance briefing payload storage. The allowlist migration also adds excerpt
  permission/limit columns and seeds the audited `finance-sources-2026.09`
  source records.
- Added `app/api/finance.py`: read-only finance source and run-filter metadata
  endpoints. `app/main.py` mounts the finance router and initializes
  `app.state.finance_store`.
- Wired the finance worker path through `app/queue/worker.py`, the finance
  schedule through `app/queue/tasks.py`, and the `worker-finance` Compose service.
  While the source approval gate is closed, the periodic path is expected to
  return `approval_required` or no-op rather than calling vendors.
- Added Discord finance delivery in `app/connectors/discord.py`: rendered
  single-message delivery to an allowlisted finance channel with a deterministic
  idempotency key of `finance:<date>:market-open:v1` and durable delivery-intent
  recording.
- Added finance configuration in `app/core/config.py`, including
  `finance_source_allowlist_version = "finance-sources-2026.09"`, source API
  credentials for the ratified set, `discord_finance_channel_id`, safe
  diagnostics, and redaction.

## Approved source allowlist

The active allowlist version is `finance-sources-2026.09`. Richard originally
approved a set containing Janes, S&P Global Commodity Insights, and ETF.com /
VettaFi. That paid-source set was explicitly superseded for this phase by the
ratified public-detail set below, because these sources expose enough public API
detail to implement and test the disabled gate without inventing private
contract endpoints.

All eight records are seeded with `enabled=false`, `approved_at=NULL`, and
`approval_audit_id=NULL`. The allowlist itself is recorded by the deterministic
audit event `lifeagent:finance-sources-2026.09:recorded`; individual sources are
not live-approved by this seed.

| Source | Classification | Version | Entitlement | Excerpt boundary |
| --- | --- | --- | --- | --- |
| `dvids` | primary | `dvids-search-v1` | Free DVIDS developer API key assigned to the registered API client | Excerpts allowed, clamped to 300 characters, with DVIDS attribution and no implied DoD endorsement. |
| `breaking_defense` | reported | `wp-rest-v2` | Public feed (Breaking Media); no key | Excerpts allowed, clamped to 200 characters, with attribution and canonical link only. |
| `eia_open_data` | primary | `eia-api-v2` | Free registered API key | Excerpts allowed without a configured clamp; attribution requested as "Source: U.S. Energy Information Administration". |
| `federal_register_energy` | primary | `federal-register-api-v1` | Public FederalRegister.gov API; no API key required | Excerpts allowed, clamped to 500 characters, with FederalRegister.gov / Office of the Federal Register attribution and canonical official links. |
| `alpha_vantage_news` | reported | `news-sentiment-v1` | Alpha Vantage premium API plan | Excerpts allowed, clamped to 500 characters, using publisher summaries with attribution and no bulk redistribution or resale. |
| `benzinga_news` | reported | `benzinga-news-v2` | Benzinga licensed newswire feed; contracted display seats | Excerpts allowed, clamped to 500 characters, for entitled users with Benzinga attribution and no public archive or resale. |
| `fmp_etf` | secondary | `fmp-api-v3` | FMP paid plan (Starter+ for ETF endpoints); commercial licence, internal use | Excerpts not allowed; numeric holdings data may be cited as a derived-number source only. |
| `alpha_vantage_etf` | secondary | `etf-profile-v1` | Alpha Vantage premium API plan; private individual use | Excerpts not allowed; numeric ETF profile and holdings data may be used only for Richard's private individual investment analysis and monitoring. |

The exact seeded licence notes and entitlement strings live in
`app/db/migrations/versions/0008_phase6_finance_allowlist.py`; this section
summarizes the operational boundary rather than replacing the migration as the
audited source of truth.

## Acceptance evidence

- `tests/unit/test_finance_sources.py` covers the hard source gate, exact-eight
  query construction, incomplete registry rejection, and no-fallback behavior
  when one adapter fails.
- `tests/unit/test_finance_http_adapters.py` covers each supported source parser,
  auth header/query-param shape, excerpt gating and clamping, timeout/status
  failure capture, malformed JSON, and freshness filtering.
- `tests/unit/test_finance_workflow.py` covers the approval-required closed gate,
  exactly eight source calls when open, source failure capture, deterministic
  card generation, thesis journal updates, delivery behavior, unlicensed excerpt
  validation, normalization diagnostics for stale documents, and the still-hard
  rejection of unapproved source IDs.
- `tests/unit/test_finance_repository.py` covers durable allowlist/source-health
  records, portfolio snapshot loading, briefing payload persistence, and run
  filter metadata.
- `tests/unit/test_finance_api.py` and `tests/unit/test_finance_main.py` cover
  the read-only source API and application wiring.
- `tests/unit/test_finance_delivery_discord.py` covers allowlisted Discord
  finance delivery, idempotency-key validation, durable delivery records,
  transient failures, authorization failures, and one-message clamping.
- `tests/integration/test_phase2_persistence.py` includes the migration check
  proving the `finance-sources-2026.09` allowlist is seeded disabled and audited
  with the recorded allowlist event.

There is not yet a standalone `tests/acceptance/test_phase6_finance.py` file in
the current tree. The Phase 6 acceptance contract is covered by the focused unit
and migration tests listed above, but a single end-to-end acceptance file remains
the clearest future consolidation point.

## Deferred external verification

- Live source fetches require credentials and individual approval records for
  `dvids`, `eia_open_data`, `alpha_vantage_news`, `benzinga_news`, `fmp_etf`,
  and `alpha_vantage_etf`; `breaking_defense` and `federal_register_energy`
  need no API key but still remain disabled until Richard flips their approval
  flags. The Phase 6 gate intentionally stays closed until all eight records are
  enabled and audited.
- Live Discord delivery requires `discord_bot_token` and
  `discord_finance_channel_id`. Automated tests use mocked transports and
  durable delivery records.
- Live finance schedules must remain no-op/approval-required until the complete
  allowlist gate is satisfied. The code is present, but the seed migration does
  not enable production vendor access.
- LLM event-card enrichment through `FinanceModelGateway.event_cards` is
  deferred. The shipped path uses deterministic cards and keeps the finance
  contracts' trade-directive validators authoritative.
- The implementation does not use or invent private Janes, S&P Global Commodity
  Insights, or ETF.com / VettaFi contract endpoints in this allowlist version.
  Those paid sources would require a future allowlist version and explicit
  entitlement review before being enabled.

## Next phase

Phase 7 exposes the operational state through the read-only FastAPI/Jinja/HTMX
console, including finance source visibility, finance run activity, and the
health/status records generated by this phase.
