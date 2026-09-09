# Runbook: Discord Cold Wake

## Purpose

The native `com.lifeagent.discord-wake` LaunchAgent is the sole Discord ingress.
It stays connected while Docker is stopped. An authorized mention produces one
immediate acknowledgement, wakes Docker Desktop, starts only `postgres`, `api`,
and `worker-academic-planner` with no build or pull, verifies the supervised
Ollama API, and submits an HMAC-signed reference to the loopback backend.

The wake path never stores raw Discord content or interaction tokens in its
host outbox, never builds an image, never pulls a model, and never sends a dummy
generation. The first real structured academic request loads Qwen.

## Install or deploy

From the repository root, configure `.env`, including the Discord bot token,
application ID, private academic channel, authorized user IDs, and Message
Content intent flag. Then run:

```bash
scripts/lifeagent_host_runtime.sh deploy
scripts/lifeagent_host_runtime.sh status
```

`deploy` builds `lifeagent-app:local`, runs the migration/test preflight,
records its non-secret image ID, creates a separate mode-`0600` HMAC key, and
installs both LaunchAgents. It then stops the API and academic worker so the
next authorized mention exercises the cold wake. Secrets are not embedded in
either plist.

Use `install` only when the image and deployment marker are already current:

```bash
scripts/lifeagent_host_runtime.sh install
```

## Diagnose

Check native supervision and logs first:

```bash
scripts/lifeagent_host_runtime.sh status
launchctl print "gui/$UID/com.lifeagent.discord-wake"
tail -n 100 "$HOME/Library/Logs/LifeAgent/discord-wake.stderr.log"
tail -n 100 "$HOME/Library/Logs/LifeAgent/ollama.stderr.log"
```

Then inspect the fixed Compose services and loopback API:

```bash
docker compose ps postgres api worker-academic-planner
curl --fail http://127.0.0.1:8000/health/live
```

Safe acknowledgement failures identify one of these operator actions:

- Docker timeout: open/check Docker Desktop and retry with a new mention.
- Deployment refresh required: run `scripts/lifeagent_host_runtime.sh deploy`.
- Compose unhealthy: inspect `docker compose logs --tail=200 api worker-academic-planner`.
- Ollama unavailable: run `scripts/ollama_qwen_status.sh` and inspect its LaunchAgent log.
- Handoff failure: check the API, worker, HMAC key permissions, and local outbox.

The ID-only outbox and HMAC key live under `.artifacts/discord-wake/` with
restrictive permissions. Do not print or copy the HMAC key into `.env`, a plist,
logs, tickets, or chat.

## Cold-wake acceptance test

1. Run `scripts/lifeagent_host_runtime.sh status` and confirm the Discord wake
   LaunchAgent is loaded.
2. Stop the Compose services without unloading the native agents:

   ```bash
   docker compose stop api worker-academic-planner postgres
   scripts/ollama_qwen_unload.sh
   ```

3. Confirm Qwen is not resident with `scripts/ollama_qwen_status.sh`.
4. Send unrelated prose without a bot mention and without an open clarification.
   Confirm there is no bot reply and no inbound content artifact. The native
   host may wake the backend to perform the owner-scoped session check, but Qwen
   must not be invoked.
5. From the authorized user in the configured private channel, send one bot
   mention with an academic request.
6. Confirm exactly one immediate acknowledgement appears with this text:

   ```text
   I’m waking up LifeAgent and Qwen. Please give me a little time to respond.
   ```

7. Confirm Docker Desktop and the three fixed Compose services become healthy,
   the acknowledgement is edited in place with semantic progress, and one
   separate proposal, clarification, or bounded failure response arrives.
8. Confirm `scripts/ollama_qwen_status.sh` reports Qwen resident only after the
   real request begins, then unloaded after the configured idle interval.

Also test one exact `confirm <proposal-uuid>` or `reject <proposal-uuid>` and one
clarification button. They may wake the backend but must remain model-free.
While an owner-scoped clarification is open, also reply once without mentioning
the bot and verify that acknowledgement/progress begins only after the backend
accepts it as that continuation.

## Uninstall

```bash
scripts/lifeagent_host_runtime.sh uninstall
```

This stops and removes both LaunchAgent plists. It intentionally leaves the
deployed image marker, HMAC key, and short-retention outbox in place. Review and
remove those files separately only when decommissioning the installation.

## Network limitation

Ollama binds to `0.0.0.0:11434` so Docker Desktop can reach it. That can expose
the API to the local network unless the macOS firewall blocks it. Use a trusted
network, enable the firewall, never forward port 11434 on a router, and never
publish it from Compose. The LifeAgent API and handoff remain bound to
`127.0.0.1`.
