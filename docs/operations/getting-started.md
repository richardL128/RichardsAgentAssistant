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

At minimum, set a non-default database password and operations-console
credentials:

```dotenv
POSTGRES_PASSWORD=choose-a-local-password
OPS_CONSOLE_USERNAME=admin
OPS_CONSOLE_PASSWORD=choose-an-operations-password
```

Leave integrations blank until they are configured. Build and start the stack:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Open `http://127.0.0.1:8000/` for the operations console. Check readiness with:

```bash
curl --fail http://127.0.0.1:8000/health/ready | jq .
```

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

LifeAgent uses a Notion internal connection with the three database IDs below.
It does not use a user's Notion password or browser cookie.

1. Open the [Notion integrations page](https://www.notion.so/profile/integrations).
2. Create an internal integration named `LifeAgent`.
3. Grant read content. Grant update content only if confirmed planner changes
   are intended to write back to Notion.
4. Copy the integration token into `NOTION_TOKEN`.
5. Open each Courses, Assessments, and Study Blocks database in Notion, choose
   **Share**, and add the `LifeAgent` connection. The token alone does not grant
   page access.
6. Copy each database ID from its URL. It is the 32-character identifier before
   any query string. Set:

```dotenv
NOTION_TOKEN=secret-or-ntn-token
NOTION_COURSES_DATABASE_ID=...
NOTION_ASSESSMENTS_DATABASE_ID=...
NOTION_STUDY_BLOCKS_DATABASE_ID=...
```

The current application expects database IDs with these variable names; do not
rename them to `*_DATA_SOURCE_ID` without changing the application code.
Property mappings must also be supplied to the planner integration and should
be matched by stable Notion property IDs, not just display names.

Notion writes are confirmation-gated. A planner proposal must be explicitly
confirmed before a page is changed. In the current app, the Notion writer is
fail-closed unless it has been injected/configured, so a token by itself does
not silently enable writes.

## 4. Configure Discord

Discord is used for allowlisted outbound briefings and alerts.

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
DISCORD_FINANCE_CHANNEL_ID=...
DISCORD_CODE_REVIEW_CHANNEL_ID=...
```

The current Compose stack does not start a Discord Gateway listener. Discord
delivery is outbound through the bot API. Planner check-ins are submitted to
the LifeAgent API and produce a proposal:

```bash
curl -H 'Content-Type: application/json' \
  -d '{"reply":"I finished the first study block"}' \
  http://127.0.0.1:8000/academic/checkin
```

Treat the returned proposal ID and confirmation event as sensitive workflow
data. Do not enable broad message collection or grant unnecessary privileged
intents.

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
