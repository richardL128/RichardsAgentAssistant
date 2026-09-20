# LEARN bridge runbook

The LEARN integration is disabled by default. It uses a dedicated host-only Chromium
profile and a loopback HMAC-authenticated bridge. Do not copy browser cookies, storage
state, authorization headers, or Waterloo credentials into `.env`, Docker, logs, test
fixtures, or model prompts.

## Prerequisites

Create exactly one Notion course row named `Classes + Tutorials + Labs`. Its child
calendar must have the standard `Name` title and `Date` date properties plus exactly
one rich-text property named `LEARN Context`. LifeAgent owns only `LEARN Context`;
Quest/user workflows continue to own the title, date, and page body.

Install the repository environment and Playwright Chromium before first login:

```bash
uv sync --locked
.venv/bin/playwright install chromium
```

## Authenticate and prove feasibility

Open the dedicated headed profile and complete Waterloo SSO/MFA manually:

```bash
scripts/lifeagent_learn_bridge.sh login
```

The command never asks for or stores a Waterloo password. Close Chromium after LEARN
has loaded, then prove that the same profile survives a headless relaunch and exposes
courses, scheduled items, and announcements:

```bash
scripts/lifeagent_learn_bridge.sh verify
```

`verify` reports status and bounded counts only. If it reports `login_required`, leave
LEARN disabled and repeat manual login; do not add password or MFA automation.

## Install the host LaunchAgent

The installer builds a dedicated owner-only runtime under
`~/Library/Application Support/LifeAgent/learn-bridge/runtime`. The generated
LaunchAgent runs from that location because macOS privacy controls can deny background
services access to a checkout under `~/Desktop`. It does not depend on the Docker image
marker or reinstall the Discord/Ollama host runtime.

```bash
scripts/lifeagent_learn_bridge.sh install
scripts/lifeagent_learn_bridge.sh status
scripts/lifeagent_learn_bridge.sh health
```

The installer creates an owner-only profile and HMAC secret under
`~/Library/Application Support/LifeAgent/learn-bridge/`. The LaunchAgent listens only
on `127.0.0.1`. Running `login` while it is installed temporarily stops the bridge,
opens headed Chromium, and restarts the LaunchAgent when Chromium closes.

To remove the LaunchAgent without deleting its authenticated profile or secret:

```bash
scripts/lifeagent_learn_bridge.sh uninstall
```

## Enable the container-side client

Set the non-secret controls in `.env` only after both the feasibility check and Notion
schema check pass:

```dotenv
LEARN_BRIDGE_ENABLED=true
LEARN_BRIDGE_URL=http://host.docker.internal:8765
```

Pass the same host secret to Compose from the owner-only file without printing it or
writing it into `.env`:

```bash
LEARN_BRIDGE_HMAC_SECRET="$(<"$HOME/Library/Application Support/LifeAgent/learn-bridge/learn-bridge-hmac.key")" \
  docker compose --env-file .env up -d --no-build worker-academic-planner
```

Use the same environment handoff for any Compose command that recreates the worker.
The secret is intentionally separate from Discord, Notion, and other credentials.

## Validation and recovery

In the authorized private Discord channel, verify course search, scheduled-item lookup,
and announcement summaries. Announcement output must contain only Qwen-reviewed
summaries, grounded dates, and LEARN links—never raw announcement bodies.
When the owner asks to add a grounded LEARN date, the bot must show a proposal for
either a new reserved-calendar page or a `LEARN Context`-only enrichment and wait for
the exact `confirm <proposal_id>` response. Verify that no per-course database changes occur.

If the session expires, interactive LEARN refresh and proposal work becomes unavailable,
but the scheduled morning briefing continues to use only the reserved Notion calendar.
Reauthenticate with:

```bash
scripts/lifeagent_learn_bridge.sh login
```

Keep `LEARN_BRIDGE_ENABLED=false` if headless feasibility, the reserved Notion schema,
or the live Discord validation has not passed.
