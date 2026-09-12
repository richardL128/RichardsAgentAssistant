# Runbook: Model Unavailable Or Slow

## Symptom

An authorized private-channel Discord message has its existing wake acknowledgement edited to
the bounded unavailable response:

```text
Qwen is unavailable on this Mac; run scripts/ollama_qwen_start.sh and try again.
```

The operations console or `/health/ready` may also show `ollama=attention` with
a diagnostic such as `Ollama unavailable`, `configured Ollama model is not
installed`, or `configured Ollama model digest does not match`.

In the default runtime, Qwen-powered work starts from a message sent by an
authorized owner in the configured private Discord academic channel or from the
automatic morning briefing's bounded event-semantic phase. A bot mention is
optional for interactive work. Startup, health checks, and messages from other
users or channels must not load Qwen.

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
checks should not be treated as permission to start any model workflow beyond
the configured private-channel conversation and morning event-semantic paths.

For a morning run, inspect `calendar_briefing.semantic_interpretation` and
`calendar_briefing.semantic_validation` rows in `run_steps`. If Ollama cannot
become ready inside the configured event/total deadlines, the briefing should
still contain trusted event titles and dates, omit unverified semantic prose,
and show one aggregate availability condition.

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

The first authorized message creates exactly one Discord acknowledgement:

```text
I’m waking up LifeAgent and Qwen. Please give me a little time to respond.
```

The host and backend edit that same message through Discord REST as they observe
startup, runtime checking/readiness, model turns, generic allowlisted tool
activity, reply preparation, and terminal stages. The Gateway WebSocket remains
the inbound event/session transport; it does not stream model progress. An
unchanged host or model await may update at 8, 20, and 45 elapsed seconds and
then every 30 seconds, capped at six host-wake edits and twelve backend edits
per inbound request. These pulses replace the active line rather than
accumulating repeated history. They are best-effort semantic liveness, not
token output, completion percentages, or hidden reasoning. Runtime-ready proves
only API/model availability. The separate
durable answer, proposal preview, clarification, or failure response remains
authoritative and precedes a successful terminal status; a progress `PATCH`
failure must not suppress it.

That first real structured request loads the model and can be noticeably slower
than later warm requests. `OLLAMA_MODEL_KEEP_ALIVE_SECONDS=300` keeps Qwen
resident across one bounded planner loop and lets Ollama unload it after about
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
3. Verify an unauthorized user, another channel, or a bot-authored message does
   not wake Qwen. Do not use an authorized unmentioned message as the negative
   case; mentions are optional in the configured private channel.
4. Send one planner request from an authorized user, with or without a mention.
5. Confirm one progress message appears, advances in place, and Qwen appears
   resident. For a deliberately slow turn, confirm the 8-second and 20-second
   liveness edits replace the active line without exposing request or tool data.
6. Confirm the bounded loop sends one separate final response before progress
   becomes terminal. For an ambiguous request, reply with another verified
   authorized reply and confirm the pending clarification continues according
   to the active workflow contract. No more than three model attempts may
   occur where that contract applies.
   Operational failures must not consume a clarification attempt.
7. Confirm Qwen unloads after the configured 300-second idle interval.

No Discord alert should be created for an Ollama-only attention state. Alerts
remain limited to the documented shared-services failure policy and must not
include model outputs, prompts, Discord message bodies, URLs, stack traces, or
secret values.
