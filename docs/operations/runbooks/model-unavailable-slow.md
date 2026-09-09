# Runbook: Model Unavailable Or Slow

## Symptom

An authorized Discord mention has its existing wake acknowledgement edited to
the bounded unavailable response:

```text
Qwen is unavailable on this Mac; run scripts/ollama_qwen_start.sh and try again.
```

The operations console or `/health/ready` may also show `ollama=attention` with
a diagnostic such as `Ollama unavailable`, `configured Ollama model is not
installed`, or `configured Ollama model digest does not match`.

In the default runtime, Qwen-powered work starts only from an authorized bot
mention in the configured private Discord academic channel. Replies to an open
academic clarification also require a verified mention. Startup, health checks,
all unmentioned prose, and every schedule must not load Qwen.

## Diagnosis

Check the API readiness endpoint:

```bash
curl http://127.0.0.1:8000/health/ready | jq .
```

The readiness probe checks Ollama's non-secret `/api/tags` endpoint only. It can
observe whether the host API is reachable and whether the configured model and
optional digest are present; it must not send a generation request or load Qwen.

Check the host-managed Ollama state from the repository root:

```bash
scripts/ollama_qwen_status.sh
```

The status script reports, with meaningful exit codes, whether the local Ollama
API is reachable, whether the configured model is installed, whether the
optional digest matches, and whether Qwen is currently resident according to
`/api/ps`. It does not print prompts, Discord messages, unrelated model
metadata, or secrets.

Verify Docker can reach the host endpoint without loading Qwen:

```bash
docker compose exec api python -c "import httpx; r=httpx.get('http://host.docker.internal:11434/api/tags', timeout=5); print(r.status_code); print(r.text[:500])"
```

Use queue inspection only to diagnose operational backlog. Queue and health
checks should not be treated as permission to start scheduled model workflows in
the current runtime.

## Fix

Start or verify the host Ollama server with the fixed operator script:

```bash
scripts/ollama_qwen_start.sh
```

The script is idempotent. It probes `http://127.0.0.1:11434/api/tags`, then
loads or kickstarts the fixed `com.lifeagent.ollama` LaunchAgent when needed.
It writes non-secret diagnostics under `~/Library/Logs/LifeAgent` and exits
nonzero if readiness does not succeed within the configured bounded timeout.
There is no `nohup` or PID-file fallback.

For Docker reachability, Ollama uses `OLLAMA_HOST=0.0.0.0:11434`. That bind can
be reachable from the host network/LAN unless protected. Keep the Mac on a
trusted network, protect the port with the macOS firewall, never add a router
port-forward, and never publish port 11434 from Compose.

If the model is missing, the script still must not pull it by default. Pull only
when the operator explicitly requests the large download:

```bash
scripts/ollama_qwen_start.sh --pull
```

If the digest does not match, first confirm the configured `OLLAMA_MODEL` and
the installed model returned by:

```bash
scripts/ollama_qwen_status.sh
```

If the new digest is intentional, update `OLLAMA_MODEL_DIGEST` in the deployment
environment after benchmarking the model. If it is not intentional, reinstall or
pull the expected model with `--pull`.

After changing `.env`, deploy the shared image and restart the native runtime:

```bash
scripts/lifeagent_host_runtime.sh deploy
```

Do not give the API container a Docker socket, SSH key, or environment command
that can start host processes. Only the native coordinator runs the fixed
Docker, Compose, launchctl, and Ollama command arrays.

## Slow Cold Starts

Qwen is host-managed and lazily loaded. The host Ollama server may already be
running while the Qwen model is absent from `ollama ps`; this is expected after
idle unload or a manual unload.

The first authorized mention creates exactly one Discord acknowledgement:

```text
I’m waking up LifeAgent and Qwen. Please give me a little time to respond.
```

The host edits that same message as it observes model, catalog lookup,
validation, and terminal stages. These updates are best-effort semantic status,
not token output or hidden reasoning; the separate durable proposal,
clarification, or failure response remains authoritative. A progress PATCH
failure must not suppress that response.

That first real structured request loads the model and can be noticeably slower
than later warm requests. `OLLAMA_MODEL_KEEP_ALIVE_SECONDS=300` keeps Qwen
resident across one bounded academic loop and lets Ollama unload it after about
five idle minutes. To unload immediately without deleting model files:

```bash
scripts/ollama_qwen_unload.sh
```

## Gateway and local handoff

The configured inbound Discord path is the sole Gateway connection in the
native `com.lifeagent.discord-wake` LaunchAgent. The Mac needs no public URL.
After Docker is ready, the daemon sends an HMAC-signed, reference-only request
to the backend's loopback endpoint. The backend refetches the Discord message,
validates its channel, author, timestamp, mention metadata, and acknowledgement,
then durably queues the row ID.

## Verify

Confirm the host server and configured model:

```bash
scripts/ollama_qwen_status.sh
curl http://127.0.0.1:8000/health/ready | jq '.status'
```

For live validation on the Mac:

1. Run `scripts/ollama_qwen_unload.sh`.
2. Confirm `scripts/ollama_qwen_status.sh` reports Qwen is not resident.
3. Send an unmentioned Discord message, including as a reply to a pending
   clarification, and confirm Qwen remains unloaded.
4. Mention the bot once from an authorized user.
5. Confirm one progress message appears, advances in place, and Qwen appears
   resident.
6. Confirm the bounded loop sends one separate final response. For an ambiguous
   request, reply with another verified mention and confirm the pending
   clarification continues. No more than three model attempts may occur.
   Operational failures must not consume a clarification attempt.
7. Confirm Qwen unloads after the configured 300-second idle interval.

No Discord alert should be created for an Ollama-only attention state. Alerts
remain limited to the documented shared-services failure policy and must not
include model outputs, prompts, Discord message bodies, URLs, stack traces, or
secret values.
