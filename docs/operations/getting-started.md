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
event interpretation plus category composition in the automatic morning
briefing. The native
macOS Discord wake LaunchAgent remains connected while the API is cold,
acknowledges an allowlisted owner's message, starts the fixed local services as
needed, and submits a signed reference to the backend. PostgreSQL and the
planner worker stay resident so scheduled work does not depend on an inbound
Discord message. Health checks, startup, and messages outside the channel or
owner allowlist must not load Qwen.

The morning agenda refreshes Notion, selects course work due today and over the
following seven days plus Jobs, misc, and reserved schedule events that overlap
today, validates Qwen-produced semantics, and sends exactly four persisted,
idempotent Discord embeds. A separate nightly schedule sends one reflection
prompt to the configured proactive owner and opens a durable conversation; it
does not load Qwen until the owner replies through the authorized channel.

Waterloo LEARN support is optional and disabled by default. It uses a separate
host-only browser profile and LaunchAgent; follow the
[LEARN bridge runbook](runbooks/learn-bridge.md) before enabling it. A LEARN
failure never changes the morning source of truth: the scheduled briefing reads
only the freshly synchronized reserved Notion calendar, not LEARN directly.

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
assessment-material ingestion, the combined morning notification, and the
nightly reflection prompt. The morning path may call the configured Qwen model
for event-local semantics and the bounded four-category composer/critic;
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

Configure the semantic/reasoning and embedding roles separately. The accepted
semantic model is `qwen3:14b` at the measured 32K profile; the embedder remains
`qwen3-embedding:4b` at 1,024 dimensions:

```dotenv
OLLAMA_MODEL=qwen3:14b
OLLAMA_MODEL_DIGEST=bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8
OLLAMA_MAX_CONCURRENCY=1
OLLAMA_NUM_CTX=32768
OLLAMA_NUM_BATCH=32
OLLAMA_MAX_INPUT_TOKENS=26624
OLLAMA_MAX_OUTPUT_TOKENS=2048
OLLAMA_CONTEXT_RESERVE_TOKENS=4096
OLLAMA_TIMEOUT_SECONDS=300
OLLAMA_REASONING=false
OLLAMA_STRUCTURED_OUTPUT_TRANSPORT=json_schema
CONVERSATION_COMPACTION_TRIGGER_TOKENS=19968
CONVERSATION_COMPACTION_TARGET_TOKENS=14336
CONVERSATION_RECENT_TAIL_MAX_TOKENS=8192
CONVERSATION_COMPACTION_MAX_OUTPUT_TOKENS=2048
EMBEDDING_MODEL=qwen3-embedding:4b
EMBEDDING_MODEL_DIGEST=
EMBEDDING_DIMENSIONS=1024
EMBEDDING_MODEL_KEEP_ALIVE_SECONDS=300
MODEL_TRIGGER_MODE=authorized_discord_channel
OLLAMA_MODEL_KEEP_ALIVE_SECONDS=300
OLLAMA_STARTUP_TIMEOUT_SECONDS=30
```

`MODEL_TRIGGER_MODE` keeps interactive model requests closed to the authorized
private Discord channel. Study-plan, code-review, and finance Qwen schedules are
not configured. The separate academic morning schedule is host-controlled and
uses the same configured model only for bounded event semantics.

Install the exact model tag and verify the digest before treating the setup as
ready:

```bash
ollama pull qwen3:14b
curl --fail http://127.0.0.1:11434/api/tags \
  | jq -r '.models[] | select(.name == "qwen3:14b") | "\(.name) \(.digest)"'
```

The printed digest for `qwen3:14b` must be
`bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8` before it
is pinned in `.env`. Clearing a digest skips that role's digest pin during
first setup only; pin it after verifying the installed model in `/api/tags`.
Do not configure a former semantic model as a fallback, alternate runtime path,
or rollback model. A digest mismatch should stop readiness until the installed
tag or `.env` is corrected.

The 14B model performs semantic decisions, structured responses, native tool
selection, and event-local morning semantics. The 4B embedding model produces
normalized 1,024-dimensional vectors for assessment materials, durable academic
memory, and generic owner-scoped preference/fact retrieval. Generic user memory
remains separate from academic learning-focus memory and is activated only by
explicit owner management requests.

The accepted context invariant is
`26624 + 2048 + 4096 = 32768`. Context assembly itself is always enabled: full
transcripts remain durable, while model calls use validated summaries, a
bounded recent tail, relevant active owner memory, and current tool-loop state.
The compaction profile is `19968` trigger tokens, `14336` target tokens,
`8192` recent-tail tokens, and `2048` compaction-output tokens.

Retain the accepted hardware benchmark artifact under `docs/benchmarks/` and
rerun the benchmark after changing model, digest, context, timeout, reasoning,
transport, concurrency, or host memory assumptions:

```bash
PYTHONPATH=. .venv/bin/python scripts/phase1_benchmark.py \
  --model qwen3:14b \
  --expected-digest bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8 \
  --num-ctx 32768 \
  --num-batch 32 \
  --max-output-tokens 2048 \
  --timeout-seconds 300 \
  --structured-output-transport json_schema \
  --output docs/benchmarks/phase1-32k-qwen3-14b-m2max.candidate.json
```

Never overwrite the accepted artifact with a rerun. Inspect every candidate
evaluation and replace the accepted artifact only after all gates and the
native-tool soak pass again.

If the benchmark or live run shows timeouts, less than 10% host free memory,
more than 1 GiB steady swap growth, or model concurrency above one, stop and
reduce competing host load before retrying. Do not lower the documented profile
or re-enable a former semantic model to mask memory pressure.

Install or verify the supervised Ollama LaunchAgent with the fixed script:

```bash
scripts/ollama_qwen_start.sh
```

The script loads or kickstarts `com.lifeagent.ollama`, checks both configured
model roles and optional digests, verifies the embedding capability and vector
dimension, and exits nonzero with a bounded diagnostic if readiness fails. Run
`scripts/ollama_qwen_status.sh` after startup to confirm the configured tag,
digest, embedding dimension, and residency state. The start script does not use
`nohup` or a PID file and never pulls a model unless the operator explicitly
opts in:

```bash
scripts/ollama_qwen_start.sh --pull
```

After the embedding model is installed and its digest is pinned, migrate and
inspect the required vector rebuild without writing any vectors:

```bash
docker compose --env-file .env run --rm api alembic upgrade head
.venv/bin/python -m app.agents.academic_planner.material_embedding_backfill \
  --batch-size 64 --max-batches 1
```

Apply the bounded, resumable backfill only after that dry run is correct:

```bash
.venv/bin/python -m app.agents.academic_planner.material_embedding_backfill \
  --apply --batch-size 64 --max-batches 10 --bootstrap-empty-corpus
```

The command prints counts only. It does not print material text, reflection
text, vectors, URLs, or owner identifiers. Repeat it until `remaining_count` is
zero, then deploy through `scripts/lifeagent_host_runtime.sh deploy`.
Use that deploy command as the canonical rollout after model configuration
changes; an ad hoc `docker compose up` is useful for local diagnosis but is not
deployment completion for the native runtime.

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

The academic planner accepts one top-level Courses database ID. Each ordinary
course is a row/page in that database and owns one seeded inline Assessments
database. Calendar views are presentation only: LifeAgent discovers and queries
the underlying database and data source. Reserved `Jobs` and `misc` rows live in
the same top-level Courses database; do not configure separate Notion IDs for
them.

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

Do not configure child calendar or data-source IDs; LifeAgent discovers them
from the top-level Courses database.

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

### Misc task structure

Create exactly one active top-level Courses row/page titled `misc`. The title is
reserved only when its normalized value is exactly `misc`; names such as
`Miscellaneous`, `Personal`, or `Tasks` are ordinary course titles and are not
used for the misc route. Do not paste a Notion page ID, data-source ID, or
calendar-view ID for this row anywhere in `.env`.

Inside the `misc` page, add the same seeded inline child database used by
courses, named `Assessments` or `Assessment Calendar`. It requires exactly one
title property named `Name` and one date property named `Date`; optional
properties and page-body notes are allowed, but not required. LifeAgent
discovers this child data source from the reserved row, then writes only the
discovered `Name` and `Date` properties after confirmation.

General timed to-dos such as `scrub toilets @6 pm tdy` are routed semantically
to the dedicated `create_misc_task` proposal tool. They must not be forced into
an academic course or the `Jobs` career workflow. The Discord preview shows the
canonical `Task — <title>` entry and the Toronto-local due time, and no Notion
change is made until the owner sends the exact `confirm <proposal-id>` command.
Misc deadlines remain ordinary calendar to-dos.

If the `misc` row is missing, duplicated, inaccessible, missing its seeded child
database, or missing the required `Name`/`Date` properties, LifeAgent fails
closed with an actionable setup message. It does not guess a target calendar or
fall back to a hardcoded Notion identifier.

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

Explicit assessment labels `Quiz`, `Assignment`, `Tutorial`, and `Lab` remain
available. Other natural titles are ordinary events. A request containing
conflicting explicit assessment labels (for example, `quiz assignment`) asks
for clarification; `Event` preserves it as an ordinary calendar item.

Confirmed assessment creations use canonical titles such as `Quiz — <title>`.
Confirmed course events preserve their exact natural title and use a Notion
Date range with both `start` and `end`. Study intent is inferred semantically
from the title plus legitimate event/course context; no prefix or keyword table
classifies it.

Personal/general timed tasks are not part of that academic type allowlist. Qwen
must select the dedicated `create_misc_task` mutation only when the synchronized
catalog contains exactly one valid reserved `misc` row with a valid seeded
Assessments/Assessment Calendar child database. The host canonicalizes these
titles as `Task — <title>` and rejects past due times before any proposal can be
confirmed.

If the Courses database is absent, inaccessible, or not shared with the
`LifeAgent` connection, the academic workflow sends a Discord setup
reminder instead of calling the model or attempting a Notion write. It will do
the same when a course or reserved `misc` page is missing the seeded
`Assessments`/`Assessment Calendar` child database or that calendar lacks its
required `Name` title or `Date` date property. The message will direct the user
back to this setup section and confirm that no Notion changes were made.

Configuration reminders will be deduplicated so an unchanged problem produces
at most one reminder per day. When only some course pages are misconfigured,
the reminder will summarize those courses while correctly configured courses
continue syncing. If Discord is unavailable, the same actionable, non-secret
condition remains visible in persisted health. A successful discovery clears
the active reminder condition.

Academic sync and deterministic in-window event selection remain model-free.
The automatic morning notification may call Qwen after host code selects those
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
ACADEMIC_END_OF_DAY_SCHEDULE=21:00
ACADEMIC_END_OF_DAY_CATCHUP_GRACE_MINUTES=30
```

`ACADEMIC_MORNING_SCHEDULE` is interpreted in `APP_TIMEZONE`. The default
30-minute catch-up grace lets the worker recover from a short startup delay; it
must not send a stale morning notification after that deadline. Each scheduled
period records one run with agent `academic_morning_notification`, schedule
`academic-morning`, and idempotency key
`academic-morning:YYYY-MM-DD:HHMM:v1`. The four-embed manifest uses category
keys `planner-morning-four-v3:YYYY-MM-DD:HHMM:v1:<category>:v1`, where category
is `courses`, `jobs`, `misc`, or `schedule`. Embed titles are at most 256
characters and descriptions at most 4,096 characters. The persisted manifest
lets retries skip categories already delivered.

Before sending a normal morning agenda, the job completes the academic sync and
then the Jobs/Interviews sync. Discord receives four embeds in the fixed order
Courses, Jobs, Misc, and Classes + Tutorials + Labs. A category failure is
disclosed in that category's embed and does not hide independently fresh
categories. Missing sharing, stale data, malformed reserved calendars, or
Discord delivery uncertainty remains fail-closed. Semantic model or critic
failure produces a visible facts-only category using trusted Notion metadata.

### Nightly academic check-in

Set one authorized Discord owner as the proactive recipient:

```dotenv
DISCORD_ACADEMIC_PROACTIVE_USER_ID=123456789012345678
ACADEMIC_END_OF_DAY_SCHEDULE=21:00
ACADEMIC_END_OF_DAY_CATCHUP_GRACE_MINUTES=30
```

The proactive user ID must also appear in
`DISCORD_ACADEMIC_AUTHORIZED_USER_IDS`. At the configured Toronto-local time,
the academic worker records one `academic_nightly_checkin` run, sends an
idempotent reflection prompt, and opens an artifact-backed conversation for at
most the configured session TTL. If another conversation is already open, the
job retries only inside the catch-up window. A late occurrence does not send a
stale prompt.

The owner's reply follows the normal private-channel model path. Study-related
reflections may update academic learning-focus memory, and the assistant may
prepare calendar proposals, but every Notion write still requires the displayed
exact confirmation command. `skip`, `skip tonight`, `skip this check-in`, or
`skip this checkin` closes that night's conversation without a write. Generic
personal memory still requires an explicit remember, correct, or forget request.

Operational health is persisted as `academic_end_of_day`. Use the
[nightly check-in runbook](runbooks/academic-nightly-checkin.md) for diagnosis.

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
DISCORD_ACADEMIC_PROACTIVE_USER_ID=123456789012345678
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
academic worker running for the morning and nightly schedules, and stops only
the API so the next authorized message exercises the cold API wake path. A
Discord-triggered wake uses `--no-build
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
the typed `emit_conversation_response` lifecycle tool. The owner's next
private-channel message resumes the same durable owner/channel session with the
original request, assistant messages, native tool calls and matching results,
private provider reasoning, prior clarification, and a host-trusted tool-state
checkpoint in their original order. Raw content remains in private immutable
artifacts; database rows and readiness diagnostics expose only metadata and
artifact keys. There is no keyword or punctuation-based continuation router.
Exact `cancel`, `start over`, and `never mind` controls close the open session.

The worker reuses one lazily initialized Discord service and gateway, while
Ollama `keep_alive` only controls model-weight residency. Session continuity is
restart-safe and does not keep a model request or database transaction open
while waiting for the owner. An expired or corrupt session fails closed and asks
for a complete resend. If the pinned active transcript exceeds the configured
input capacity, LifeAgent preserves it and reports an actionable capacity
failure instead of dropping or summarizing prior evidence.

Conversation-triggered course events are the user-facing path for placing new
academic time on the Notion calendar. Misc tasks are the equivalent
path for placing personal/general timed to-dos on the reserved `misc`
Assessments calendar. Qwen reasons over the unsanitized, free-form request and
native tool results to select create, update, archive, `create_course_event`, or `create_misc_task`
proposal tools; there is no keyword workflow for those operations.
Deterministic code validates only authorization, typed schemas, known
owner-scoped targets, bounds, confirmation, idempotency, and stale-write
preconditions. A course create request must resolve to exactly one synchronized
course; a misc task must resolve to the unique valid reserved `misc` row. The
bot may ask for the start time, due time, duration, and whether multiple topics
should be combined or separate. For separate sessions such as `45 minutes each`,
LifeAgent proposes ordered, sequential events with no invented gap. The Discord
proposal preview is deterministic and Toronto-local; it shows every target,
the exact natural course-event title or `Task — <title>`, start or due time,
and duration when applicable before exact confirmation. Confirmation creates
Notion pages under the target's discovered Assessments data source using only
the discovered `Name` and `Date` property IDs. Timed course-event Date payloads
include both `start` and `end`; misc tasks use the selected future due time.

These course events do not use Google Calendar, Apple Calendar, Microsoft
Calendar, or a separate Notion Calendar API. Misc tasks follow
the same Notion-only confirmation boundary and do not use an external calendar.

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
onto explicit assessment types or an ordinary course event. If the meaning
remains materially ambiguous, Qwen asks one bounded clarification and emits no
mutation tool. The host never guesses study intent from a title; it accepts only
schema-valid tools and independently validates study intent semantically.

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

When Notion is configured, the Discord service constructs a reviewed
`AcademicNotionWriter` from the connector and synchronized target store. It
still writes only exact discovered page targets and allowlisted property IDs;
read synchronization never invents mappings. If the connector or a required
mapping is unavailable, proposal creation and rejection continue to work while
confirmation returns an actionable fail-closed response.

For an ambiguous assessment label, LifeAgent first persists the request and
then sends `Quiz`, `Assignment`, `Tutorial`, `Lab`, `Event`, and `Ignore`
buttons. Each type choice shows and confirms one exact canonical
title, such as `Tutorial — Chapter 4`. Before PATCHing, LifeAgent rechecks the
stored title and edited timestamp, then changes only the discovered title
property. A concurrent Notion edit cancels the write. Ignore records the
decision and performs no write; repeated interactions are harmless.

For course-event proposals, the same confirmation boundary creates new pages
in the relevant course's discovered Assessments data source. The writer sends
only the allowlisted `Name` title property and `Date` date property; no external
calendar or hidden automation database is used.
For misc-task proposals, the same boundary creates pages only under the unique
reserved `misc` Assessments data source after rechecking that the row and
properties are still valid.

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
