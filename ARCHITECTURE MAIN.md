# Architecture Main

## Shared design principles

All workflow domains follow the same pattern:

```text
Narrowly scoped connectors → fact extraction → local Qwen reasoning
→ proposed output/action → user approval → audit log
```

The local Qwen model performs reading, reasoning, prioritization, and explanation. Deterministic tools collect facts, run tests/scanners, and perform approved writes. Credentials remain outside the model context: the orchestration layer exposes narrow tools such as “fetch today's commits” or “read an assessment page,” rather than unrestricted account access.

The configured runtime permits Qwen in two bounded paths: an authorized message
in the private academic Discord channel, and bounded event interpretation plus
non-course category composition for the automatic morning calendar briefing. A native
macOS LaunchAgent owns the
sole Discord Gateway connection, acknowledges interactive messages, wakes the
fixed Compose services and Ollama API, and submits an HMAC-signed reference to
the loopback backend. There is no scheduled study-plan allocation workflow;
scheduled Qwen workflows for code review and finance remain non-executable.
Exact `confirm <proposal-id>` and `reject <proposal-id>` commands and
clarification buttons remain model-free exceptions.

The API may remain cold between conversations, but the academic worker stays
resident with PostgreSQL. That worker owns the minute-level periodic deferrer;
keeping it resident is what makes the configured morning occurrence executable
without loading Qwen or waiting for an inbound Discord message.

Ordinary authorized Discord requests use one generic durable native conversation
session per owner/channel. Relational rows contain only lookup, revision,
idempotency, lifecycle, expiry, and content-addressed artifact keys; immutable
private artifacts contain the ordered user, assistant, native tool-call,
tool-result, provider-reasoning, lifecycle, and trusted host-tool checkpoints.
Every model boundary receives a host-budgeted working set: current policy and
owner input, an adjacency-safe recent tail, a validated cumulative session
summary, relevant active generic owner memories, and current tool-loop state.
The full append-only transcript remains the recovery and audit source and is
never replaced by summary or memory prompt blocks. The model terminates each
turn with the typed `emit_conversation_response` control tool, selecting
`awaiting_user` or `completed`; host code does not infer continuity from prose.
Exact cancel, confirmation, and rejection commands remain model-free controls.

The worker lazily reuses one Discord service and model gateway across wakes, but
durability never depends on that process or on Ollama residency. No inference or
database transaction remains open while a person replies. A worker restart,
Ollama unload, or container restart reloads the transcript and trusted tool
capabilities from PostgreSQL plus the artifact store. Open sessions are compacted
only into separately validated derived summaries; canonical transcripts are
never truncated. Summary corruption fails closed, expiry asks the owner to
resend, and context-capacity exhaustion preserves the session for recovery.
Generic owner memory uses separate owner/channel-scoped tables and private
artifacts; explicit remember/correct/forget actions are required for active
writes, and academic learning-focus memory remains a separate domain.
The native default is `qwen3:14b` at a 32,768-token context with 26,624 input
tokens, 2,048 output tokens, and a 4,096-token reserve; startup rejects
incoherent budgets and a mismatched pinned digest.

The host-controlled morning agenda runs from `ACADEMIC_MORNING_SCHEDULE` in
`APP_TIMEZONE` and uses `ACADEMIC_MORNING_CATCHUP_GRACE_MINUTES` as its bounded
catch-up window. Host code selects course work due today and over the following
seven local dates, plus Jobs, misc, and reserved schedule events that overlap
the intended local day. Qwen interprets bounded event evidence and composes
bounded non-course category prose, while host code owns authorization, coverage,
dates, links, ordering, validation, deterministic Courses rendering, cache
reuse, and delivery. Each period is keyed as
`academic-morning:YYYY-MM-DD:HHMM:v1`; the persisted four-embed manifest uses
`planner-morning-four-v3:YYYY-MM-DD:HHMM:v1:<category>:v1`.

A second host-controlled schedule runs the v2 evening course-task checklist at
`ACADEMIC_END_OF_DAY_SCHEDULE`, with bounded catch-up from
`ACADEMIC_END_OF_DAY_CATCHUP_GRACE_MINUTES`. It refreshes the academic catalog,
uses local model semantics plus a critic to select current-day movable course
work, opens an artifact-backed native conversation with a trusted checklist
checkpoint, and sends the first concrete task question. Nightly completion
answers can apply only the guarded `Completed — <existing title>` rename for
the current task. Incomplete answers preview one Toronto-calendar-day move,
which can be applied only after a separate natural confirmation in the same
nightly session. Other academic proposals still require exact
`confirm <proposal-id>`. An existing owner conversation delays the checklist
rather than replacing it, and `skip` closes that night's check-in without a
write.

Native source, configuration, dependencies and wake state are installed under
`~/Library/Application Support/LifeAgent`, independent of the development
checkout. Deployment builds the application image and snapshots the native
runtime together; the wake path does not build or download dependencies.
Academic catalog tools refresh Notion once per conversational turn before
searching synchronized data, while general questions do not require Notion.
Academic, career, and enabled LEARN list tools use the same host-owned query
contract. Relative scopes (`today`, `tomorrow`, `this_week`, `upcoming`, and
`overdue`) resolve once from the immutable owner-message timestamp and owner
timezone; activity, archival, completion, source, role/course, lexical, and
date constraints are applied before stable cursor pagination. Each bounded
result envelope records normalized filters, source freshness, completeness,
and pagination state. Model-facing results are either complete JSON records
within the payload budget or a typed oversize error—never a successful string
prefix.

Temporal list answers select only IDs from a current trusted envelope. The host
validates completeness and stale-data acknowledgements and renders canonical
titles and owner-local dates beneath `emit_conversation_response`. Academic
freshness is scoped to the requested calendars, while the intentional career
cache fallback is marked `cached_stale` and always disclosed. Root and domain
tool checkpoints use the v2 contract and include enabled LEARN capabilities;
incompatible v1 checkpoints restart safely instead of reactivating inventory
reads.

The system should use Toronto local time unless explicitly configured otherwise.

---

## 1. Daily cross-repo code review agent

### Goal

Review every push made that day across all repositories, then send one consolidated, evidence-based report.

```text
GitHub push history / webhooks
          ↓
Per-repo checkout at exact commit SHA
          ↓
Diff + tests + security scanners + dependency checks
          ↓
Qwen: focused reviews and finding triage
          ↓
Daily report
```

### Inputs

- GitHub account activity or push webhooks, including repositories not cloned locally.
- Each commit’s before/after SHA, diff, changed dependency/configuration files, and PR description when available.
- Repository-specific instructions: build/test commands, languages, framework, and ownership files.
- Tool output from tests, lint/type checks, secret scanning, dependency audits, Semgrep/static-analysis findings, and language-specific scanners.

- On the code's first run, it should do a "large batch" ingestion. It should ingest each project in the user's GitHub account one by one, perform a larger "code review" just to get an idea of the each repo's content and what it performs. It should store this context in a SKILLS.md. So when a particular project is being analyzed, a SKILLS.md which matches that project should be ingested. 

### Review flow

1. Discover all pushes made since midnight Toronto time and deduplicate commits already reviewed.
2. Classify risk before deep reading:
   - High-risk: auth, payments, database migrations, permissions, infrastructure, dependency changes.
   - Low-risk: documentation, formatting, generated files.
3. Build a compact review packet: changed code plus relevant surrounding code, tests, and configuration.
4. Run focused reviews for:
   - correctness and regressions;
   - security and data flow;
   - missing tests and maintainability.
5. Merge duplicate findings and discard vague speculation.
6. Deliver a daily report with actionable findings only. It should be presented in a way where a frontier model running on a harness (Claude Code, Codex) would be able to read the findings, and immediately **propose** a fix. 

### Finding standard

Each finding includes:

- Severity: block, important, or suggestion.
- Exact file and line.
- What can go wrong and why the changed code creates the risk.
- A minimal reproduction or missing test.
- Confidence and stated assumptions.

The agent must not create issues or commit fixes. Its normal delivery channels are inline GitHub review comments for high-confidence findings and a concise Discord summary; every published comment is retained in the audit log. It may prepare GitHub issue drafts or TODOs for review.

### Persistent memory and historical schedule context

A local database tracks reviewed SHAs, dismissed findings, project conventions,
and recurring false positives. A consolidated review around 6-7 p.m. and
webhook-triggered quick scans are planned capabilities, but they are not part of
the current authorized-private-channel runtime. They are non-executable today and
would require a future architecture change before they can load Qwen or run as
scheduled/background model workflows.

---

## 2. Investment news and thesis-monitoring agent

### Goal

Produce a daily, source-cited briefing on portfolio/watchlist exposure in tech, oil/energy, defence, and ETFs. The agent surfaces information and challenges theses; it does not provide autonomous trade instructions.

```text
Eight individually scoped trusted-source fetches
                    ↓
Validate, timestamp, and deduplicate source material
                    ↓
Match events → holdings / watchlist / ETF exposure / thesis
                    ↓
Qwen: mechanism, counter-case, and materiality reasoning
                    ↓
Dated daily briefing + thesis journal updates
```

### Strict trusted-source retrieval policy

The agent must **not** conduct open-ended web searches or mix untrusted search results into its analysis.

For each briefing, the orchestration layer makes a fixed number of narrow tool calls—initially **eight**, adjustable later. Each call retrieves only from one named, approved source. Qwen receives only the normalized results of those eight calls plus the user’s portfolio/thesis data.

Example initial allowlist categories:

1. SEC EDGAR filings.
2. Company investor-relations releases and earnings materials.
3. U.S. Energy Information Administration releases/data.
4. U.S. Department of Defense contract announcements or other approved government procurement source.
5. Reuters, if approved.
6. Financial Times, if approved and subscription/API access permits.
7. Wall Street Journal, if approved and subscription/API access permits.
8. Bloomberg, if approved and subscription/API access permits.

The final set should be explicit, versioned, and user-approved. A source tool is allowed to return no relevant items; the system must not substitute another site to fill the gap. It should retain source URL, publication time, title, author/outlet, and any licensing limitations. The briefing cites the original source links rather than reproducing full articles.

### Inputs

- A manually maintained database with ticker, portfolio percentage, sector/theme, personal thesis, and “what would change my mind.”
- The approved eight-source allowlist described above.
- Market and ETF facts from separately approved data sources, such as prices, issuer-published ETF holdings, and benchmarks.

### Historical daily flow

This market-open briefing is a planned capability and is non-executable today.
Adding a scheduled finance model path would require a future architecture change
outside the current authorized-private-channel runtime.

1. Run the eight source-specific tool calls for material published that morning as well as material published since the market closed last night(Market Open) -> This flow should run as soon as the market opens (9:00 am EST)
2. Extract factual claims, publication time, involved companies/sectors/countries, and whether the information is primary or reported.
3. Deduplicate the same event reported by multiple approved outlets.
4. Map events against owned holdings, watchlist entries, ETF constituents/weights, and themes such as AI infrastructure, semiconductors, crude supply, and defence procurement.
5. Ask Qwen structured questions:
   - What changed today?
   - Which holdings or ETFs have plausible exposure?
   - Through what mechanism could it matter?
   - Is the likely impact immediate, medium-term, or merely narrative?
   - What would disconfirm the concern?
   - Is this new information or a restatement of an existing story?
   - What are potential stocks/funds that are worth investing in based on any global events. 

### Output: information and trade suggestions, not trade instruction

Rank a maximum of 5–10 events. Each event card includes:

```text
Event:
Exposure: ticker / ETF / sector, including direct or indirect exposure
Why it may matter:
Time horizon: days / quarters
Verified facts:
Uncertainties:
Counter-case:
Source links:
Portfolio/thesis impact: monitor / revisit thesis / no action
```

### Reusable evidence-first event-card template

Use the following template for every event. It is deliberately sector-neutral: it applies to technology, oil and gas, and defence/military holdings. Write for a 19-year-old investor with some finance knowledge. On first use, define any potentially unfamiliar term in plain English. Keep verified facts separate from analysis and forecasts.

```md
## [Short, factual event title] — [publication date, time zone]

**Event**

[One or two sentences saying exactly what happened, who announced or reported it, and when. Do not describe an estimate, rumour, or management forecast as an established fact.]

**Exposure**

- **Direct:** [ticker(s) owned or watched] — [one-sentence connection to the event].
- **Indirect:** [ETF(s), supplier/customer, commodity, or peer] — [how the connection works].
- **Sector/theme:** [for example: AI infrastructure, crude oil, defence procurement].
- **Portfolio context:** [portfolio weight, if available; otherwise “weight not provided”].

**Why it may matter**

[Explain the economic mechanism in 2–4 short sentences. State whether it could change revenue, costs, demand, supply, margins, regulation, contract backlog, or valuation. Define terms on first use: for example, “backlog (contracted work a company has not yet completed).”]

**Key figures**

| Figure | What it means in plain English | Period / as-of date | Source |
|---|---|---|---|
| [exact value and unit] | [plain-English interpretation] | [quarter/date] | [source name + link] |
| [exact value and unit] | [plain-English interpretation] | [quarter/date] | [source name + link] |
| [derived value, if useful] | [show the calculation] | [same period] | Agent calculation using [linked source(s)] |

Rules: Every number needs a unit, a time period or “as of” date, and a linked source. Label agent calculations explicitly and show the formula. Label company guidance, analyst estimates, and forecasts as estimates—never as results.

**Time horizon**

- **Days to weeks:** [possible near-term market reaction and why].
- **Quarters:** [what must happen in operating results for the thesis to be supported or challenged].

**Verified facts**

- [Atomic, source-backed fact with an inline link.]
- [Atomic, source-backed fact with an inline link.]
- [If sources conflict, state the disagreement instead of selecting a version without explanation.]

**Uncertainties**

- [What is unknown, unconfirmed, estimated, or dependent on a future decision?]
- [What data point, filing, earnings report, or government announcement would clarify it?]

**Counter-case**

[Give the strongest reasonable explanation for why the event may not help or hurt the holding. Include the condition that would make this counter-case more likely.]

**What to watch next**

- [Specific future date, release, contract award, inventory report, earnings report, or price/volume data point.]
- [A measurable threshold or development that would cause a thesis review.]

**Source links**

- [Primary source: company filing, company investor-relations release, government agency, or ETF issuer]
- [Approved reporting source, if used]

**Portfolio/thesis impact: `[monitor | revisit thesis | no action]`**

[One-sentence rationale. This is research context, not a trade instruction.]
```

Sector-specific evidence to prioritize:

- **Technology:** earnings revenue by segment, cloud/AI capital spending, product shipment data, gross margin, customer concentration, and official regulatory filings.
- **Oil and gas:** EIA production, inventory, and demand data; OPEC+ decisions; company production volumes; realized prices; refining margins; and geopolitical supply-disruption evidence.
- **Defence/military:** official contract awards, appropriations/budget documents, backlog, programme milestones, delivery schedules, export approvals, and defence-company filings. Do not treat headlines about conflicts as proof that a specific company will receive revenue.

The agent must not connect to a brokerage, place trades, or use unqualified “buy”/“sell” directives. A thesis-change log records whether an event invalidates, strengthens, or does not affect the user’s existing thesis.

---

## 3. Jobs and interview preparation

Career data is a separate persistence and orchestration domain. The reserved
active `Jobs` row in the configured Courses database is excluded from academic
course discovery. Its ordinary Notion table blocks are ingested losslessly and
without fixed headers. One inline `Interviews` database supplies one page per
interview round; its unique title property named `Name` and date property named
`Date` are the only required scheduling schema.

```text
Jobs page ordinary tables + Interviews data source
                    ↓
bounded source snapshots + Toronto-local interview dates
                    ↓
strict Qwen row interpretation/matching with quoted source evidence
                    ↓
constrained public-HTTPS posting/company research
                    ↓
one versioned preparation plan per interview
                    ↓
deterministic reminders in the existing planner-channel morning message
```

Host code includes incomplete active, non-archived interview events whose date
interval overlaps the intended Toronto-local morning date. It owns the date,
time, title, source link, coverage, and ordering. Qwen may add a cited digest
from bounded event-local properties and page-body text, but it cannot change
dates, IDs, or source citations. Ambiguous matches, missing postings, and
insufficient evidence create focused, durable clarification records. Missing or
invalid dates remain unscheduled and surface as actionable sync diagnostics.

Research accepts only a selected posting URL plus an allowlisted intent. It
uses public HTTPS GETs with DNS/redirect revalidation, private-address and
metadata blocking, content-type/size/time/request ceilings, visible-text
extraction, and source fingerprints. Company-wide search remains fail-closed
until an approved provider is configured; posting-only preparation must say so.

Interview Date changes and preparation-plan publication are proposals. The
host persists an immutable exact preview, requires the matching owner
confirmation event, rechecks Notion page/property/owned-target preconditions,
and updates only the Date property or the LifeAgent-owned preparation child
page.

The reserved active `misc` row in the configured Courses database is a third
planner calendar role for personal/general timed to-dos. The role is assigned
only when the top-level Courses row title normalizes exactly to `misc`; no
hardcoded Notion page, database, data-source, or calendar-view ID is accepted.
The row uses the same seeded child Assessments/Assessment Calendar structure as
course rows, with exactly one `Name` title property and one `Date` date
property. Missing, duplicate, inaccessible, or malformed misc setup fails
closed and produces an actionable setup condition rather than routing the item
to a course or Jobs.

General timed requests such as `scrub toilets @6 pm tdy` are selected
semantically by Qwen through the dedicated `create_misc_task` mutation, not by
course or career tools. Host validation rechecks that the selected target is
the unique reserved `misc` calendar and that the due time is future-dated, then
renders the canonical `Task — <title>` proposal. The write path is identical to
academic proposals: no Notion change occurs before the exact owner
confirmation, and confirmation writes only the discovered `Name` and `Date`
properties on the misc row's Assessments data source.

---

## 4. University personal-assistant and academic planner

### Goal

Use Notion as the source of truth for assignments, tests, quizzes, ordinary
calendar events, rubrics, instructions, and course outlines. Send a
host-rendered automatic morning calendar briefing through Discord and retain
learning-focus memory for personalized academic help.

```text
Notion assessment calendar + attached PDFs/pages
                    ↓
Typed calendar facts + lossless private material ingestion/OCR
                    ↓
Assessment-scoped chunks + local embeddings + semantic evidence critic
                    ↓
Bounded Qwen semantic event and conversation reasoning
                    ↓
Neutral agenda, ordinary event proposals, and confirmation-gated writes
```

The automatic morning path keeps source truth and delivery host-owned while
using Qwen for bounded event semantics plus non-course category composition:

```text
ACADEMIC_MORNING_SCHEDULE in APP_TIMEZONE
                    ↓
fresh complete Notion sync
                    ↓
course work due today + seven dates; today-overlap Jobs, misc, and schedule events
                    ↓
bounded event evidence → Qwen interpreter + critic → exact semantic cache
                    ↓
deterministic Courses rendering + critic-checked Jobs/Misc/Schedule composition
                    ↓
retry-safe four-embed Discord briefing
```

### Suggested Notion structure

Use one top-level Courses database. Each ordinary course row/page owns one
seeded inline Assessments database; its calendar is only a view over those
assessment pages. The reserved `Jobs`, `misc`, and
`Classes + Tutorials + Labs` rows are semantic roles in that same top-level
database, not separately configured Notion IDs. The schedule row is only a
category marker: its events come from the secret Google Calendar iCal address
configured as `ACADEMIC_SCHEDULE_ICAL_URL`. It does not own a child Assessments
database and the iCal schedule is read-only.

| Source | Essential fields |
| --- | --- |
| Courses | Course Code title, priority ranking, course-outline PDF/page, term, assessment policy |
| Per-course and misc Assessments | Name title, Date, optional typed metadata, arbitrary body text, and supported PDF attachments |
| Reserved Classes + Tutorials + Labs schedule | Exact Notion row title plus a secret Google iCal URL in runtime configuration |
| PostgreSQL semantic event cache | Independent activity intent, cited overview/description, model/config/prompt versions |

The configured Discord runtime is one native, role-separated model harness. It
receives authorized free-form input intact, can answer unrelated questions, and
selects tools without a semantic pre-router or command taxonomy. Before a native
tool call, the model's own action description is passed through the durable
response path. The editable progress message also uses bounded, host-authored
activity labels without request details. Structured tool results remain
internal, failures are summarized safely, and hidden reasoning is never exposed.
The host application boundary validates tool schemas, deterministic query
filters and freshness, grounded result IDs and dates, allowlisted/owner-scoped
IDs, time and size bounds, stale-write preconditions, idempotency, and exact
confirmation. The Notion calendar displays the underlying assessment pages.
Study and review activity uses the same ordinary event records
as other timed academic work; model-supported activity intent is stored
independently from the event title and generated prose.

Conversation-triggered course events use this same per-course Assessments
calendar as their sole academic Notion write path. Personal/general timed
to-dos use the unique reserved `misc` row's Assessments calendar through the
dedicated semantic `create_misc_task` mutation. These task deadlines are
calendar reminders. Every message from an
allowlisted owner in the private academic Discord channel is acknowledged,
durably queued, and passed intact to the same harness. A mention is optional and
is removed only as Discord transport syntax. The model may ask a natural
follow-up without a separate deterministic clarification state machine.
A resolved course event or misc task creates an ordered proposal, not a write.
Its Discord preview is rendered by host code in Toronto local time and shows
every target, the exact natural course-event title or canonical
`Task — <title>`, start or due time, and duration when applicable before the
exact `confirm <proposal-id>` command can apply it.

The reserved Classes + Tutorials + Labs schedule is different: its Google iCal
feed is read-only and is never a Notion mutation target. LifeAgent expands
recurrences and exclusions inside a bounded Toronto-local window, stores only
stable hashed source/event identities, and never logs or persists the secret
feed address.

Authorized PDF attachments use the same conversation and confirmation boundary.
The Gateway captures only bounded attachment metadata before the acknowledgement;
the handoff then re-fetches the authenticated Discord message and downloads at
most five PDFs from Discord's official media hosts into the private,
content-addressed artifact store. Relational rows and queued manifests contain
only opaque identifiers and bounded metadata—never attachment URLs, bytes, or
extracted text. The harness can inspect a PDF, search the synchronized assessment
catalog, and propose either a new assessment or attachment to one existing
assessment. The deterministic preview names the target and files, but no Notion
upload occurs before the exact confirmation command.

```text
authorized Discord PDF attachment
                    ↓
bounded capture + private artifact
                    ↓
Qwen inspection and assessment matching
                    ↓
owner-visible proposal + exact confirmation
                    ↓
direct Notion file upload + assessment page/block attachment
                    ↓
canonical Notion re-sync + local indexing/profile refresh
```

A second authorized message with the same captured material atomically
supersedes the earlier pending proposal. Only the newest confirmation token can
apply. Upload uncertainty or partial failure is terminal for automatic replay;
the old architecture is not retained as a fallback write path.

No external calendar is involved: not Google Calendar, Apple Calendar, Microsoft
Calendar, or a separate Notion Calendar API. Confirmed course-event writes create
ordinary assessment pages under the discovered
course-owned Assessments data source, using only the discovered title and date
property IDs. Confirmed misc-task writes use the discovered title and date
property IDs from the unique reserved `misc` row's Assessments data source.
For timed course-event pages, the Notion Date value includes both `start` and
`end`. Historical externally owned titles are left unchanged. Misc tasks are
due-at items and use the single selected
future Date value.

### Ingestion flow

1. Sync assessment metadata, then queue identifier-only material jobs.
2. Preserve arbitrary page-body text and supported PDFs as private, versioned artifacts.
3. Extract up to 15 PDF pages with block/page citations and bounded local OCR;
   mark incomplete visual coverage `partial`.
4. Chunk and embed material locally, with every read hard-scoped to its assessment.
5. Let the semantic material agent select free-form, useful assessment insights
   and require exact evidence chunk IDs plus a separate critic.
6. Build a bounded, versioned material profile from cited evidence; a separate
   critic must accept it before activation, and the previous accepted profile
   remains active if refresh fails.
7. Keep persisted dates, bounds, and commitment invariants deterministic after
   Qwen has semantically interpreted free-form conversation. Ambiguous weights
   or other typed facts never become hard constraints without reconciliation.

### Prioritization and scheduling status

The former deterministic study-block allocator, daily-plan persistence, and
carry-forward scheduler are removed and are not an alternate runtime path.
Material profiles may retain cited scope, effort, and dependency-risk evidence
for interactive assistance, but they do not create PostgreSQL study blocks or
move Notion dates. New calendar time is proposed through the native Discord
harness and requires exact owner confirmation before a scoped Notion write.

### Automatic agenda and interactive updates

The automatic morning notification is the executable agenda schedule. It
performs fresh academic and Jobs syncs, selects course work due today and over
the following seven dates, and selects Jobs, misc, and reserved schedule events
that overlap today. Qwen adds only validated event semantics and bounded
Jobs, Misc, and schedule prose/inferences. Courses are rendered
deterministically from validated per-event overviews and host-owned due labels;
a title-only valid overview can omit description, and model failure falls back
to trusted title-plus-date rows instead of hiding course work. The complete
four-embed manifest is persisted before delivery so retries resume missing
categories without duplicating delivered content. Courses, Jobs, Misc, and
Classes + Tutorials + Labs remain separate categories. If a source is
inaccessible, incomplete, stale, or misconfigured, its category reports an
actionable unavailable state
without hiding independently fresh categories.

Operational health persists this schedule as `health_checks.check_name =
'academic_morning'`. Before the configured occurrence and throughout the grace
window, an absent run is non-overdue. After the grace deadline, the health check
reports attention for a missing run, failed/attention run, missing delivery,
failed delivery, or uncertain delivery. A successful run advances the next
expected time to the following occurrence plus grace.

The nightly checklist is the second executable academic schedule. It records
`academic_nightly_checkin` runs under the `academic-end-of-day` schedule,
delivers one idempotent task question, and opens a durable proactive
conversation for the configured authorized owner. Its operational health row is
`academic_end_of_day`. It never confirms ordinary proposals, activates generic
personal memory, or performs unrelated Notion writes merely because the owner
replies.

Optional Waterloo LEARN access is interactive rather than a third schedule.
When enabled, a dedicated loopback-only, HMAC-authenticated Playwright bridge
uses a host-only browser profile established through manual SSO/MFA. The native
Discord harness exposes search-first course, scheduled-item, and announcement
tools; raw announcement bodies never appear in tool results. The reserved
Classes + Tutorials + Labs schedule is a separate read-only Google iCal source,
so LEARN cannot create, enrich, update, or delete its events. The morning
briefing reads the independently synchronized iCal schedule and never depends
directly on LEARN availability.

Every message from an allowlisted owner in the authorized private channel may
reach Qwen. Exact confirmation and rejection commands remain deterministic,
model-free HITL hooks and are intercepted before the harness. The assistant may
record explicit progress or propose new calendar events, but it must not infer
completion merely because an item appeared in the morning agenda.

Confirmed Notion batches are not treated as atomic because Notion does not
provide multi-page transactions. The proposal enters a durable `applying` state
before the first external write, and an operation journal records each ordered
change by proposal ID and ordinal with bounded receipt information when a page
is definitely created. A repeated confirmation must not issue another create
while the proposal is `applying` or already `applied`. If a connector failure,
timeout, or process crash leaves the batch uncertain, LifeAgent reports the
proposal as not safely verified, directs the operator to inspect the operation
journal and the course Notion calendar, and never claims the whole proposal was
applied unless every operation returned a receipt.

The assistant is a planner and tutor, not a mechanism for completing graded work dishonestly.

---

## 5. Minimal operations UI

The frontend's complete architecture and implementation guidance lives in [frontend/AGENTS.md](frontend/AGENTS.md). The UI is a read-only health and audit console for the three agents; Discord and GitHub remain the places where work and communication occur.

---

## Recommended implementation order

1. Maintain the authorized private-channel academic assistant as the only
   conversation-triggered path for proposing ordinary course calendar events.
2. Keep the removed historical study-plan allocator non-executable. Scheduled
   code-review and finance Qwen workflows also remain non-executable in the
   current runtime; restoring them requires a future architecture change. The
   host-controlled morning briefing and semantic nightly checklist remain the
   only automatic academic schedules.
3. Consider any future scheduled finance briefing only after selecting and
   authorizing the exact eight-source allowlist, subscriptions/data access, and a
   new runtime design.
