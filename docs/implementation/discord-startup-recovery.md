# Discord startup recovery — 2026-09-09

Status: implementation and validation in progress. Live Discord acceptance is not complete.

## Diagnosis from current runtime

The previous LaunchAgent remediation fixed directory permissions, virtualenv
identity and loaded-versus-running status checks. It did not remove the runtime's
dependency on the Desktop checkout. The current Discord LaunchAgent repeatedly
exits 126: its stderr reports `Operation not permitted` opening the startup script
and accessing the working directory under Desktop. Ollama is reachable and its
installed model matches the configured digest, but a dead Gateway listener cannot
receive a ping to start the application.

The application containers also restarted independently: Compose supplied
`authorized_discord_channel`, but the deployed image still accepted only
`discord_mentions_only`. Rebuilding from the current checkout and recreating the
containers with the existing handoff key restored API and worker startup.

The current native handler uses a synchronized SQL catalog without invoking a
Notion refresh. Requests after a cold wake can therefore use stale or empty
course data. Separately, the worker records handler-returned failures as completed.
The host API probe also accepted redirects and client errors as healthy.

## Implementation plan

1. Install a self-contained native runtime under the user's Application Support
   directory. Copy runtime source and configuration, create its own dependencies,
   and render LaunchAgents against that installation. No runtime path may depend
   on the Desktop checkout. Preserve the existing handoff key and pending ID-only
   outbox during migration.
2. Deploy one matching image/configuration snapshot and validate it before
   declaring installation successful. Keep the native listener as the sole
   Discord ingress and keep builds and model pulls out of the wake path.
3. Refresh Notion lazily before the first academic catalog search in a turn.
   General questions do not need Notion. Failed refreshes must not be represented
   as fresh empty results or successful searches.
4. Persist failed handler outcomes as failed. Require HTTP 200 from the host's
   backend liveness probe.
5. Honor the user's model reconfiguration pause. Do not add input truncation or
   change model/context limits. Resume model validation after the user finishes
   selecting the model and editing configuration.

## Evidence and remaining gates

- Reviewed the historical fix report, root implementation plan, phases 0–8,
  semantic memory implementation plan, architecture and startup runbooks.
- Rebuilt the application image and observed the API healthy and worker running.
- A live read-only Notion discovery returned seven courses and no diagnostics.
- Before the model pause, one real native model request answered correctly;
  this was an internal diagnostic, not Discord acceptance.
- Before subsequent concurrent queue-refactor edits, checks after the Docker
  readiness fix passed: 799 unit tests
  passed; 22 integration tests
  passed and three opt-in destructive recovery checks were skipped. Repository
  lint and type checks passed. Shell syntax and diff whitespace checks passed.
- A fresh runtime snapshot using example configuration installed a managed
  Python 3.12.14 interpreter and locked dependencies in an isolated temporary
  directory. Imports of the host daemon, HTTP client and WebSocket client were
  verified to resolve entirely inside that snapshot and its managed Python root.
  This check did not launch the Discord listener or invoke the model.
- The user subsequently saved a 16,384-token context, a 12,000-token input
  threshold and a 2,048-token output limit for the original model. A direct
  application gateway request answered correctly in 29.7 seconds. Ollama
  reported 16,384 context tokens, approximately 20 GB resident and 100% GPU.
- A second diagnostic exercised the current native handler with its real
  Notion sync, SQL catalog and original model; only Discord delivery was
  replaced with local output. It refreshed and listed all seven courses
  correctly in 34.7 seconds. This remains internal diagnostic evidence, not
  user-facing Discord acceptance.
- The user completed deployment. Independent checks confirmed the installed
  runtime's 16,384-token configuration, matching image marker, reachable Ollama
  and stable native Discord listener (PID 64889 across repeated samples).
- With permission to interrupt the other project's database temporarily, the
  Docker cold-wake test stopped the engine. Docker Desktop's stop command timed
  out on two lingering `docker-mcp` processes; the engine socket disappeared.
  Two real Discord requests on September 10 UTC received acknowledgements in
  approximately 165 and 511 milliseconds, followed by explicit Docker startup
  failures. Both native outbox entries were failed, not falsely successful.
- The Docker CLI then reported `already running` with exit code zero while
  `docker info` still failed because the engine socket was absent. The wake
  implementation checked readiness only once after the start command. A
  bounded readiness polling fix now waits within a shared startup deadline
  and kills/reaps cancelled command subprocesses. Polling alone does not prove
  recovery from a genuinely stuck Desktop process.
- Manual app launch from the agent failed with LaunchServices error -10827,
  although the bundle executable exists. Computer control for Docker is not
  approved. The user was asked to open Docker Desktop manually. A subsequent
  engine probe succeeded, and `jobappmodelassist-db` was running and healthy
  again. No other project's container configuration was changed.
- After engine restoration, a real Discord greeting at 02:06:30 UTC received
  its acknowledgement in 0.361 seconds and a correct final answer in 35.952
  seconds. The request successfully started API/worker and used Qwen. This
  establishes one successful prompt, not successful Desktop cold startup.
- The second prompt asked for assignments due within the next week. Its
  acknowledgement arrived in 0.437 seconds, but streamed tool messages hit
  Discord HTTP 429 twice. The worker retried the whole turn, then marked the
  acknowledgement completed without sending a final answer. Positional
  `harness-event-N` delivery keys restarted at one on retry, so a different,
  shorter retry transcript collided with already-delivered events. This is
  failed acceptance despite the worker's success status. The native harness
  now gives the final answer a stable delivery key separate from positional
  intermediate events and delivers it before reporting completion. Focused
  regression tests cover replay and a shorter retry transcript. Discord sends
  now honor a valid Retry-After header (or JSON retry_after when absent) for
  HTTP 429, with at most three attempts and five seconds total delay. The
  destination, content and nonce stay fixed; ambiguous 5xx are not retried.
- Read-only verification found zero assessments in the running backend's SQL
  database and empty calendars for all seven courses in live Notion discovery,
  with no discovery diagnostics. Empty search results were consistent with
  the connected source; they did not excuse the missing final answer.
- Final repository validation encountered concurrent queue/health refactoring
  outside this recovery's edits: 808 unit tests passed and three failed;
  21 integration tests passed, one failed and three were skipped. Failures
  reference removed `tasks._handlers`, a missing `RunStatus` import in the
  shared-service alert path, and a removed finance next-due expectation. Lint
  and type checks also flag the missing import. Those concurrent edits were
  preserved. Deployment is paused until the refactor is reconciled; the
  currently running services were not rebuilt from this mixed snapshot.
- The final focused wake, coordinator, native-harness, delivery and wake-job
  regression suite passed all 64 tests. Lint passed for the recovery-owned
  Python files; this does not override the repository-level failures above.
- Remaining: validate and deploy
  the readiness and Discord delivery fixes; exercise a successful Docker cold wake;
  confirm immediate acknowledgement, truthful progress and final delivery for
  three random natural-language prompts submitted by the user in Discord.

## Requested 50-turn limit

The native tool-loop default is now 50, and the Discord handler no longer
overrides it with 10. Regression tests verify final answers on turns 11 and 50,
failure at the 50-turn boundary with accumulated proposals discarded, and no
51st invocation. All 21 focused harness tests pass. This does not change model
context, input limits or input truncation behavior.

Read-only inspection still finds a 10-turn limit in both the installed runtime
and running worker. Deployment and real Discord validation are therefore still
required. The parallel refactor subsequently updated its two worker tests to
assert that retired task entry points are absent. The remaining finance
integration assertion was aligned with the explicitly event-driven health
implementation: historical approval runs remain persisted in attention, but
have no scheduled next run. No retired runtime or schedule was restored. The
unit suite now passes all 814 tests; the integration suite passes 22 tests with
three opt-in tests skipped, and repository lint/type checks pass.

Deployment must be run from the user's terminal because this agent cannot
write the installed Application Support runtime or LaunchAgent files. Use the
canonical `scripts/lifeagent_host_runtime.sh deploy` workflow, with
`LIFEAGENT_UV=/private/tmp/lifeagent-uv-bootstrap/bin/uv` if uv is not on PATH;
then run `scripts/lifeagent_host_runtime.sh status`. Do not claim the 50-turn
change is live until installed/runtime inspection and Discord validation pass.

## Deployment resumed from pasted terminal output

Both supplied terminal transcripts were read. They show successful image
builds, migration/preflight (182 tests passed each time), standalone runtime
creation, both native LaunchAgents running, and intentional API/worker stops.
The VIRTUAL_ENV mismatch warning was non-blocking: uv used the installed
runtime's own environment rather than the activated checkout environment.

Independent live verification now confirms the 50-turn default in the installed
runtime and image, no 10-turn handler override, and a matching deployment marker
(`sha256:d3eb3c04c3a576317b1c0b4a46faf2acd2dd8e291fecba2df6facd6cd279e306`).
After a real Discord request woke the stopped services, the running worker also
reported 50 turns. Native Discord listener PID 9038 was running; API and worker
started successfully, and the unrelated project's database remained healthy.
The installed model settings remain 16,384 context / 12,000 input / 2,048 output.

A weather request at 02:29:37 UTC received an acknowledgement in 0.232 seconds
and a final answer in 20.078 seconds. It honestly stated that no weather-data
tool was available. Further user-selected prompts, especially the previously
failed assignments query, are being monitored. This does not yet establish
successful full Desktop cold startup or complete the outstanding acceptance
gates.

The goal remains incomplete until those user-facing checks succeed. Historical
mention-only descriptions are migration history; the current configured handler
accepts the authorized user's messages in the configured private channel.

## Live acceptance defects found after the 50-turn deployment

A read-only audit of the resumed Discord run found several clean general-answer
turns, but the strict three-flawless-prompt gate still failed. One arithmetic
answer exposed a closing reasoning tag, tool result/error events exposed raw JSON
and internal identifiers, one yearless September date was resolved to 2025, and
study sessions described as 6 PM and 7 PM Toronto were submitted as 18:00Z and
19:00Z. The latter proposal was subsequently confirmed, so its two Notion rows
represent 2 PM and 3 PM Toronto; they are not being silently rewritten by this
remediation.

The native harness now strips common complete and orphan reasoning wrappers from
all visible assistant text. Discord still shows the model's natural-language
action descriptions, but structured tool results remain internal and raw tool
errors are replaced with bounded safe progress. Academic system context now
includes the request's actual local date, time, and IANA timezone. Mutation tool
schemas require local wall-clock timestamps without `Z` or an offset; the host
applies the configured timezone, rejects ambiguous/nonexistent DST times, and
normalizes the resulting aware timestamp through existing contracts. Newly
created assessments must also be due in the future, preventing a model-selected
past year from reaching review.

Post-fix repository validation passes: 826 unit tests, 22 integration tests with
three opt-in recovery tests skipped, repository lint, and repository type checks.
The fixed snapshot still requires deployment to the installed runtime followed by
new live Discord prompts. These automated checks do not satisfy the real boundary
gate on their own.

The user deployed that snapshot as image
`sha256:2e07f69d21f6e43bc23425217b5bd15b441d7d2de08b3d5b1b491c014be79448`.
Installed-file hashes matched the checkout for the generic harness, native Discord
handler/service, and proposal validator, and the image reported a 50-turn default.
Three new Discord prompts then exercised the user-facing boundary. The Notion
assessment query exposed neither raw tool JSON nor catalog identifiers. The study
session proposal truthfully remained a proposal and rendered September 15 at
6:00–6:20 PM America/Toronto, proving the local wall-clock conversion; the user
subsequently chose to confirm and apply it. Delivery rows for all messages were
sent once, and inbound rows completed without handler retries.

A subsequent read-only Notion discovery found the newly applied active ECE 250
row as `Studying Block — Insertion sort`, with start/end values
`2026-09-15T22:00:00Z`–`22:20:00Z` (6:00–6:20 PM America/Toronto), and no
discovery diagnostics. This verifies the external write rather than relying only
on the Discord confirmation or local operation journal.

The arithmetic prompt did not pass strict acceptance despite returning the correct
value: it ignored “reply with only the result,” narrated a recalculation, and
repeated the result. The local source now strengthens the native system contract
to follow explicit output formats, keep scratch work/self-correction private, and
avoid academic tools for non-academic requests. Native decoding is deterministic
at temperature zero with the configured seed. A real Qwen probe against the
patched source returned exactly `10063` with no tool calls. This latest patch must
be redeployed and the three-prompt live gate restarted; the earlier two successful
prompts cannot be combined with a new image to satisfy one deployment's gate.

Before that redeployment, repository drift was reconciled with the concurrent
academic-morning work: the manual sync API response now matches the richer sync
result contract, and queue import-isolation coverage no longer reuses a stale
package attribute. The current authoritative validation is 856 unit tests passed,
23 integration tests passed with three opt-in recovery tests skipped, repository
lint passed, configured type checking passed, and diff whitespace checks passed.

## Gateway reconnect defect on unstable Wi-Fi

After the final deterministic-response snapshot converged as image/marker
`sha256:d1b75792b3f539e1c80e982ad53ea82960fc1a304fe2618a5fb640d3bd6d6dd7`,
a new real Discord greeting received no acknowledgement within 60 seconds. The
native listener was running under launchd but had restarted 18 times. Its stderr
showed repeated `websockets.exceptions.ConnectionClosedError` failures caused by
TCP resets on eduroam. `DiscordGatewayListener.run_forever` retried ordinary
connection/HTTP failures but did not classify the WebSocket library's transport
exception as transient, so it escaped the process. Launchd restarted it after a
gap, losing the in-memory Discord session/sequence and leaving a window in which
messages were missed.

The listener now catches `WebSocketException` alongside its existing transient
connection failures. It keeps the process alive, retains session/sequence state
for Discord resume, waits one second, and reconnects; privileged-intent and other
explicit configuration closes still raise the separate terminal configuration
error. Regression coverage drives two synthetic WebSocket resets through
`run_forever` and verifies both reconnect attempts and bounded sleeps. Current
validation passes 859 unit tests, 23 integration tests with three opt-in recovery
tests skipped, repository lint, configured type checking, and diff whitespace
checks. The reconnect change still requires deployment and a fresh real Discord
gate; the missed greeting is a failed acceptance attempt, not a success.
