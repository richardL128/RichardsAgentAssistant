# LifeAgent implementation plan

## Decision summary

Build one Python application with a default `api` service, PostgreSQL, one task
queue, one local model endpoint, schemas, audit records, and deployment
configuration. Code review, academic planner, and finance remain separate
workflow domains. Scheduled code-review and finance Qwen workers remain
non-executable in the current runtime. Academic Qwen has one configured path:
messages from an authorized owner in the configured private Discord channel,
received by the native host wake daemon.
The model-free academic morning notification is the sole automatic academic
schedule.

Run Ollama natively on the Mac, not in Docker. The Dockerized API uses
`http://host.docker.internal:11434` to call it. This preserves Apple Metal
acceleration and lets the model weights live once on the M2 Max instead of
being copied into images. Never expose port 11434 beyond the Mac.

The configured local reasoning model is `qwen3-32gb:latest` with a pinned
digest in normal deployments. Keep one concurrent physical model request and a
modest context limit until latency and memory pressure justify a change. Qwen is
not preloaded at startup: the first authorized message causes the first real
structured request, and `OLLAMA_MODEL_KEEP_ALIVE_SECONDS=300` lets Ollama unload
the model after a short idle period.

The build is deliberately Python-first. FastAPI serves both the API and the read-only console (Jinja templates with small HTMX interactions), so there is no React/Node frontend application to maintain.

## Chosen stack

| Area | Technology | Why this is the default |
| --- | --- | --- |
| Language and package management | Python 3.12, `uv`, `pyproject.toml` | One modern, fast Python toolchain and a reproducible lockfile. |
| Web/API/UI | FastAPI, Pydantic v2, Jinja2, HTMX, Tailwind CSS | FastAPI is a strong fit for webhooks, typed APIs, and internal pages. Server-rendered templates keep the small, read-only console in the same Python service; HTMX adds filtering/acknowledgement without a SPA; Tailwind produces the responsive, accessible static CSS at build time. |
| Agent orchestration | LangGraph + LangChain + `langchain-ollama` (`ChatOllama`) | LangGraph makes every workflow step, retry, checkpoint, and approval boundary explicit. LangChain supplies the stable Ollama client and Pydantic-oriented structured outputs. Use graphs/workflows, not unrestricted ReAct agents. |
| Model runtime | Native Ollama, `qwen3-32gb:latest` | Local, private inference on Apple Silicon. Qwen is lazily loaded only by a message from an authorized owner in the private Discord channel; deterministic Python code collects facts and performs writes. |
| Relational data | PostgreSQL 16 with optional `pgvector`, SQLAlchemy 2, Alembic | One durable source for domain data, idempotency keys, audit events, run metadata, and dashboard queries. `pgvector` is an extension in this existing database, not a second database. Alembic makes every schema change reviewable. |
| Background work | Procrastinate (PostgreSQL-backed Python task queue) | The queue retains deterministic task, lock, retry, and audit contracts. The Discord academic wake job is the only configured model job; the academic morning notification is scheduled but model-free. |
| Long artifacts | Local Docker volume behind an `ArtifactStore` interface | Keeps phase-one local and simple while separating raw logs, PDFs, source extracts, and reports from dashboard tables. The interface can later switch to S3-compatible storage without changing agents. |
| External HTTP | `httpx` with typed, narrow connector adapters | Avoids an SDK per vendor. Each adapter has an allowlisted base URL, timeouts, rate limits, retry policy, and redaction rules. |
| Document extraction | PyMuPDF first; bounded local OCR for pages with insufficient text | Fast deterministic text/page extraction and page-level citations. Do not add a broad document-AI platform. |
| Code-review tools | `git`, GitHub REST API, Semgrep, Gitleaks, Trivy, repository-native test/lint commands | Deterministic tools find and substantiate issues; Qwen receives their compact results plus relevant diffs, rather than being asked to guess from a whole repository. |
| Quality | pytest, pytest-asyncio, Testcontainers, Ruff, Pyright, pre-commit | Test the system at unit, connector-contract, database, and Compose integration levels using the same tools locally and in CI. |
| Observability | JSON `structlog`, PostgreSQL audit/run tables, `/health` endpoints | Structured logs plus queryable business records provide enough visibility locally without adding a separate telemetry platform on day one. |
| Secrets and access | `.env` only for local development; Docker secrets or host keychain for real credentials | Secrets stay outside prompts, database rows, browser responses, and committed files. Use scoped GitHub App, Discord bot/webhook, and Notion integration credentials. |

### Explicitly not selected yet

- **No Kubernetes, microservice-per-connector, or separate frontend repository.** Docker Compose and one Python codebase suit a single Mac deployment.
- **No separate vector-database service.** PostgreSQL full-text search remains available for exact course-document lookup, while assessment material uses active, assessment-scoped exact `pgvector` retrieval in the same database. Neither path replaces structured assessment fields or source citations.
- **No autonomous browser/search tool.** Finance retrieval is exactly the approved, versioned source adapters; code and school connectors have narrowly specified operations.
- **No LangSmith dependency.** Local structured logs and persisted runs are sufficient initially; tracing can be added later without changing graph logic.
- **No custom authentication in the first local-only deployment.** Bind the UI/API to `127.0.0.1`. If remote access is needed, put it behind the user's existing identity-aware network/proxy (for example Tailscale) before exposing it; do not invent a password system.
- **No brokerage connection or trading capability.** Finance outputs remain source-cited research context and thesis prompts.

## Target topology

```text
                           macOS host (not a container)
          Discord Gateway LaunchAgent     Ollama LaunchAgent + Qwen
                    | signed loopback ref          ^
                    v                              | host.docker.internal:11434
 Docker Compose ---------------------------------------------------------------
 |  api + server-rendered UI                                                 |
 |          |                                                                 |
 |          +---------------- PostgreSQL (data + task queue + audit)        |
 |                     ^   ^                                                  |
 |                     |   |                                                  |
 |        artifact volume  |                                                  |
 |                     |   |                                                  |
 ------------------------------------------------------------------------------
```

Use one image, such as `lifeagent-app`, for the `api`. The default Compose
operation starts `postgres`, `api`, and the academic planner worker; the
Discord academic wake job is the sole model path.
No code-review or finance Qwen workers are runtime options. The academic worker
is isolated to Discord academic work, ingestion, model-free confirmations, and
the model-free automatic academic morning notification.

Default service names:

```text
postgres, api, worker-academic-planner
```

`docker compose` never starts an Ollama container or the Discord Gateway. The
native LaunchAgents remain available if Compose is stopped. In the installed
runtime, PostgreSQL and `worker-academic-planner` stay resident for the
model-free morning schedule while the API may remain cold between Discord
conversations. The `api` health page must
report “Ollama unavailable” when its host health check fails. Operators start,
inspect, and unload the host runtime with `scripts/ollama_qwen_start.sh`,
`scripts/ollama_qwen_status.sh`, and `scripts/ollama_qwen_unload.sh`.
For Docker reachability, the host script uses `OLLAMA_HOST=0.0.0.0:11434`;
operators must protect that LAN-reachable bind with the macOS firewall/trusted
network controls. Compose must not publish port 11434.

## Application shape

```text
app/
  api/                 # FastAPI routes, Jinja views, webhook handlers
  core/                # config, time, ids, logging, redaction, error shapes
  db/                  # SQLAlchemy models, repositories, Alembic migrations
  queue/               # Procrastinate tasks, retry and idempotency helpers
  llm/                 # ChatOllama factory, concurrency gate, prompts, schemas
  artifacts/           # local ArtifactStore and retention handling
  connectors/          # github, discord, notion, finance_sources
  agents/
    code_review/       # graph, deterministic analysis, report publisher
    academic_planner/  # graph, extraction, deterministic allocator, publisher
    finance/           # graph, eight-source retrieval, exposure mapper, publisher
  health/              # deterministic health evaluator and alert policy
  templates/           # Jinja templates for the read-only console
  static/              # CSS and minimal browser-side assets
tests/
  unit/ contract/ integration/ fixtures/
infra/
  compose.yaml Dockerfile .env.example
docs/
```

Keep prompt text and output Pydantic models next to the corresponding graph. Prompt templates must request only schema-backed output. Validate once in Pydantic; on failure retry a bounded repair prompt, then record an `analysis_invalid_output` failure with the raw output stored in the redacted artifact store.

## Shared operating rules

### LLM boundary

1. A connector performs a narrow, authenticated request and normalizes its result into Pydantic data.
2. Deterministic code validates freshness, ownership, allowlists, schemas, and idempotency.
3. A LangGraph node receives a compact packet of normalized facts and returns a Pydantic proposal.
4. Deterministic code validates the proposal and performs an approved publish/write, or creates an approval request.
5. Every attempt and delivery receipt is recorded before the job is marked successful.

Tools must be ordinary Python functions behind the graph, each with one purpose and least privilege. Do not give Qwen shell access, database credentials, filesystem roots, arbitrary URLs, arbitrary GitHub methods, or a generic “send Discord message” function.

All model calls go through `llm.gateway`. The gateway supplies the exact model
identifier, low temperature for factual extraction/review, timeouts, token
limits, `keep_alive`, an asyncio semaphore of one, request IDs, and
usage/latency telemetry. The authorized Discord path calls the narrow Ollama
runtime readiness boundary, which probes only `/api/tags`, validates model name
and optional digest, coalesces simultaneous readiness checks, and never spawns a
shell or accepts a host command from configuration.

### Time, retries, and idempotency

- Store all timestamps as timezone-aware UTC. Convert to `America/Toronto` only at scheduling and display boundaries.
- Procrastinate jobs use locks to prevent duplicate enqueueing. Academic
  material embeddings use the separate local embedding model at low priority and
  do not compete with Discord-triggered Qwen reasoning. The automatic academic
  morning notification is model-free and does not take the local-model lock.
- Every work item has a deterministic idempotency key, for example
  `academic-discord-message:<message-id>:proposal:v1` or
  `academic-morning:YYYY-MM-DD:HHMM:v1`.
- Scheduled morning Discord delivery derives from the same stable local period:
  `academic-morning-delivery:YYYY-MM-DD:HHMM:v1`.
- Procrastinate retries transient connector/model failures with capped exponential backoff and jitter. It never retries authorization failures until credentials change.
- Publishers use provider-side idempotency where available, otherwise persist a delivery intent before sending and reconcile an uncertain send before retrying.
- A job is complete only when required processing, persistence, and delivery all have durable success records.

### Academic document retrieval and embeddings

Embeddings are useful when the planner has to answer *meaning-based* questions across many documents—for example, “Which rubric requirements apply to this draft?”, “Find past course policies that are similar to this late-submission rule,” or “What material from all my documents is relevant to this study block?” They are not the source of truth for a deadline, weight, test time, or course policy: those facts must still be extracted into typed assessment fields with page/block citations and confirmed when ambiguous.

Use this retrieval design when the planner needs it:

```text
PDF/Notion document -> text + heading/page chunks -> metadata + PostgreSQL full-text index
                                       -> local assessment embedding -> pgvector
user/planner question -> course/term/type filter -> hybrid lexical + semantic retrieval
                       -> cited chunks only -> Qwen answer/proposal
```

- Chunk by heading/page where possible (target 400–700 tokens, about 10% overlap); preserve document ID, course, term, page/block, heading, document version, and access classification on every chunk.
- Use PostgreSQL full-text search plus metadata filtering for transparent exact course-policy lookup. Assessment-material interpretation uses assessment-scoped semantic retrieval as its sole active meaning-selection path.
- Embed assessment-material chunks asynchronously during identifier-only ingestion. Store model identity and dimensions so model changes trigger re-embedding instead of mixing incomparable vectors.
- Use the local `qwen3-embedding:0.6b` model initially (currently about 639 MB),
  through Ollama's embedding endpoint. It is separate from the Qwen reasoning
  model and should be benchmarked/pinned; batch ingestion runs at low priority
  and does not compete with Discord-triggered Qwen reasoning.
- Up to about **5,000 chunks**, exact `pgvector` similarity search is simple and sufficient on this machine. Add an HNSW index only when the corpus reaches roughly **10,000 chunks** or measured p95 retrieval latency exceeds 200 ms. Evaluate answer grounding/recall before and after indexing because approximate indexes trade recall for speed.
- Retrieve a small, diverse set (for example 8–12 chunks), always filtered to the relevant course/term unless the user explicitly asks cross-course. Return citations and have the planner say when no supporting chunk was found; never let retrieved text override structured confirmed fields.

This means a typical student with 5–10 courses, a few outlines, and tens of assignment documents can use exact full-text lookup and semantic assessment guidance without another database service. PostgreSQL plus `pgvector` is enough for this single-user workload.

### Shared data model

Implement these tables first; use UUID primary keys and explicit `created_at`/`updated_at` timestamps.

| Table | Purpose |
| --- | --- |
| `agent_runs` | Run ID, agent, trigger/schedule, model/config/input versions, lifecycle status, summary, artifact references, error code. |
| `run_steps` | Per-node start/end/status/attempt/diagnostic/model-call reference for a reconstructable processing timeline. |
| `deliveries` | Delivery intent/receipt, channel, target, idempotency key, status, external permalink/URL. |
| `evidence_refs` | Claim/event ID, title, URL, publication/retrieval time, source version, primary/reported classification, artifact reference. |
| `health_checks` | Deterministically evaluated state, rule, last success, next due, diagnostic. |
| `audit_events` | Immutable actor/action/target/result events; append-only at the application role level. |
| `ui_acknowledgements` | Presentation-only acknowledgement attached to a user and run/alert. |
| `approval_requests` | Proposed write/publish/change, redacted preview, requester, state, decision, expires-at, audit reference. |

Agent-specific tables are added only when needed: `repositories`, `reviewed_commits`, `review_findings`, and `project_profiles`; `courses`, `assessments`, `study_blocks`, `planning_preferences`, `documents`, `document_chunks`, and `document_embeddings`; `holdings`, `watchlist`, `investment_theses`, `approved_sources`, and `thesis_events`.

Raw source bodies, PDFs, tool logs, model prompts/responses, and rendered reports belong in the artifact volume, named by a content hash. Relational rows hold metadata and an artifact key, not large blobs. Apply redaction before artifact persistence. Retention begins with a documented local policy (for example 90 days for raw logs/extracts, retain immutable run summaries/audit metadata) and is configurable by class of data.

### Security baseline

- Use a GitHub App rather than a broad personal token: read repository contents/metadata, receive push webhooks, and pull-request/comment write access only when high-confidence comments are enabled.
- Verify GitHub and Discord webhook signatures before queueing a job. Deduplicate delivery IDs.
- Use a Notion integration token scoped only to the intended workspace/databases. Planner writes are blocked until a user confirms the proposed change.
- Finance connectors have an explicit `approved_sources` record: name, base URL/API, source version, license note, enabled flag, and approval audit record. A failed source returns “no result/failure”; it does not fall back to search.
- Keep `secrets.env` ignored. `.env.example` contains names only. Redact access tokens, cookies, authorization headers, portfolio identifiers, private Discord text, and source material restricted by licence from logs and UI data.
- UI routes return escaped Jinja output and a strict Content Security Policy. The API exposes redacted summaries, paginated records, and external deep links—not raw licensed articles or credentials.

## Agent designs

### Code-review agent

The code-review graph is an evidence pipeline, not a code-writing agent:

```text
future architecture change required -> deduplicate SHA -> checkout exact SHA -> classify risk
-> collect diff/context -> run configured tools/tests -> focused Qwen review
-> validate/merge findings -> approval policy -> GitHub/Discord delivery -> audit
```

Initial large-batch ingestion is a separate, rate-limited queue job per repository. It clones/checks out a pinned default-branch SHA, extracts a compact `project_profile` (purpose, languages, commands, conventions, high-risk paths, first-party `AGENTS.md`/`SKILLS.md` instructions), stores citations to the commit/tree, and produces a human-reviewable `SKILLS.md`-compatible artifact. Do not silently make the model’s summary an executable instruction source. At review time, load the reviewed profile and current repository instructions as untrusted context, with path/commit provenance.

The model sees only changed files, bounded surrounding code, manifest/config diffs, native test/lint/scanner output, and the applicable repository profile. It proposes findings in a Pydantic schema containing severity, file/line, explanation, reproduction/missing test, confidence, assumptions, and evidence references. Deterministic validation rejects line locations not in the review packet, duplicate findings, unsupported claims, and speculative/no-actionable findings.

This is historical/planned capability context only. The current
authorized-private-channel runtime cannot schedule or trigger this model path; making
Qwen create code-review drafts would require a future architecture change and a
separate publication policy. The agent never commits, pushes, changes issues, or
applies fixes.

### Academic planner

```text
Notion delta sync -> PDF/page extraction -> uncertainty detection
-> deterministic constraints and priority scores -> deterministic allocation
-> authorized private-channel message -> bounded Qwen planner response
-> proposed Notion changes -> explicit confirmation -> write/audit
```

Represent fixed commitments, sleep, commute, deadlines, availability, and buffers as typed constraints. The schedule allocator is deterministic Python: it allocates finite work blocks over the next 7–14 days and never moves fixed deadlines/tests. Qwen may propose a breakdown, estimates, explanations, and a critique of the candidate plan, but cannot directly alter calendar/Notion state.

Record every extracted fact with its source page/block and confidence. An ambiguous deadline, time zone, weight, rubric, or test scope becomes an `approval_request`/Discord question and is excluded from automatic hard constraints until confirmed. End-of-day natural-language replies become proposed changes; a second confirmation is required before Notion writes.

### Finance thesis-monitoring agent

```text
validate approved allowlist vN -> exactly eight scoped fetches -> normalize/dedupe
-> map facts to holdings/watchlist/ETF exposure -> Qwen event cards + counter-case
-> deterministic source/claim validation -> daily Discord briefing -> thesis audit
```

This is historical/planned capability context only. The current runtime cannot
execute the scheduled finance model path; adding it requires a future
architecture change after the user approves the exact eight source records,
entitlement/API method, and licences. Each source adapter receives only the
allowed source/date/window/tickers/themes. It
must neither search the web nor substitute an unapproved outlet. Store links and
short licensed excerpts only when permitted; the briefing links to original
sources.

Portfolio data is manually managed in PostgreSQL or a reviewed import—not from a broker. Qwen must use the event-card schema in the architecture file, label derived calculations/formulas and forecasts, describe uncertainty/counter-case, and use `monitor`, `revisit thesis`, or `no action`; no buy/sell/position-size directives. “Potential investments” is a watchlist research lead with source evidence and risks, never an execution recommendation.

## Testable implementation phases

Each phase ends with a pull request-sized change, migrations, fixtures, and automated acceptance checks. Do not begin the next phase while its acceptance checks fail.

### Phase 0 — Repository and local platform

**Build**

- Create the Python project, locked dependencies, formatting/type/test configuration, conventional `.env.example`, and pre-commit hooks. Add a pinned Tailwind CLI build stage that emits `app/static/app.css`; the browser receives only compiled CSS, never a Tailwind runtime.
- Add the single application image and Compose services for PostgreSQL, `api`,
  and the sole `worker-academic-planner` scheduled-model runtime.
- Create `/health/live` and `/health/ready`; ready verifies PostgreSQL, Procrastinate's required schema, artifact-volume writability, and the non-secret Ollama `/api/tags` probe.
- Bind application ports to `127.0.0.1`; mount named volumes for PostgreSQL and artifacts.

**Acceptance tests**

```bash
uv run ruff check .
uv run pyright app
uv run pytest -q
docker compose up -d --build postgres api
curl --fail http://127.0.0.1:<api-port>/health/ready
docker compose ps
```

`ready` must return a useful degraded diagnostic when Ollama is absent; it must not leak settings or secrets.

### Phase 1 — Model gateway and quality baseline

**Build**

- Install/pull the exact configured model once on the macOS host through
  `scripts/ollama_qwen_start.sh --pull`.
- Implement `llm.gateway` with `ChatOllama`, model/config version capture,
  timeout, token budget, one-request semaphore, `keep_alive`, JSON/Pydantic
  validation, and a bounded repair retry.
- Implement `llm.ollama_runtime` so authorized Discord messages check
  `/api/tags`, the configured model, and optional digest before the first real
  model request.
- Create a small, versioned evaluation fixture set: code finding triage, finance fact-versus-inference, academic extraction, and invalid JSON/tool-output cases.
- Record latency, input/output token estimates, response validity, and model identity for every evaluation.

**Acceptance tests**

- A container reaches Ollama through `host.docker.internal` while Compose does
  not publish port 11434, and the operator docs warn that
  `OLLAMA_HOST=0.0.0.0:11434` must be firewall-protected on trusted networks.
- All fixture outputs validate against their Pydantic schemas, or are visibly marked failed after the bounded repair path.
- Baseline results document p50/p95 latency and peak memory under one request. Set the initial context/token/concurrency settings from that measurement.
- Run two queued tasks concurrently and verify that only one model invocation is active at once and both complete without corruption.

**Gate**: pin the exact Ollama model identifier/digest and settings only after
the benchmark is saved as an artifact. If the configured model does not leave
enough system headroom, lower the context window before changing model.

### Phase 2 — Shared durable core

**Build**

- Add Alembic migrations for shared tables, repositories, artifact storage, redaction, `RunContext`, and standardized errors.
- Add Procrastinate task definitions, idempotency, retry classification, and
  failed/stalled-job visibility. Periodic Qwen scheduling is non-executable in
  the current runtime.
- Implement structured audit events, deterministic health calculation, and Discord failure-alert adapter (not normal-status spam).
- Add base LangGraph run/checkpoint support backed by PostgreSQL. Graph nodes must be idempotent at their side-effect boundary.

**Acceptance tests**

- Re-submitting an identical idempotency key creates one run/delivery intent only.
- A simulated transient failure retries and records every attempt; an invalid token becomes failed without retry looping.
- A paused approval graph survives an API/worker restart and resumes with the original run ID.
- The health evaluator reports healthy, attention, and failed from fixture records—not model self-report.

### Phase 3 — Code review: one repository end to end

**Build**

- Configure one GitHub App installation and signed push webhook.
- Build safe repository checkout to an ephemeral per-run path; enforce SHA, repository allowlist, command timeout, output limits, and cleanup.
- Implement risk classification, diff/context packet assembly, Semgrep/Gitleaks/Trivy/native command runners, model finding proposal, validation/deduplication, report artifact, and Discord summary.
- Add the rate-limited one-repository project-profile ingestion job.

**Acceptance tests**

- Fixture repository with seeded regression, leaked test secret, and documentation-only commit produces attributable, de-duplicated findings and suppresses vague/document-only speculation.
- A push replay is deduplicated by repository/SHA; an exact SHA is checked out rather than the branch tip.
- Scanner/test timeout and clone failure create attention/failed records with usable diagnostics and do not block other jobs.
- Discord delivery is idempotent. No GitHub comment is created in this phase.

**Gate**: evaluate at least 20 representative commits manually. Enable a very narrow high-confidence inline GitHub-comment policy only if false positives and incorrect line references meet an agreed threshold; otherwise retain Discord/report-only delivery.

### Phase 4 — Code review: account-scale ingestion and daily operation

**Build**

- Add GitHub repository discovery and one-at-a-time initial ingestion, resumable from persisted state.
- Preserve nightly Toronto daily consolidation, catch-up, and webhook-triggered
  high-risk quick scan as historical/planned capability context only; these
  model paths are non-executable until a future architecture change.
- Add review history, dismissed finding reasons, project-profile review/refresh, and report format aimed at a coding harness proposing a fix.

**Acceptance tests**

- Interrupting initial ingestion resumes at the next unprofiled repository without duplicating profiles.
- Time-zone boundary tests prove “today’s pushes” is Toronto midnight-to-now, including DST fixtures.
- A daily report includes only actionable records and delivery receipts.

### Phase 5 — Academic planner

**Build**

- Configure a scoped Notion integration and map the three required databases.
- Implement Notion delta sync, private assessment-body/attachment ingestion,
  bounded PyMuPDF/OCR extraction with page or block citations, PostgreSQL
  full-text lookup, assessment-scoped local embeddings, and uncertainty queue.
- Implement deterministic priority scores, a constraint-aware 7–14 day
  allocator, Discord-triggered grounded Qwen academic responses, the model-free
  scheduled academic morning notification, model-free reminders, and
  confirmation-only Notion writes. The historical model-based MorningBriefing
  schedule and formatter are non-executable.

**Acceptance tests**

- Notion fixtures covering an assignment, quiz, fixed class, incomplete block, ambiguous PDF deadline, and DST boundary produce valid records/citations.
- Retrieval fixtures show exact course/policy questions answered from full-text search with citations; once embeddings are enabled, paraphrased queries return only chunks from the requested course/term and retain their citations.
- The allocator never moves a test/deadline/fixed commitment, preserves configured buffer, and carries incomplete work forward visibly.
- An ambiguous fact sends a question and does not become a hard constraint.
- An authorized natural-language message in the configured private Discord
  channel checks Ollama readiness, creates a proposal when appropriate, and
  makes no Notion write until the exact confirmation event is supplied.
  Messages from other channels or users do not persist, deliver, check
  readiness, or call Qwen.
- A controlled academic morning trigger uses
  `ACADEMIC_MORNING_SCHEDULE` in `APP_TIMEZONE`, catches up only inside
  `ACADEMIC_MORNING_CATCHUP_GRACE_MINUTES`, records the stable run and delivery
  keys, sends at most one live Discord notification for the period, and is
  replay-safe.

### Phase 6 — Finance briefing (only after source approval)

**Build**

- Add migrations/UI-visible records for holdings, watchlist, ETFs, theses, approved source version, and source entitlement/health.
- Implement exactly eight adapter calls, normalization, freshness/language/licence validation, event deduplication, exposure mapping, event-card graph, and thesis journal updates.
- Preserve the Toronto market-open schedule as historical/planned capability
  context only. It is non-executable in the current runtime; adding an
  evidence-linked scheduled Discord briefing requires a future architecture
  change.

**Acceptance tests**

- A run issues exactly eight source calls; a failing source is reported and no search/fallback request occurs.
- Every generated number in a fixture event card has unit/date/source and every derived number has a formula/source list.
- Cards distinguish verified facts, uncertainty, counter-case, and permitted impact labels; a prohibited trade directive fails schema/content validation.
- No raw licensed article appears in the persisted UI payload or Discord summary beyond approved excerpts/links.

**Gate**: record the approved sources, licences/subscriptions, and compliance
boundaries before any future architecture change. Do not add a scheduled model
path merely because code is complete.

### Phase 7 — Read-only operations console

**Build**

- Build the `/`, `/activity`, `/activity/:runId`, and `/settings/sources` views from the architecture document using FastAPI/Jinja/HTMX.
- Add stable read-only API endpoints with pagination/filtering and a UI-only acknowledgement endpoint.
- Add deep links, Toronto timestamps, accessible healthy/attention/failed labels, redacted run details, and phone-responsive CSS.

**Acceptance tests**

- Browser tests show a card for each agent/service with state, exact last success, next expected run, diagnostic, and filtered activity link.
- Activity filters work by agent/date/attention/repository/ticker-theme/course and never execute output HTML/Markdown.
- Acknowledgement changes only `ui_acknowledgements`; it cannot enqueue work or contact an external service.
- Security tests prove secrets/private source bodies are absent from API and HTML responses; keyboard navigation and narrow viewport smoke tests pass.

### Phase 8 — Reliability, backup, and operating runbook

**Build**

- Add database backup/restore procedure, encrypted host backup target, artifact retention job, graceful shutdown, Compose restart policy, and connector-token expiry diagnostics.
- Add operational runbooks for model unavailable/slow, queue backlog, duplicate delivery, failed database migration, source failure, GitHub rate limit, and Notion ambiguity.
- Add a CI workflow that runs lint/type/unit tests and container integration tests; production deploy remains a deliberate local Compose action.

**Acceptance tests**

- Restore a disposable PostgreSQL backup into a clean Compose environment and verify run/audit/artifact references.
- Kill a worker during a non-side-effecting graph step and confirm safe retry/resume; kill it during a delivery intent and confirm no duplicate message/comment.
- Simulate unavailable Ollama, PostgreSQL, a stalled worker, and each connector; health state and Discord failure policy match the documented rules.

## Implemented enhancement — semantic assessment-material retrieval

Semantic retrieval is active for arbitrary assessment-page body text and
supported PDF attachments. Typed dates and commitments remain authoritative;
full-text search remains available for exact lookup.

The implementation uses exact assessment-scoped similarity now because academic
guidance requires meaning-based selection from heterogeneous material even for a
small corpus. It does not wait for a document-count threshold.

Do not expand this into a universal rubric schema or approximate index merely
because documents exist.

The active path extends `academic_documents` and `academic_document_chunks`,
embeds assessment material with the separately pinned local embedding model,
and sends at most a bounded set of assessment-owned cited chunks to Qwen. The
shared model lock keeps embedding ingestion from competing with
Discord-triggered reasoning. Retrieved content never overrides confirmed
structured facts.

Use exact `pgvector` similarity search initially. Add an HNSW approximate index only after approximately **10,000 chunks** or a measured p95 retrieval latency over 200 ms. The index improves speed, but can slightly reduce recall, so re-run the retrieval evaluation after adding it.

## Configuration contract

Document these settings and validate them on startup; values shown are names, not secrets.

```dotenv
APP_TIMEZONE=America/Toronto
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen3-32gb:latest
MODEL_TRIGGER_MODE=authorized_discord_channel
OLLAMA_MODEL_KEEP_ALIVE_SECONDS=300
OLLAMA_STARTUP_TIMEOUT_SECONDS=30
OLLAMA_MAX_CONCURRENCY=1
OLLAMA_NUM_CTX=2048
EMBEDDING_MODEL=qwen3-embedding:0.6b
ACADEMIC_MORNING_SCHEDULE=08:00
ACADEMIC_MORNING_CATCHUP_GRACE_MINUTES=30
DATABASE_URL=postgresql+psycopg://...
ARTIFACT_ROOT=/var/lib/lifeagent/artifacts
GITHUB_APP_ID=
GITHUB_PRIVATE_KEY=
GITHUB_WEBHOOK_SECRET=
DISCORD_BOT_TOKEN=
DISCORD_WEBHOOK_SECRET=
NOTION_TOKEN=
FINANCE_SOURCE_ALLOWLIST_VERSION=
```

`MODEL_TRIGGER_MODE` is closed to the authorized private Discord channel. There is
no separately configured periodic model path. `ACADEMIC_MORNING_SCHEDULE` is an
executable model-free schedule interpreted in `APP_TIMEZONE`; the default
30-minute catch-up grace is configured by
`ACADEMIC_MORNING_CATCHUP_GRACE_MINUTES`.

Add explicit settings for retry cap, per-connector timeouts, repository
allowlist, Discord targets, artifact retention, and each finance source.
Settings that broaden access must be versioned/audited. Any scheduled Qwen
trigger requires a future architecture change.

## Delivery order and first implementation ticket

Start with **Phase 0**, then Phase 1 and Phase 2 before building agent functionality.
The executable model path is the Discord-mentioned academic assistant.
Scheduled academic Qwen, code-review Qwen, and finance Qwen flows remain
historical/planned context; finance also waits for explicit source/entitlement
approval. The automatic academic morning notification is the sole executable
academic schedule and is model-free. Build the UI only after shared records
exist, so it reports real operations rather than placeholders.

The first ticket should create `pyproject.toml`, `app/core/config.py`, the Compose topology, health endpoints, a minimal migration, and the CI-quality commands from Phase 0—nothing agent-specific. Its definition of done is the Phase 0 acceptance block above.

## Reference links

- [Ollama Qwen model tags](https://ollama.com/library/qwen3) — source for the
  host-managed local Qwen reasoning model.
- [Ollama Qwen3 Embedding tags](https://ollama.com/library/qwen3-embedding/tags) — documents the separate, lightweight local embedding-model option.
- [LangChain ChatOllama integration](https://docs.langchain.com/oss/python/integrations/chat/ollama) — `langchain-ollama` supports tool calling, structured output, and native async.
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview) — supports the selected durable, stateful orchestration and human-approval boundary.
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) — documents persisted approval pauses/resume requirements.
- [Tailwind CLI installation](https://tailwindcss.com/docs/installation/tailwind-cli) — supports generating static CSS at build time, including with a standalone executable.
- [Procrastinate documentation](https://procrastinate.readthedocs.io/en/stable/) — documents the selected PostgreSQL-backed queue, retries, locks, and periodic jobs.
- [pgvector documentation](https://github.com/pgvector/pgvector) — documents exact similarity search, optional HNSW indexing, and hybrid full-text/vector retrieval.
