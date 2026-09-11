# Runbook: Discord Cold Wake

## Purpose

The native `com.lifeagent.discord-wake` LaunchAgent is the sole Discord ingress.
It stays connected while the API is cold and can also recover after Docker is
stopped. An authorized private-channel message produces one immediate
acknowledgement, starts any unavailable fixed services (`postgres`, `api`, and
`worker-academic-planner`) with no build or pull, verifies the supervised Ollama
API, and submits an HMAC-signed reference to the loopback backend.

The wake path never stores raw Discord content or interaction tokens in its
host outbox, never builds an image, never pulls a model, and never sends a dummy
generation. The first real conversational request loads the configured model.

The native conversational agent allows up to 50 model turns per request. A
turn may contain multiple tool calls; this is not a token/context limit. The
agent can finish earlier, and reaching the limit produces an explicit failure
without applying accumulated Notion proposals. Changing this source limit
requires `deploy` to update the worker image and installed runtime together.

The native runtime is installed under
`~/Library/Application Support/LifeAgent/runtime`. Its source, Python environment,
Compose file and private configuration are independent of the development
checkout. LaunchAgents must not use the Desktop checkout as their working
directory or resolve scripts, Python dependencies, configuration or state there.
The development checkout remains the source used by `deploy`.

## Install or deploy

From the repository root, configure `.env`, including the Discord bot token,
application ID, private academic channel, authorized user IDs, and Message
Content intent flag. Then run:

```bash
scripts/lifeagent_host_runtime.sh deploy
scripts/lifeagent_host_runtime.sh status
```

`deploy` builds `lifeagent-app:local`, runs the migration/test preflight,
records its non-secret image ID, installs a runtime snapshot with a separate
Python environment, preserves the existing HMAC key and outbox, and installs
both LaunchAgents. It then keeps PostgreSQL and the academic worker resident for
the model-free morning schedule and stops only the API, so the next authorized
message exercises the cold API wake. Secrets are not embedded in either plist.

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

Treat `loaded` as registration only. The cold-wake path is ready only when the
native listeners are actively and durably running:

```bash
for label in com.lifeagent.discord-wake com.lifeagent.ollama; do
  launchctl print "gui/$UID/$label" | grep -E 'state =|pid =|last exit code =|runs =|successive crashes =|throttle interval ='
done
sleep 30
for label in com.lifeagent.discord-wake com.lifeagent.ollama; do
  launchctl print "gui/$UID/$label" | grep -E 'state =|pid =|last exit code =|runs =|successive crashes =|throttle interval ='
done
```

Use the results this way:

- `discord_wake_launchd=unloaded` or `ollama_launchd=unloaded`: the agent is not
  registered in the user launchd domain. Run `scripts/lifeagent_host_runtime.sh
  deploy`, or `scripts/lifeagent_host_runtime.sh install` only when the image
  marker is already current.
- `loaded` with no `state = running` and no live `pid`: launchd knows about the
  job, but it is inactive and is not listening. Kickstart the affected label
  with `launchctl kickstart -k "gui/$UID/<label>"` or rerun the documented
  deploy/install command, then recheck the two samples.
- `state = running` with a `pid` that remains present across both samples: the
  process is an active listener. For Discord this is the required Gateway
  listener. For Ollama, also require `scripts/ollama_qwen_status.sh` to report
  `ollama_api=reachable`.
- A changing `pid`, increasing `runs` count, `successive crashes`, or a nonzero
  `last exit code` means launchd is restarting the job and the listener is not
  durably ready, even if the status line says `loaded`. Inspect the matching
  stderr log, fix the safe diagnostic it reports, and redeploy or kickstart
  before retrying Discord.

For Ollama during a cold-wake test, `qwen_resident=no` is expected before the
authorized academic mention. It is not an Ollama listener failure when
`ollama_launchd=running`, `ollama_api=reachable`, `model_installed=yes`, and
`digest_match=yes` are present. It is a failure if the API is unreachable, the
model is missing, or the digest mismatches.

Then inspect the fixed Compose services and loopback API:

```bash
docker compose ps postgres api worker-academic-planner
curl --fail http://127.0.0.1:8000/health/live
```

Safe acknowledgement failures identify one of these operator actions:

- Docker timeout: open/check Docker Desktop and retry with a new message.
  Verify `docker info` succeeds; `docker desktop start` reporting "already
  running" does not establish engine readiness. A partially stopped Desktop
  with lingering helper processes may require opening the app manually.
- Deployment refresh required: run `scripts/lifeagent_host_runtime.sh deploy`.
- Compose unhealthy: inspect `docker compose logs --tail=200 api worker-academic-planner`.
- Ollama unavailable: run `scripts/ollama_qwen_status.sh` and inspect its LaunchAgent log.
- Handoff failure: check the API, worker, HMAC key permissions, and local outbox.

For Discord delivery failures, inspect bounded worker logs for HTTP 429 and
retry events. A worker job marked successful or a "Completed" progress edit
does not establish acceptance: verify that the user also received a final
answer. In particular, a retried model turn must not reuse an intermediate
message's delivery identity for its final answer.

The ID-only outbox and HMAC key live under the installed runtime's
`.artifacts/discord-wake/` with
restrictive permissions. Do not print or copy the HMAC key into `.env`, a plist,
logs, tickets, or chat.

When sharing diagnostics, include only non-secret command output such as
launchd state lines, bounded stderr excerpts, Compose service status, and health
probe status. Do not include Discord bot tokens, interaction tokens, HMAC
contents, `.env` dumps, raw Discord message bodies, or stack traces that contain
secret-bearing URLs or headers.

## Cold-wake acceptance test

1. Run `scripts/lifeagent_host_runtime.sh status` and confirm it reports
   `discord_wake_launchd=running`, then take the two `launchctl print` samples from
   Diagnose. The test may continue only when `com.lifeagent.discord-wake` is
   `state = running` with a live `pid` in both samples and no crash-loop signal.
   Also confirm `ollama_launchd=running` and `scripts/ollama_qwen_status.sh` reports
   the Ollama API reachable when this install is expected to supervise Ollama.
2. Stop only the API without unloading the native agents or disabling the
   academic morning scheduler:

   ```bash
   docker compose stop api
   scripts/ollama_qwen_unload.sh
   ```

   To test full Docker Desktop recovery separately, also quit Docker Desktop
   only after checking
   for other running projects and obtaining permission to interrupt them.
   Confirm `docker info` fails before sending the request. A timed-out stop
   with lingering processes is a separate partial-shutdown failure case, not
   a clean Desktop shutdown. Restore and verify any interrupted projects after
   the test; do not delete containers or volumes to recover.

3. Confirm Qwen is not resident with `scripts/ollama_qwen_status.sh`, while
   keeping the Ollama LaunchAgent running and the Ollama API reachable.
4. Verify in automated boundary tests that messages from unauthorized users,
   other channels, or bots produce no acknowledgement, handoff or model call.
   In the configured private channel, the authorized user's prose is a valid
   request even without a mention; it must not be used as a negative test.
5. From the authorized user in the configured private channel, send one bot
   mention with an academic request.
6. Confirm exactly one immediate acknowledgement appears with this text:

   ```text
   I’m waking up LifeAgent and Qwen. Please give me a little time to respond.
   ```

7. Confirm Docker Desktop and the three fixed Compose services become healthy,
   the acknowledgement is edited in place with semantic progress, and one
   separate answer, proposal, clarification, or bounded failure response arrives.
   A failure response is evidence of honest failure handling, not successful
   acceptance of the feature. Complete three user-selected natural-language
   prompts with correct answers before declaring recovery complete.
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
