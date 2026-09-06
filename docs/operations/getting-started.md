# LifeAgent operations guide

This guide takes a new user from a fresh checkout to a working local LifeAgent
stack. It assumes:

- macOS or Linux with Docker Desktop/Engine, Docker Compose, `curl`, and `jq`;
- Qwen is installed in Ollama on the host computer; and
- the repository root is the current directory.

LifeAgent is local-first: PostgreSQL and the application run in Compose, while
Ollama runs on the host. Keep all credentials in `.env` or a secret manager;
never commit them or paste them into logs, tickets, or chat.

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

Leave integrations blank until they are configured. Build and start the stack:

```bash
docker compose config --quiet
docker compose up -d --build
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

The API container runs migrations automatically. The three workers are named
`worker-code-review`, `worker-academic-planner`, and `worker-finance`.

## 2. Connect host Ollama to Docker

The application container cannot use `localhost` to reach a host process:
inside the container, `localhost` means the container itself. Use the Docker
host name instead:

```dotenv
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

### Make Ollama listen on the host interface

Check the model name and the host API first:

```bash
ollama list
curl http://127.0.0.1:11434/api/tags | jq '.models[] | {name, digest}'
```

Set `OLLAMA_MODEL` to the exact name printed by `ollama list`, for example:

```dotenv
OLLAMA_MODEL=qwen3-32gb:latest
OLLAMA_MODEL_DIGEST=
```

Clearing `OLLAMA_MODEL_DIGEST` skips digest pinning during first setup. Pin the
actual digest later after verifying the model, by copying the digest returned
from `/api/tags`.

If Ollama is managed by the desktop app, quit it and start a host listener from
a terminal:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Keep that terminal running. On macOS, an alternative is to set the launch
environment before restarting the Ollama app:

```bash
launchctl setenv OLLAMA_HOST 0.0.0.0:11434
```

Do not forward port 11434 through your router or expose it to the public
internet. Docker Compose maps `host.docker.internal` to the local host; the
application still uses the host-only API port.

Test the path from inside the API container:

```bash
docker compose exec api python -c "import httpx; r=httpx.get('http://host.docker.internal:11434/api/tags', timeout=5); print(r.status_code); print(r.text[:500])"
```

If this fails, verify Ollama is running, the model name matches, and that the
listener is bound to `0.0.0.0:11434` rather than only `127.0.0.1:11434`.
Restart the app after changing `.env`:

```bash
docker compose up -d --force-recreate api worker-code-review worker-academic-planner worker-finance
```

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

Standalone `Quiz`, standalone `Assignment`, and the explicit spelling
correction `Assigment` are classified deterministically. Labels such as
`Homework`, `Paper`, and `Test` remain unknown. Unknown labels are persisted
for Discord clarification and are not scheduled or sent to Qwen while pending.

If the Courses database is absent, inaccessible, or not shared with the
`LifeAgent` connection, the academic worker sends a Discord setup
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

The morning planner runs this sync before loading facts. For setup recovery or
an immediate refresh after fixing a template/share problem, run the same
idempotent boundary manually:

```bash
curl --fail -X POST http://127.0.0.1:8000/academic/sync
```

The response contains bounded counts and diagnostic codes, never raw Notion
response bodies. A `setup_required` response does not call Qwen, create a
schedule, or attempt a Notion write.

## 4. Configure Discord

Discord is used for allowlisted outbound briefings and alerts, plus an optional
outbound Gateway connection for academic clarification buttons.

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
DISCORD_ACADEMIC_GATEWAY_ENABLED=true
DISCORD_FINANCE_CHANNEL_ID=...
DISCORD_CODE_REVIEW_CHANNEL_ID=...
```

Only the listed Discord users can authorize a clarification. The Gateway is an
outbound WebSocket connection and does not expose a public port. Button
interactions do not require broad message collection or the privileged Message
Content intent.

Planner check-ins can also be submitted to the LifeAgent API and produce a
separate confirmation-gated proposal:

```bash
curl -H 'Content-Type: application/json' \
  -d '{"reply":"I finished the first study block"}' \
  http://127.0.0.1:8000/academic/checkin
```

Treat the returned proposal ID and confirmation event as sensitive workflow
data. Do not enable broad message collection or grant unnecessary privileged
intents.

For an ambiguous assessment label, LifeAgent first persists the request and
then sends `Quiz`, `Assignment`, and `Ignore` buttons. Quiz or Assignment shows
and confirms one exact title, such as `Quiz — Chapter 4`. Before PATCHing,
LifeAgent rechecks the stored title and edited timestamp, then changes only the
discovered title property. A concurrent Notion edit cancels the write. Ignore
records the decision and performs no write; repeated interactions are harmless.

## 5. Configure finance APIs

Finance runs are read-only and are gated by the exact allowlist version in
`FINANCE_SOURCE_ALLOWLIST_VERSION`. The current eight source IDs are:

| Source | Credential | Where to obtain it |
| --- | --- | --- |
| DVIDS | `DVIDS_API_KEY` | DVIDS/API account, if required by the selected endpoint |
| Breaking Defense | none | Public endpoint; no key in this repository |
| EIA Open Data | `EIA_API_KEY` | [EIA Open Data registration](https://www.eia.gov/opendata/register.php) |
| Federal Register Energy | none | Public Federal Register API |
| Alpha Vantage News and ETF | `ALPHA_VANTAGE_API_KEY` | [Alpha Vantage](https://www.alphavantage.co/support/#api-key) |
| Benzinga News | `BENZINGA_API_TOKEN` | Benzinga developer/account portal |
| Financial Modeling Prep ETF | `FMP_API_KEY` | [FMP developer portal](https://site.financialmodelingprep.com/developer/docs) |

The Alpha Vantage credential is used by two allowlisted source adapters, so it
is listed twice conceptually but configured once. Put keys in `.env`:

```dotenv
FINANCE_SOURCE_ALLOWLIST_VERSION=finance-sources-2026.09
DVIDS_API_KEY=...
EIA_API_KEY=...
ALPHA_VANTAGE_API_KEY=...
BENZINGA_API_TOKEN=...
FMP_API_KEY=...
```

A key is necessary but not sufficient: the database must also contain one
approved record for each of the eight source IDs, with the matching allowlist
version. The finance gate intentionally stays disabled if an approval record,
credential, parser, or source contract is missing. Never substitute open web
search or a different source. Check the gate at:

```bash
curl http://127.0.0.1:8000/finance/sources | jq .
```

Do not add paid news credentials unless the provider has granted programmatic
access and permitted the intended retention and local-LLM use. A normal
website subscription is not an API license.

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
docker compose restart worker-code-review worker-academic-planner worker-finance
docker compose logs --tail=200 worker-finance
docker compose down                 # stops containers; preserves named volumes
docker compose up -d --build
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
- Finance source approvals match `finance-sources-2026.09`.
- No API key, token, or private URL appears in a commit or log.
