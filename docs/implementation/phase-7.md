# Phase 7 - Read-only operations console

## Contract and acceptance criteria

Phase 7 adds the read-only operations console for LifeAgent. FastAPI serves the
API and Jinja-rendered pages for system health, cross-agent activity, run
detail, and finance source settings. HTMX is used only for small progressive
enhancement interactions, such as acknowledging an activity item locally. The
console is not an agent, not a chat surface, and not a place to trigger
Discord, GitHub, Notion, finance-source, brokerage, or model actions.

The plan's acceptance tests require that:

1. browser tests show a card for each agent/service with state, exact last
   success, next expected run, diagnostic, and a filtered activity link;
2. activity filters work by agent, date range, attention-only state,
   repository, ticker/theme, and course, and never execute output HTML or
   Markdown;
3. acknowledgement changes only `ui_acknowledgements`; it cannot enqueue work
   or contact an external service;
4. security tests prove secrets, private source bodies, and full licensed
   article text are absent from API and HTML responses; and
5. keyboard navigation and narrow viewport smoke tests pass.

## Implemented

- Added fail-closed single-user HTTP Basic authentication for the console. The
  operations API and server-rendered routes require configured
  `ops_console_username` and `ops_console_password` credentials before any
  console data is returned.
- Added the authenticated `/api/operations` surface for health cards, activity
  pagination and filtering, run detail, read-only source settings, and a
  UI-only acknowledgement endpoint.
- Added server-rendered Jinja routes for `/`, `/activity`, `/activity/{runId}`,
  and `/settings/sources`. The UI uses exact Toronto-time timestamps,
  accessible healthy/attention/failed labels, keyboard-visible navigation, and
  mobile-friendly markup.
- Added deterministic operational health wiring. Agent and shared-service
  health cards are derived from persisted run, delivery, retry, and connector
  facts rather than model self-assessment.
- Added the source settings projection directly from the finance repository.
  The `/settings/sources` page displays the allowlist version, gate state,
  schedule state, source slots, names, hostnames, entitlements, and enabled
  flags without exposing credentials, vendor bodies, licence bodies, or write
  controls.
- Added redaction and escaping boundaries for operations responses and pages.
  Agent output, logs, titles, diagnostics, and source extracts are treated as
  untrusted display data. The base page config disables HTMX script evaluation.
- Added vendored HTMX 2.0.10 under the frontend build inputs for offline use.
  The copy is used as a static asset with its BSD-0 licence boundary noted in
  the source tree.
- Kept the frontend Python-first: there is no React application, no separate
  Node app, and no client-side data store. Node is used only in the existing
  Docker Tailwind build stage.

## Decisions

- Authentication uses a single shared HTTP Basic credential from settings. It
  fails closed when the username or password is not configured.
- Browser acceptance is literal Playwright coverage rather than TestClient-only
  HTML assertions. Sol integrates and validates the Playwright acceptance suite
  for this wave.
- HTMX is vendored as version 2.0.10 for offline operation instead of loaded
  from a CDN.
- The source settings page reads a direct `FinanceRepository` projection
  through `OperationsRepository.source_settings(...)` instead of proxying the
  finance API route.

## Acceptance evidence

- Unit coverage exercises operations authentication, operations API schemas and
  filters, source settings projection, base-template security configuration,
  health badge rendering, and the read-only source settings template.
- Template tests assert that malicious source fields are escaped, all eight
  allowlist slots render, no enable/disable controls exist, and no arbitrary
  vendor links are emitted from source records.
- Literal Playwright acceptance drives a live Uvicorn server through Chromium.
  It covers the health, activity, run-detail, acknowledgement, and source
  settings routes; verifies Tab/Enter focus behavior; and checks both activity
  and source settings for horizontal overflow at a 375 by 667 pixel viewport.

## Deferred external verification

- None within Phase 7. The browser acceptance suite passed in a Linux
  Playwright container; the macOS Codex sandbox cannot launch Chromium because
  it denies Chromium's Mach rendezvous registration.
- Live production access still depends on Richard configuring the HTTP Basic
  console credentials in the deployment environment.

## Next phase

Phase 8 completes reliability hardening: encrypted database backup and restore,
artifact retention, graceful shutdown proof, Compose/healthcheck audit,
connector-token diagnostics, CI, and operational runbooks.
