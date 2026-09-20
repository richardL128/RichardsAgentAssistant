# Runbook: Model Unavailable Or Slow

## Symptom

An authorized private-channel Discord message has its existing wake acknowledgement edited to
the bounded unavailable response:

```text
Qwen is unavailable on this Mac; run scripts/ollama_qwen_start.sh and try again.
```

The operations console or `/health/ready` may also show `ollama=attention` or
`academic_embeddings=failed`. Embedding failure codes distinguish a missing
model, digest mismatch, unsupported capability, bad 1,024-dimensional probe,
timeout, and stale material/reflection vectors.

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

Reasoning readiness uses Ollama's non-secret model metadata without generating
text. Academic embedding readiness additionally checks `/api/show`, sends one
fixed non-private embedding probe, validates 1,024 finite values, and checks
counts of stale material and reflection vectors. It never sends user content as
part of health checking.

Check the host-managed Ollama state from the repository root:

```bash
scripts/ollama_qwen_status.sh
```

The status script reports, with meaningful exit codes, whether the local Ollama
API is reachable, whether both the semantic/reasoning and embedding models are
installed, whether optional digests match, whether the embedding role advertises
its capability, and whether either role is resident according to `/api/ps`. It
does not print prompts, Discord messages, unrelated model metadata, or secrets.

The default semantic model is `qwen3:14b`, pinned to digest
`bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`. The
embedding model remains `qwen3-embedding:4b` with `EMBEDDING_DIMENSIONS=1024`.
There is no configured fallback semantic model.

The default native context budget is `OLLAMA_NUM_CTX=32768`,
`OLLAMA_MAX_INPUT_TOKENS=26624`, `OLLAMA_MAX_OUTPUT_TOKENS=2048`, and
`OLLAMA_CONTEXT_RESERVE_TOKENS=4096`; the accepted runtime also uses
`OLLAMA_NUM_BATCH=32`, `OLLAMA_MAX_CONCURRENCY=1`, `OLLAMA_REASONING=false`,
and `OLLAMA_STRUCTURED_OUTPUT_TRANSPORT=json_schema`. Startup validates that
the input budget, output budget, and reserve fit inside the configured context
window before a model request is accepted.

Native conversations no longer replay the entire transcript into each model
call. The exact artifact-backed transcript remains canonical for audit and
restart recovery, while every model boundary receives a budgeted assembly of a
validated cumulative session summary, an adjacency-safe recent tail, relevant
active generic owner memories, and current tool-loop messages. Summary and
memory blocks are explicitly untrusted and are never checkpointed into the
canonical transcript. If summary validation or compaction fails, the session
pauses with its complete transcript intact; there is no full-replay fallback.

The `/health/ready` response also checks the metadata tables for durable native
conversation sessions, compactions, and generic owner memory. It reports only
schema presence; transcript, summary, memory, tool-result, reasoning, and
trusted checkpoint content never appears in health diagnostics.

Verify Docker can reach the host endpoint without loading Qwen:

```bash
docker compose exec api python -c "import httpx; r=httpx.get('http://host.docker.internal:11434/api/tags', timeout=5); print(r.status_code); print(r.text[:500])"
```

Use queue inspection only to diagnose operational backlog. Queue and health
checks should not be treated as permission to start any model workflow beyond
the configured private-channel conversation and morning event-semantic paths.

For a morning run, inspect ordinal-specific rows such as
`calendar_briefing.event_001.semantic_interpretation` and
`calendar_briefing.event_001.semantic_validation` in `run_steps`. If Ollama
cannot become ready inside the configured event/total deadlines, the briefing
should still contain trusted event titles and dates, omit unverified semantic
prose, and show one aggregate availability condition.

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

If either model is missing, the script still must not pull by default. Pull only
when the operator explicitly requests the download:

```bash
scripts/ollama_qwen_start.sh --pull
```

If a digest does not match, first confirm `OLLAMA_MODEL`, `OLLAMA_MODEL_DIGEST`,
`EMBEDDING_MODEL`, `EMBEDDING_MODEL_DIGEST`, and the installed roles returned by:

```bash
scripts/ollama_qwen_status.sh
curl --fail http://127.0.0.1:11434/api/tags \
  | jq -r '.models[] | select(.name == "qwen3:14b") | "\(.name) \(.digest)"'
```

If a new digest is intentional, update the matching `OLLAMA_MODEL_DIGEST` or
`EMBEDDING_MODEL_DIGEST` after verification. If it is not intentional, reinstall
or pull the expected model with `--pull`. The accepted `qwen3:14b` digest is
`bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8`; do not
switch to an older semantic model to work around a mismatch.

If the models are healthy but `academic_embeddings` reports stale vectors, run
the counts-only dry run, then the bounded backfill:

```bash
.venv/bin/python -m app.agents.academic_planner.material_embedding_backfill \
  --batch-size 64 --max-batches 1
.venv/bin/python -m app.agents.academic_planner.material_embedding_backfill \
  --apply --batch-size 64 --max-batches 10 --bootstrap-empty-corpus
```

Repeat the apply command until `remaining_count` is zero. Semantic retrieval
does not silently fall back to keyword search while embeddings are unavailable.

After changing `.env`, deploy the shared image and restart the native runtime:

```bash
scripts/lifeagent_host_runtime.sh deploy
```

Use this canonical deploy path before declaring model recovery complete. It
builds the shared image, runs the deployment preflight, and refreshes the native
runtime snapshot; an ad hoc Compose restart is only a diagnostic step.

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

That first real structured request loads the 14B model at a 32,768-token
context and can be noticeably slower than later warm requests.
`OLLAMA_TIMEOUT_SECONDS=300` is the bounded request timeout for the accepted
profile. `OLLAMA_MODEL_KEEP_ALIVE_SECONDS=300` keeps Qwen resident across one
bounded planner loop and lets Ollama unload it after about five idle minutes.
To unload immediately without deleting model files:

```bash
scripts/ollama_qwen_unload.sh
```

Model residency is not conversation memory. While an owner is answering a
clarification, no generation request or database transaction stays open. The
next wake reloads the complete canonical transcript and trusted tool checkpoint,
then assembles bounded summary-plus-tail model context, so an unload or worker
restart is safe. If a turn still reports context capacity exhaustion after safe
compaction, do not delete the session or retry with full replay; ask the owner to
shorten the current request or cancel and resend a narrower request. Expired or
corrupt sessions require a complete resend.

Generic durable memory is owner-and-channel scoped and separate from academic
learning-focus memory. Active writes require explicit owner language such as
"remember", "forget", or "correct what you remember". Semantic retrieval may
be unavailable while exact/category retrieval continues; that state must not be
reported as proof that memory is empty.

The 32K profile is the documented default because the checked-in acceptance
artifact passed with `gate_passed=true`, observed resident context exactly
`32768`, stable model identity, physical model concurrency one, successful
combined generation-plus-embedding residency, p95 below 300 seconds, at least
10% host free memory, and no more than 1 GiB steady swap growth. If slow turns
coincide with lower free memory, continuing swap growth, or another resident
model, run `scripts/ollama_qwen_unload.sh`, stop competing workloads, confirm
`ollama ps`, and retry from the canonical deploy. Do not add a legacy model or
full-replay alternate profile as a workaround.

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
