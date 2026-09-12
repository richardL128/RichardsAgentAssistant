# LifeAgent operations guide

This guide takes a new user from a fresh checkout to a working local LifeAgent
stack. It assumes:

- macOS or Linux with Docker Desktop/Engine, Docker Compose, `curl`, and `jq`;
- Ollama is managed on the host computer and Qwen can be installed there; and
- the repository root is the current directory.

LifeAgent is local-first: PostgreSQL and the application run in Compose, while
Ollama runs on the host. Keep all credentials in `.env` or a secret manager;
never commit them or paste them into logs, tickets, or chat.

Qwen is configured for authorized private Discord conversations and bounded
event-local semantic analysis in the automatic morning briefing. The native
macOS Discord wake LaunchAgent remains connected while the API is cold,
acknowledges an allowlisted owner's message, starts the fixed local services as
needed, and submits a signed reference to the backend. PostgreSQL and the
planner worker stay resident so scheduled work does not depend on an inbound
Discord message. Health checks, startup, and messages outside the channel or
owner allowlist must not load Qwen.

The one executable automatic planner schedule is the combined morning
notification. Host code refreshes Notion, builds the intended local day's
academic plan, selects course and Jobs events in the exact 10-day-12-hour local
window, validates Qwen-produced event semantics, and sends a persisted,
idempotent multipart Discord briefing.

## 1. Start the platform

Copy the example configuration and edit it before starting anything:

```bash
cp .env.example .env
chmod 600 .env
```

At minimum, set the database identity, a non-default database password, and
operations-console credentials:

```dotenv
POSTGRES_DB=lifeagent
POSTGRES_USER=lifeagent
POSTGRES_PASSWORD=choose-a-local-password
OPS_CONSOLE_USERNAME=admin
OPS_CONSOLE_PASSWORD=choose-an-operations-password
```

Set `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` before the first
`docker compose up`. The Postgres image reads those values only when it
initializes an empty `postgres_data` volume; changing them later does not rename
or re-password the already-created database automatically.

Leave integrations blank until they are configured. For an API-only setup,
build and start the services manually:

```bash
docker compose config --quiet
docker compose up -d --build postgres api worker-academic-planner
docker compose ps
```

Check readiness before opening the console:

```bash
curl --fail http://127.0.0.1:8000/health/ready | jq .
```

Then open `http://127.0.0.1:8000/` and sign in with
`OPS_CONSOLE_USERNAME` and `OPS_CONSOLE_PASSWORD`. The console intentionally
returns `503` when either value is blank.

If the readiness curl cannot connect and `docker compose ps` shows the API
restarting, inspect the logs:

```bash
docker compose logs --tail=120 api
```

If the logs show PostgreSQL authentication or missing-role errors after you
changed `POSTGRES_USER`, `POSTGRES_DB`, or `POSTGRES_PASSWORD`, the existing
`postgres_data` volume was initialized with older credentials. Either restore
the original database values in `.env`, create the new role/database in
PostgreSQL, or reset the local `postgres_data` volume if it contains no data you
need to keep.

The API container runs migrations automatically. It does not own a Discord
Gateway listener. `worker-academic-planner` owns durable planner-channel jobs,
assessment-material ingestion, and the combined morning notification. The
morning path may call the configured Qwen model only for event-local semantics;
no legacy model worker or other scheduled model workflow is a runtime option.

## 2. Connect host Ollama to Docker

The application container cannot use `localhost` to reach a host process:
inside the container, `localhost` means the container itself. Use the Docker
host name instead:

```dotenv
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

### Make Ollama listen on the host interface

Use the checked-in host scripts from the repository root. They read only
allowlisted non-secret Ollama settings from `.env`, respect explicit
environment overrides, and never evaluate `.env` as shell code.

```bash
scripts/ollama_qwen_status.sh
```

Set `OLLAMA_MODEL` to the exact configured Qwen model and pin
`OLLAMA_MODEL_DIGEST` after verifying the installed model:

```dotenv
OLLAMA_MODEL=qwen3-32gb:latest
OLLAMA_MODEL_DIGEST=
MODEL_TRIGGER_MODE=authorized_discord_channel
OLLAMA_MODEL_KEEP_ALIVE_SECONDS=300
OLLAMA_STARTUP_TIMEOUT_SECONDS=30
```

`MODEL_TRIGGER_MODE` keeps interactive model requests closed to the authorized
private Discord channel. Study-plan, code-review, and finance Qwen schedules are
not configured. The separate academic morning schedule is host-controlled and
uses the same configured model only for bounded event semantics.

Clearing `OLLAMA_MODEL_DIGEST` skips digest pinning during first setup. Pin the
actual digest later after verifying the model, by copying the digest returned
from `/api/tags`.

Install or verify the supervised Ollama LaunchAgent with the fixed script:

```bash
scripts/ollama_qwen_start.sh
```

The script loads or kickstarts `com.lifeagent.ollama`, checks the configured
model and optional digest, and exits nonzero with a bounded diagnostic if
readiness fails. It does not use `nohup` or a PID file and never pulls a large
model unless the operator explicitly opts in:

```bash
scripts/ollama_qwen_start.sh --pull
```

Do not forward port 11434 through your router or expose it to the public
internet. Docker Compose maps `host.docker.internal` to the local host; the
application still uses the host-only API port.

The Ollama network bind used for Docker reachability is
`OLLAMA_HOST=0.0.0.0:11434`, which is reachable from the host network/LAN unless
the Mac firewall and local network controls block it. Keep the Mac on a trusted
network, protect the port with the macOS firewall, never add a router
port-forward, and never publish port 11434 from Compose.

Test the path from inside the API container:

```bash
docker compose exec api python -c "import httpx; r=httpx.get('http://host.docker.internal:11434/api/tags', timeout=5); print(r.status_code); print(r.text[:500])"
```

If this fails, run `scripts/ollama_qwen_status.sh`, verify the model name and
digest, then restart the API after changing `.env`:

```bash
docker compose up -d --force-recreate api
```

Qwen is not intentionally kept resident at startup. The first authorized private-channel
message causes the first real native request, which lazily loads the model.
`OLLAMA_MODEL_KEEP_ALIVE_SECONDS=300` keeps it resident across one bounded loop
and then lets Ollama unload it after roughly five idle minutes. To unload it
immediately without deleting model files, run:

```bash
scripts/ollama_qwen_unload.sh
```

A cold start can take noticeably longer than a warm request because Docker may
need to start and Ollama must map the model into memory. The host immediately
creates exactly one message: `I’m waking up LifeAgent and Qwen. Please give me
a little time to respond.` The backend adopts and edits that same message with
runtime state. Unchanged host/model awaits may update at 8, 20, and 45 elapsed
seconds and then every 30 seconds, capped at six host-wake edits and twelve
backend edits per inbound request; each pulse replaces the active line. These
are semantic liveness updates, not token streaming or hidden
reasoning. Runtime-ready means the configured local runtime passed its checks,
not that an answer is ready. The separate answer or proposal preview is sent
before the progress message becomes terminal.

## 3. Configure Notion

LifeAgent uses a Notion internal connection. It does not use a user's Notion
password or browser cookie.

### Courses database configuration

The academic planner accepts one top-level Courses database ID. Each course is
a row/page in that database and owns one seeded inline Assessments database.
Calendar views are presentation only: LifeAgent discovers and queries the
underlying database and data source.

1. Open the [Notion integrations page](https://www.notion.so/profile/integrations).
2. Create an internal integration named `LifeAgent`.
3. Grant read content. Grant update content only if authorized Discord
   clarification buttons should rename ambiguous assessment titles.
4. Copy the integration token into `NOTION_TOKEN`.
5. Open the top-level Courses database, choose **Share**, and add the
   `LifeAgent` connection. The token alone does not grant page access.
6. Copy the Courses database ID from its URL. It is the 32-character
   identifier before any query string. Set:

```dotenv
NOTION_TOKEN=secret-or-ntn-token
NOTION_COURSES_DATABASE_ID=...
```

`NOTION_ASSESSMENTS_DATABASE_ID` and `NOTION_STUDY_BLOCKS_DATABASE_ID` are
accepted only as deprecated migration metadata. They are never queried and can
be removed after confirming the Courses-only sync is healthy. Do not configure
child calendar or data-source IDs; LifeAgent discovers them.

LifeAgent uses Notion API version `2025-09-03`: it retrieves each database
container to discover its physical data-source ID, then queries that data
source. Cursors are scoped to those discovered physical sources so calendars
cannot accidentally share pagination state.

Configure the top-level Courses database with:

| Property | Notion type | Requirement | Example or default |
| --- | --- | --- | --- |
| `Course Code` | Title | Required | `CSC301` |
| `Term` | Select or text | Optional | `Fall 2026`; defaults to `unspecified` |
| `Priority` | Number | Optional | `80`; defaults to `50` |

Inside every course page, add one inline database named `Assessments`. Add a
calendar view to that database and configure the view to use its `Date`
property. The underlying database should contain:

| Property | Notion type | Requirement | Purpose or default |
| --- | --- | --- | --- |
| `Name` | Title | Required | Calendar label, such as `Quiz 1` |
| `Date` | Date | Required for scheduling | Due date or event time |
| `Weight` | Number | Optional | Grade percentage; defaults to `0` |
| `Estimated Minutes` | Number | Optional | Work estimate; uses the planner default when blank |
| `Status` | Status | Optional | For example, `Not started` or `Completed` |

`Assessments` is the name of the child database; `Name`, `Date`, and the other
fields are properties inside it. LifeAgent will associate an event with its
course from this parent-child structure, so the child calendar does not need a
separate Course relation property.

To keep new courses consistent, create a Courses database template:

1. Open the menu next to the Courses database's **New** button and create a
   template named `New Course`.
2. In the template page body, create the inline `Assessments` database.
3. Rename its title property to `Name`, add `Date`, and add any optional
   properties from the table above.
4. Add a calendar view and select `Date` as the calendar date.
5. Create future course rows from this template.

The integration discovers each nested Assessments database automatically;
users do not copy an ID for every course. Discovery requires exactly one child
named `Assessments` or `Assessment Calendar`, exactly one underlying data
source, one `Name` title property, and one `Date` date property. Zero or
multiple matches are reported as setup problems rather than guessed.

### Jobs and Interviews structure

Create exactly one active top-level Courses row/page titled `Jobs`. Inside it,
keep applications in ordinary Notion table blocks. Headers such as `Company`,
`Job`, and `Status` are optional user content: LifeAgent preserves cell order
and asks Qwen to interpret a row with source-cell evidence, so changing headers
or column order does not require a migration.

Inside the same Jobs page, create exactly one inline database titled
`Interviews`. Each page is one interview round. The database requires exactly
one title property named `Name` and one date property named `Date`; other
properties and page-body text are optional context. Put the public HTTPS
posting URL in a supported URL property or the page body. LifeAgent discovers
the database/data source, not a calendar view ID.

Do not create an Applications database and do not add child `Assessments` to
Jobs. The reserved Jobs page is routed exclusively to the career domain.

Company research is constrained and read-only. Direct posting retrieval works
without a search provider. Company-wide search intentionally reports
`provider_unconfigured` until Richard approves and configures a provider. The
bounded defaults are shown in `.env.example` under `JOB_RESEARCH_*`.

Run a read-only refresh and inspect only safe counts/conditions:

```bash
curl --fail -X POST http://127.0.0.1:8000/job-interviews/sync
curl --fail http://127.0.0.1:8000/job-interviews/health
```

The complete user-directed todo-type allowlist is `Quiz`, `Assignment`,
`Tutorial`, `Lab`, and the complete phrase `Studying Block`. Matching is
deterministic, case-insensitive, and respects word or phrase boundaries; the
explicit `Assigment` spelling correction remains accepted as `Assignment`.
Labels such as `Homework`, `Paper`, `Test`, and `Thing` remain unknown, and a
request containing conflicting supported labels (for example,
`quiz assignment`) is also unresolved. Unknown or conflicting labels ask for
clarification and cannot create a write-capable proposal. Ambiguous labels
imported from Notion are persisted for Discord clarification and are not
scheduled or sent to Qwen while pending.

Confirmed creations use one canonical title format: `Quiz — <title>`,
`Assignment — <title>`, `Tutorial — <title>`, `Lab — <title>`, or
`Studying Block — <title>`. LifeAgent replaces an existing supported prefix
instead of stacking prefixes. A conversation-triggered `Studying Block` todo is
a timed item in a course's Notion Assessments calendar with both Date `start`
and Date `end`; it is distinct from planner-generated PostgreSQL study-block
allocations, which are not exported to Notion automatically.

If the Courses database is absent, inaccessible, or not shared with the
`LifeAgent` connection, the academic workflow sends a Discord setup
reminder instead of calling the model or attempting a Notion write. It will do
the same when a course page is missing the seeded `Assessments` calendar or
that calendar lacks its required `Name` title or `Date` date property. The
message will direct the user back to this setup section and confirm that no
Notion changes were made.

Configuration reminders will be deduplicated so an unchanged problem produces
at most one reminder per day. When only some course pages are misconfigured,
the reminder will summarize those courses while correctly configured courses
continue syncing. If Discord is unavailable, the same actionable, non-secret
condition remains visible in persisted health. A successful discovery clears
the active reminder condition.

Academic sync and deterministic schedule construction remain model-free. The
automatic morning notification may call Qwen after host code selects in-window
events and collects bounded event-local textual properties and supported page
blocks. Raw Notion envelopes, relations, files, attachment/OCR content,
credentials, unauthorized Discord message bodies, embeddings, and unrelated
memory do not cross this model boundary.

### Automatic morning notification

Configure the morning schedule in local application time:

```dotenv
APP_TIMEZONE=America/Toronto
ACADEMIC_MORNING_SCHEDULE=08:00
ACADEMIC_MORNING_CATCHUP_GRACE_MINUTES=30
CALENDAR_SEMANTIC_EVENT_TIMEOUT_SECONDS=180
CALENDAR_SEMANTIC_TOTAL_TIMEOUT_SECONDS=600
CALENDAR_SEMANTIC_PROMPT_MAX_CHARS=16000
```

`ACADEMIC_MORNING_SCHEDULE` is interpreted in `APP_TIMEZONE`. The default
30-minute catch-up grace lets the worker recover from a short startup delay; it
must not send a stale morning notification after that deadline. Each scheduled
period records one run with agent `academic_morning_notification`, schedule
`academic-morning`, and idempotency key
`academic-morning:YYYY-MM-DD:HHMM:v1`. The combined academic/interview Discord
manifest derives ordered delivery keys as
`planner-morning-delivery-v2:YYYY-MM-DD:HHMM:v1:NNN`. Each part is at most
2,000 characters, and the persisted manifest lets retries skip parts already
delivered.

Before sending a normal morning plan, the job completes the academic sync and
then the Jobs/Interviews sync. A career failure is disclosed in the combined
message but does not hide a valid academic plan; one invalid interview does not
hide other valid interview reminders. Missing academic sharing, stale academic
sync, or Discord delivery uncertainty remains fail-closed. Semantic model or
critic failure does not suppress trusted event titles and dates; the message
omits unverified prose and includes one bounded availability condition.

For setup recovery or an immediate refresh after fixing a template/share
problem, run the same idempotent boundary manually:

```bash
curl --fail -X POST http://127.0.0.1:8000/academic/sync
```

The response contains bounded counts and diagnostic codes, never raw Notion
response bodies. A `setup_required` response does not call Qwen, create a
schedule, or attempt a Notion write.

## 4. Configure Discord

Discord is used for allowlisted outbound messages and a required native Gateway
connection for authorized academic private-channel messages, exact confirmation/rejection commands, and
clarification buttons. Discord deliveries use durable idempotency keys and the
existing nonce, content-length, and allowed-mentions protections.

For Qwen-powered assistant work, the authorized private-channel Gateway path is
the only configured entrypoint. The Gateway is an outbound WebSocket from the native
`com.lifeagent.discord-wake` LaunchAgent to Discord; it does not require a
public URL. The daemon stores only bounded IDs and timestamps in its local
outbox. Its backend handoff is HMAC-authenticated and bound to loopback. Gateway
handles inbound events and its own session heartbeat; creating and editing the
user-visible progress message uses Discord's REST API.

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Add a Bot user and copy its token into `DISCORD_BOT_TOKEN`.
3. Invite it to a private server with OAuth2 scopes `bot` and
   `applications.commands`.
4. Grant only View Channel, Send Messages, Embed Links, Read Message History,
   and Use Application Commands. Do not grant Administrator.
5. Create private channels for the planner, finance, and code-review outputs.
6. Enable Developer Mode in Discord, right-click the channels, and copy their
   IDs into:

```dotenv
DISCORD_BOT_TOKEN=...
DISCORD_ACADEMIC_CHANNEL_ID=...
DISCORD_ACADEMIC_AUTHORIZED_USER_IDS=[123456789012345678]
DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED=true
DISCORD_APPLICATION_ID=...
MODEL_TRIGGER_MODE=authorized_discord_channel
DISCORD_ACADEMIC_PDF_MAX_ATTACHMENTS=5
DISCORD_ACADEMIC_PDF_MAX_BYTES=20971520
DISCORD_ACADEMIC_PDF_DOWNLOAD_TIMEOUT_SECONDS=15
DISCORD_ACADEMIC_PDF_INTAKE_TTL_HOURS=48
DISCORD_FINANCE_CHANNEL_ID=...
DISCORD_CODE_REVIEW_CHANNEL_ID=...
```

PDF intake is deliberately bounded: one message may carry one to five PDF
attachments, each no larger than 20 MiB. Lower limits may be configured; the
20 MiB ceiling cannot be raised through configuration. Downloads reject
redirects and non-Discord media hosts, verify the declared and observed length,
require a PDF signature, and never forward the bot token to the media host.
Inspection and canonical ingestion retain the existing 15-page extraction/OCR
cap; longer documents are reported with partial coverage instead of implying
that every page was indexed.

Only the listed Discord users can authorize a request. The Gateway is an
outbound WebSocket connection and does not expose a public port. Button
interactions do not require broad message collection or the privileged Message
Content intent.

To accept private planner messages, open the application in the Discord
Developer Portal, select **Bot**, enable only **Message Content Intent** under
Privileged Gateway Intents, and set:

```dotenv
DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED=true
DISCORD_APPLICATION_ID=<the application's numeric Application ID>
```

Free-text mode requests only Guild Messages and Message Content. Do not enable
Presence Intent or Server Members Intent. Keep the academic channel private and
grant the bot only View Channel, Send Messages, Embed Links, and Read Message
History. The Application ID lets LifeAgent normalize bot mentions; the Public
Key remains unnecessary unless a separately deployed HTTP Discord interactions
endpoint is added later.

After setting Discord and Notion configuration, deploy the native wake runtime:

```bash
scripts/lifeagent_host_runtime.sh deploy
scripts/lifeagent_host_runtime.sh status
```

`deploy` builds `lifeagent-app:local` once, runs migrations and tests, records
the image ID, and installs a runtime snapshot under
`~/Library/Application Support/LifeAgent/runtime`. A managed Python installation
under `~/Library/Application Support/LifeAgent/python` provides its independent
virtualenv. The installer requires `uv` on PATH or an absolute `LIFEAGENT_UV`
executable path. The snapshot preserves the HMAC key and ID-only outbox and
copies configuration privately. Neither LaunchAgent depends on the Desktop
checkout or its temporary Python installation.

After both native agents pass the startup check, deploy keeps PostgreSQL and the
academic worker running for the morning schedule, and stops only the API so the
next authorized message exercises the cold API wake path. A Discord-triggered wake uses `--no-build
--pull never`; a missing or stale image marker fails safely and tells the
operator to rerun `deploy`. Use `install` only to reinstall LaunchAgents for an
already deployed image, and `uninstall` to stop and remove both plists without
deleting the image, HMAC key, or outbox.

An authorized message in `DISCORD_ACADEMIC_CHANNEL_ID` may use an exact
model-free command or send any free-form request; mentioning the bot is optional:

```text
completed <assessment-id>
logged <assessment-id> <minutes>
actual <assessment-id> <minutes>
confirm <canonical-proposal-uuid>
reject <canonical-proposal-uuid>
<@bot> create an assignment in ECE 202 due Friday, and delete the old lab in ECE 250
<@bot> I need to study for ECE 250, specifically race conditions and insertion sort.
<attach syllabus.pdf> Add this material to the right assessment and seed my calendar.
```

Free-form requests run through the local Qwen agent loop after
the host Ollama readiness boundary verifies `/api/tags`, the configured model,
and the optional digest. The loop can search only the synchronized course and
assessment catalog. Create, update, and delete/archive operations are returned
as one proposal; none is written to Notion until the exact
`confirm <canonical-proposal-uuid>` response is received. LifeAgent implements
delete as Notion archive and refuses stale or ambiguous targets. With
`DISCORD_APPLICATION_ID` is still used to validate the bot identity and strip
mention syntax, but it does not classify the request. Exact confirmation and
rejection replies remain model-free.

An attachment-only authorized message is valid. After the immediate wake
acknowledgement, LifeAgent re-fetches the Discord message, stores verified PDFs
privately, and lets the harness inspect them and search synchronized assessment
targets. It may propose attaching files to one existing assessment or creating
a new assessment and attaching them. The proposal preview shows the target,
filename, size, and exact confirmation command; it never exposes internal
artifact or intake identifiers. No Notion upload occurs until that command is
received.

If the target is ambiguous, the PDFs remain in `awaiting_target` state. The
owner's next message in the same private channel receives a bounded list of
recent unresolved intake IDs, so the harness can inspect and bind the retained
files without accepting another owner's or channel's material.

The editable progress message moves through honest PDF-specific states such as
capture, inspection, catalog matching, awaiting confirmation, Notion upload,
and seeded with indexing queued/complete/delayed. A partial or uncertain Notion
outcome ends with an actionable failure state; it never remains indefinitely
working or claims indexing completed without a receipt.

Sending a second authorized message for the same intake replaces the pending
proposal atomically. The old confirmation token then fails safely. Confirmed
files use Notion's direct upload API, are attached to the assessment page, and
are followed by canonical sync. Local indexing may finish later; Discord reports
that as delayed instead of re-uploading the file.

Each accepted model attempt adopts the idempotent wake acknowledgement and edits
it with host-owned runtime, turn, generic tool-activity, and reply-preparation
states. Model-authored tool-call narration continues through the separate
durable response path. Structured tool arguments and results remain internal,
failures are summarized safely, and hidden reasoning, secrets, private request
details, and raw exception details are never published. The durable final
response or proposal preview is authoritative and is delivered before the
progress message reports successful completion.

When facts are missing or ambiguous, the model asks a natural follow-up through
the same harness. The owner's next private-channel message is ordinary input;
there is no keyword-based continuation router or fixed clarification counter.

Conversation-triggered study sessions are the current user-facing path for
placing new study time on the Notion calendar. Qwen reasons over the unsanitized,
free-form request and native tool results to select create, update, or archive
proposal tools; there is no keyword workflow for those operations. Deterministic code validates only authorization, typed
schemas, known owner-scoped targets, bounds, confirmation, idempotency, and
stale-write preconditions. A create request must resolve to exactly one synchronized course. The bot may ask for the
start time, duration, and whether multiple topics should be combined or
separate. For separate blocks such as `45 minutes each`, LifeAgent proposes
ordered, sequential blocks with no invented gap. The Discord proposal preview
is deterministic and Toronto-local; it shows every course, canonical
`Studying Block — <topic>` title, start, end, and duration before exact
confirmation. Confirmation creates Notion pages under the course's discovered
Assessments data source using only the discovered `Name` and `Date` property
IDs. The Date payload includes both `start` and `end`.

These study sessions do not use Google Calendar, Apple Calendar, Microsoft
Calendar, or a separate Notion Calendar API. They also do not auto-export
planner-generated PostgreSQL `StudyBlock` rows; the generated daily plan remains
separate from explicitly confirmed Notion assessment pages.

Only user-answerable ambiguity consumes the clarification budget. Ollama
unavailability, timeout, invalid structured output, database/Discord failure,
or host validation failure receives a bounded recovery response, leaves no
automatic continuation pending, and requires a new mention after recovery.

Multi-page Notion proposals are applied cautiously. Before writing, the proposal
is claimed into an `applying` state so duplicate confirmations cannot start a
second batch. Each ordered operation is journaled by proposal ID and ordinal
with bounded receipt data when it is definitely completed. If Notion accepts one
page and a later page fails or times out, LifeAgent must not replay the batch or
claim complete success; inspect the operation journal and the relevant course
Assessments calendar before deciding on manual repair.

Qwen maps misspellings, paraphrases, pronouns, and non-schema-compliant wording
onto the five typed creation capabilities: Quiz, Assignment, Tutorial, Lab, and
Studying Block. If the meaning remains materially ambiguous, Qwen asks one
bounded clarification and emits no mutation tool. The host never guesses or
re-parses the user's label; it accepts only a valid enum in structured output
and shows the canonicalized result in the confirmation preview.

Confirmation and rejection commands must match exactly, with no extra spaces or
arguments. Every other authorized private-channel message reaches the native
harness as free-form input; the model may answer directly, use tools, or ask a
natural follow-up. Proposal previews include the proposal ID, bounded typed
changes, expiry, and both exact commands. Replies from other users/channels and
bot-authored messages are ignored before their content is inspected or
persisted.

The default API does not mount the legacy HTTP check-in creator or the legacy
GitHub model-queue webhook. New academic proposals originate only in the native
Discord harness. Treat proposal IDs and confirmation events as sensitive
workflow data. Do not enable broad message collection or grant unnecessary
privileged intents.

Private intake artifacts outlive their proposal TTL by a safety margin and are
deleted only after the configured retention boundary. Readiness diagnostics
surface bounded counts for intake awaiting a target, pending proposals, active
seeding, uncertain writes, orphan uploads, and delayed indexing; they never
include URLs, PDF bytes, or extracted text.

Confirmed Notion writes remain unavailable until a host integration supplies a
reviewed `AcademicNotionWriter` containing the exact discovered page targets and
allowlisted property IDs. Courses-only read synchronization does not invent
write mappings. Proposal creation and rejection continue to work while that
writer is unavailable; confirmation returns an actionable fail-closed response.

For an ambiguous assessment label, LifeAgent first persists the request and
then sends `Quiz`, `Assignment`, `Tutorial`, `Lab`, `Studying Block`, and
`Ignore` buttons. Each type choice shows and confirms one exact canonical
title, such as `Tutorial — Chapter 4`. Before PATCHing, LifeAgent rechecks the
stored title and edited timestamp, then changes only the discovered title
property. A concurrent Notion edit cancels the write. Ignore records the
decision and performs no write; repeated interactions are harmless.

For study-session proposals, the same confirmation boundary creates new pages
in the relevant course's discovered Assessments data source. The writer sends
only the allowlisted `Name` title property and `Date` date property; no external
calendar, hidden automation database, or planner `StudyBlock` export is used.

## 5. Configure finance sources

Finance runs are read-only and are gated by the exact allowlist version in
`FINANCE_SOURCE_ALLOWLIST_VERSION`. The configured default is the public-first
`finance-sources-2026.09-v2` architecture. Its source records are seeded
disabled, so a newly migrated deployment remains fail-closed until every source
has a separate audited approval.

The public-first v2 allowlist is `finance-sources-2026.09-v2` and contains these
eight logical source IDs:

| Source ID | Coverage | Credential |
| --- | --- | --- |
| `defense_gov_rss` | Official Defense RSS at its current `war.gov` canonical host | none |
| `breaking_defense_public` | Fast reported defense discovery from the public WordPress endpoint | none |
| `eia_public_data` | Official EIA public data, bulk by default | none in `bulk`; optional `EIA_API_KEY` in `api` |
| `federal_register_energy` | Official Federal Register energy/regulatory documents | none |
| `sec_edgar` | Public SEC submissions and filing metadata | descriptive `SEC_USER_AGENT` only |
| `company_ir_registry` | Reviewed official issuer IR feeds | none |
| `issuer_etf_holdings` | Reviewed direct issuer ETF holdings files | none |
| `technology_official_feeds` | CISA KEV and reviewed official vendor security feeds | none |

Use these local defaults for the v2 public baseline:

```dotenv
FINANCE_SOURCE_ALLOWLIST_VERSION=finance-sources-2026.09-v2
SEC_USER_AGENT=LifeAgent/0.1 contact@example.com
FINANCE_EIA_MODE=bulk
EIA_API_KEY=
FINANCE_FEED_POLL_MINUTES=10
FINANCE_FEDERAL_REGISTER_POLL_MINUTES=60
FINANCE_EIA_BULK_POLL_MINUTES=720
FINANCE_EIA_API_POLL_MINUTES=60
FINANCE_ETF_POLL_MINUTES=1440
FINANCE_COLD_START_BACKFILL_HOURS=24
FINANCE_REGISTRY_MAX_FANOUT=20
FINANCE_BULK_MAX_PAYLOAD_BYTES=67108864
```

`FINANCE_EIA_MODE=api` is selected only at startup and requires `EIA_API_KEY`.
If API mode is selected without a key, startup fails closed. Bulk mode must work
with `EIA_API_KEY` empty.

These legacy v1 credentials are not part of normal setup. They remain listed
only because this rollout explicitly requires v1 rollback compatibility; keep
them empty unless that exceptional rollback is deliberately invoked after a
provider-terms review:

```dotenv
DVIDS_API_KEY=
ALPHA_VANTAGE_API_KEY=
BENZINGA_API_TOKEN=
FMP_API_KEY=
```

Approval remains separate from configuration: the database must contain exactly
eight records for the active allowlist version, and each source must be enabled
with `approved_at` and `approval_audit_id`. The v2 seed intentionally creates
disabled records with no individual approvals. Never substitute open web search
or a different source. Check the gate at:

```bash
curl http://127.0.0.1:8000/finance/sources | jq .
```

Do not add paid news credentials unless the provider has granted programmatic
access and permitted the intended retention and local-LLM use. A normal
website subscription is not an API license.

The initial v2 registries are deliberately small: SEC and company IR cover LMT
only, ETF holdings cover IVV only, and technology feeds begin with CISA KEV
only. Missing mappings should appear as attention diagnostics instead of causing
generic crawling or arbitrary URL fetches.

Roll out v2 in this order: deploy the code and migration with finance delivery
disabled, confirm all eight v2 records are present and disabled, validate
fixtures and permitted public smoke tests, record the endpoint/licence review,
approve sources individually through the audited procedure, and run one
delivery-disabled dry briefing. A scheduled Qwen finance briefing is
non-executable in the current authorized-private-channel runtime; adding it would
require a future architecture change, not a current runtime setting.

The explicitly requested legacy rollback does not downgrade or delete v2 audit
records. Switch
`FINANCE_SOURCE_ALLOWLIST_VERSION` back to `finance-sources-2026.09` and keep
the v1 credential settings available for that path.

## 6. Verify and operate

After every `.env` change, recreate the affected services and run:

```bash
docker compose ps
curl http://127.0.0.1:8000/health/ready | jq .
curl http://127.0.0.1:8000/finance/sources | jq '.enabled, .sources[] | {source_id, enabled, health}'
docker compose logs --tail=100 api
```

Use the console at `http://127.0.0.1:8000/` to inspect health and activity.
Useful recovery commands are:

```bash
docker compose restart api
docker compose logs --tail=200 api
docker compose down                 # stops containers; preserves named volumes
docker compose up -d --build postgres api
```

For model failures, inspect `docs/operations/runbooks/model-unavailable-slow.md`.
For queue, source, Notion, or delivery failures, use the runbooks in
`docs/operations/runbooks/`.

## Security checklist

- `.env` is local-only and has restrictive file permissions.
- Default database credentials have been replaced.
- Ollama is reachable only from the local machine/Docker network.
- Notion pages and Discord channels are shared only with the intended account.
- Discord has no Administrator permission.
- Finance source approvals match the configured allowlist version; v2 remains
  disabled until all eight records are individually approved.
- No API key, token, or private URL appears in a commit or log.
