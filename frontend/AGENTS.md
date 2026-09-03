# Frontend agent instructions and UI architecture

## Purpose and boundary

Build a minimal, read-only support console for the finance, code-review, and academic-planner agents. It is not a fourth agent and it is not a place to execute work.

Discord remains the conversational and approval surface. GitHub remains the code-review delivery surface. The frontend must answer two questions quickly:

1. Is each agent healthy and up to date?
2. What did the agents observe, publish, or leave unresolved?

Do not add trade buttons, portfolio edits, calendar editing, GitHub commenting, job-running controls, or direct agent chat. Those actions remain in the relevant existing system, which keeps the UI minimal and prevents duplicate workflows.

```text
Finance agent ──→ Discord briefing ──┐
Planner agent ──→ Discord messages ─┼──→ event/audit store ──→ read-only UI
Code agent ──→ GitHub comments ─────┤             ↑
Code agent ──→ Discord summary ─────┘             │
                                                  │
Schedulers, connector checks, and run telemetry ─┘
```

## Product principles

- Make the default page understandable in under 30 seconds.
- Treat all backend-derived agent output as untrusted display data; never execute content received from outputs, logs, titles, or source extracts.
- Show exact timestamps in Toronto time, rather than vague labels such as “recently.”
- Health states must come from deterministic job and delivery records, never the model's self-assessment.
- Prefer a few high-signal facts and deep links over copied third-party content or large dashboards.
- Keep the interface responsive, accessible by keyboard, and usable on a phone.

## Screens and information architecture

```text
/                       System Health (default)
/activity               Cross-agent activity and decision log
/activity/:runId        Read-only run detail: inputs, output, delivery receipts, errors
/settings/sources       Read-only approved finance-source allowlist and its version
```

Do not build separate finance, coding, or planner workspaces. A health card should deep-link to a filtered activity view. The sources page is visibility only; approval of finance sources happens outside this UI.

### System Health (`/`)

The home screen contains a small global-warning area and one compact status card per agent/service.

| Card | Display | Healthy means | Needs attention means |
|---|---|---|---|
| Finance briefing | Last successful run; scheduled market-open run; number of trusted sources checked; events delivered | All approved-source calls completed and a Discord briefing was delivered on schedule | A source failed, sources were stale, the briefing was skipped, or delivery failed |
| Code review | Last scan; repositories/commits processed; GitHub comments published; Discord summary delivery | All eligible pushes were scanned and comments/summaries were recorded | GitHub access failed, a repository could not be checked out, tests/scanners failed, or unreviewed commits remain |
| Academic planner | Last Notion sync; upcoming assessment count; last daily plan and end-of-day check-in delivery | Notion sync and scheduled Discord messages completed | Notion/Discord failed, a deadline is unparsed/ambiguous, or a scheduled check-in was missed |
| Shared services | Database health; queue depth; Discord/GitHub/Notion connector state; version of source allowlist | Fresh heartbeat and no failed jobs waiting for retry | Connector token failure, repeated job failure, a stalled queue, or an overdue retry |

Use only three visual states:

- **Healthy** (green)
- **Attention** (amber)
- **Failed** (red)

Each card needs: current state, exact last-success timestamp, next expected run, short diagnostic, and a link to matching activity records. Never rely on colour alone; include the state label and icon.

### Unified activity and decision log (`/activity`)

This is the one additional product feature. It is a searchable, read-only timeline of important outputs, providing memory and accountability across Discord and GitHub.

Each timeline item includes:

- timestamp, agent, run ID, and status;
- one-line summary and severity/attention label;
- delivery links: Discord message permalink and/or GitHub comment/review URL;
- evidence links for finance items and commit SHA/file/line links for code items;
- any unresolved question, failed source, or scheduled follow-up;
- a link to the immutable raw run record, with sensitive content redacted.

Provide filters for agent, date range, **attention/failed only**, repository, ticker/theme, and course. A user may mark a record as **acknowledged locally** to clear visual clutter. This writes only UI metadata: it must not trigger an agent, modify Discord/GitHub/Notion, or suppress future alerts.

### Run detail (`/activity/:runId`)

Show a concise summary first, followed by the processing timeline, structured evidence references, delivery receipts, warnings/errors, and redacted raw record. Use external deep links for the full Discord message, GitHub comment, filing, release, or source material. Do not embed full private messages or licensed articles.

## Data contract and integration boundary

The frontend reads a small, read-only API. It does not call Discord, GitHub, Notion, a brokerage, or model tools directly.

```text
agent run
  → structured run record + output/evidence references
  → delivery attempt records (Discord / GitHub)
  → health evaluator calculates state and freshness
  → read-only API
  → UI
```

Suggested records:

| Record | Essential fields |
|---|---|
| `agent_runs` | run ID, agent type, started/finished time, schedule name, status, input snapshot/version, output summary, error code |
| `deliveries` | run ID, channel, target identifier, sent time, delivery status, Discord permalink or GitHub URL |
| `evidence_refs` | run ID, claim/event ID, source title, URL, publication time, primary/reported classification, retrieval time |
| `health_checks` | service/agent, evaluated time, state, rule that produced the state, last successful run, next expected run |
| `ui_acknowledgements` | user ID, run/alert ID, acknowledged time; strictly presentation metadata |

Store long outputs, source extracts, and logs separately from the fast dashboard summary; `agent_runs` should retain references. All API responses should have stable schemas, explicit timestamps with time zones, pagination for activity records, and a documented error shape.

## Health rules and alerts

- A run is **healthy** only after both processing and its required delivery succeed.
- A run is **attention** when it is late, partially complete, waiting for retry, or finished with a non-critical source/scanner failure.
- A run is **failed** when required input, analysis, or delivery failed, or a connector is unauthenticated.
- The UI must show the failed component and a useful diagnostic; for example: “Reuters fetch failed at 09:02 ET; retry 2 of 3; no substitution source used.”
- Discord receives concise alerts only for failures, missed schedules, or repeated retries—not duplicate normal health updates.

## Security and privacy requirements

- Require the user's existing authentication and least-privilege, read-only UI credentials.
- Never expose Discord tokens, GitHub/Notion credentials, portfolio account identifiers, raw private messages, or full licensed article text in the browser.
- Use signed/deep links to Discord and GitHub rather than copying third-party content into the UI.
- Redact secrets and personal data from logs before persistence; audit access to detailed run records.
- Treat Discord/GitHub delivery as idempotent: retries must not create duplicate comments or messages.

## Implementation order

1. Define shared `agent_runs`, `deliveries`, `evidence_refs`, and `health_checks` schemas; have all three agents emit them.
2. Build the health evaluator and Discord failure alerts.
3. Build the health page with deep links to existing Discord/GitHub messages.
4. Add the activity/decision log and filters.
5. Add run-detail pages, redaction, retention, and acknowledgement metadata.
