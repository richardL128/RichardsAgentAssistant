# Implement deterministic Discord ABORT command

## Objective and user-visible outcome

Add an exact Discord `ABORT` code word for the LifeAgent Discord bot and the agent turn it powers. When an authorized user sends a Discord message whose trimmed raw content is exactly `ABORT`, the bot must stop the active Discord conversation turn for that same authorized user/channel, stop scheduling any new agent tool calls for that turn, best-effort cancel currently awaited model/tool work, and send a Discord status message explaining what it was doing and what state the active tool call was left in.

The ABORT behavior is for the product runtime only. Do not add instructions telling Codex, Sol, Luna, or any codebase-building agent to stop this development conversation.

## Requirements

- Match only the exact ASCII code word `ABORT` after `str.strip()`. Do not treat prose containing “abort”, lowercase `abort`, punctuation, or other commands as aborts.
- Accept ABORT only from the already authorized Discord user/channel boundary.
- Process ABORT before normal mention/prose routing, before enqueueing the abort message as an ordinary academic turn, and before the model sees the code word.
- Scope the abort to every earlier non-terminal Discord message turn for the same channel and user. The global model lock means at most one is running, but older queued turns must also be cancelled so the stopped conversation cannot resume later. Never abort another user/channel or a message received after the ABORT timestamp.
- Send an immediate Discord acknowledgement for the ABORT message, then update or follow with the final abort status when cancellation outcome is known.
- If the target turn has not reached the backend worker yet, cancel the host-side handoff path and edit the original progress/wake acknowledgement when available.
- If a target turn is queued or running in the backend worker, persist the abort request outside the global model lock and use Procrastinate's native job cancellation/abort API so it can interrupt the locked worker.
- The worker must stop before starting another model turn or tool call after the abort is observed.
- In-flight async model/tool work should be cancelled with `asyncio.CancelledError` where possible. For atomic external calls that cannot be proven cancelled, report an honest `unknown` or `completed_before_cancel` status instead of claiming rollback.
- Preserve idempotency: duplicate ABORT messages or gateway replays must not send contradictory final states.
- Keep raw Discord content and private tool data out of durable abort records and user-visible status messages.
- Close owner/channel-scoped resumable conversation state so the next ordinary message begins a fresh interaction. Preserve the historical rows and record an explicit aborted/cancelled outcome.

## Non-goals

- Do not implement a general natural-language cancel command.
- Do not abort scheduled jobs, reminders, unrelated queue tasks, or other users' turns.
- Do not guarantee rollback of external side effects that already completed before cancellation was observed.
- Do not reject or cancel a proposal that was fully produced by an earlier completed turn and is already awaiting explicit confirmation; `ABORT` is not a substitute for `reject <uuid>`.
- Do not delete conversation, wake, delivery, proposal, material, or audit history.
- Do not rely on repository plans or prompts as evidence of current behavior during implementation; inspect the current code first.

## Current repository context

Evidence gathered from executable code:

- `DiscordGatewayListener` normalizes allowed-channel/authorized-user `MESSAGE_CREATE` events and schedules message handling tasks instead of awaiting them inline. See `app/connectors/discord_gateway.py` around `_handle_message_create`, `_schedule_message`, and `_run_message_handler` (lines 437-461 and 586-623).
- `HostWakeCoordinator.process_message` currently treats authorized exact confirmation/rejection text as a command and all other authorized channel prose as a mention-style handoff. See `app/host/coordinator.py` lines 160-203 and `_request_kind` at lines 428-442.
- The host wake daemon wires the gateway listener to `coordinator.handle_message`. See `app/host/daemon.py` lines 47-57 and 60-75.
- Host acknowledgements and progress edits already exist through `DiscordWakeAckAdapter.send_acknowledgement` and `edit_acknowledgement`. See `app/host/discord.py` lines 65-155.
- Host-to-backend handoff is signed, localhost-only, and reference-only via `DiscordHostHandoffEvent` and `DiscordHostHandoffClient`. See `app/host/handoff.py` lines 22-67 and 114-178.
- Backend handoff currently refetches, validates, persists `DiscordWakeInbound`, and enqueues `queue_tasks.defer_discord_wake`. See `app/api/discord_handoff.py` lines 47-118 and 163-182.
- Discord wake jobs run under the global model lock: `defer_discord_wake` configures `lock=_MODEL_LOCK`, and `discord_wake_task` delegates to the registered worker handler. See `app/queue/tasks.py` lines 140-151 and 325-340. Therefore ABORT must not be implemented as an ordinary `discord_wake_task`, or it will wait behind the work it is supposed to interrupt.
- The installed Procrastinate 3.9 job manager exposes `cancel_job_by_id_async(job_id, abort=True)`. For a queued job it changes `todo` to `cancelled`; for a running async job it sets `abort_requested`, notifies the worker, and the worker cancels that job's asyncio task. Use this supported mechanism instead of inventing a second polling cancellation loop. Persist the returned queue job ID on the wake row so the abort target is exact.
- The durable wake table currently has states `queued`, `running`, `completed`, and `failed`, with no abort state/fields. See `app/db/models.py` lines 205-263 and repository transitions in `app/db/discord_wake.py` lines 21-42 and 255-292.
- `DiscordWakeJob` marks a wake running, calls the academic Discord service, and then marks completed/failed. See `app/agents/academic_planner/discord_wake_job.py` lines 43-84.
- The academic Discord handler runs `run_native_tool_loop`; it already catches `asyncio.CancelledError` and currently finishes progress as failed before re-raising. See `app/agents/academic_planner/discord_harness.py` lines 429-441.
- The native harness cancels the model invocation task if its parent task is cancelled. See `app/agents/harness.py` lines 218-277. This is the path to use for best-effort interruption of a model call.
- Progress already captures model turns and tool activity, and can edit the existing host wake message via `progress_message_id`. See `app/agents/academic_planner/discord_harness.py` lines 410-427 and `app/connectors/discord.py` lines 1452-1614.
- There is no generic durable native conversation model in the executable runtime. Current resumable dialogue uses owner/channel-scoped `AcademicDiscourseSession` rows for learning-focus, memory-review, and agent-clarification flows. ABORT must close these current session types rather than assuming an unimplemented generic conversation service exists.

## Expected files and components to change

- `app/agents/academic_planner/commands.py`: add the single canonical exact ABORT parser used at deterministic boundaries.
- `app/host/coordinator.py`: recognize exact `ABORT`, maintain host-side active message scope, cancel host-level in-flight processing, and submit a backend abort handoff.
- `app/host/discord.py`: add abort acknowledgement/final status copy if the coordinator needs constants/helpers.
- `app/host/handoff.py`: add a signed reference-only `DiscordHostAbortEvent` and localhost-only abort client method or sibling client.
- `app/host/outbox.py`: add an `aborted` terminal outbox state and query/update helpers for active rows by channel/user.
- `app/connectors/discord_gateway.py`: likely no routing change beyond tests, because it already schedules messages concurrently; add tests proving ABORT can be handled while another message task is running.
- `app/api/discord_handoff.py`: accept the signed abort event on the loopback boundary, refetch and exactly verify the Discord message, request queue cancellation outside the model lock, and return a bounded final receipt status.
- `app/db/models.py`: add `queue_job_id`, abort lifecycle, and safe activity fields to `DiscordWakeInbound`; add a small idempotent abort-request record only if one wake row cannot represent one ABORT affecting multiple target wakes cleanly.
- `app/db/discord_wake.py`: add repository methods to bind queue job IDs, atomically select/request abort for all eligible owner/channel turns, track allowlisted activity, close queued/running wakes correctly, and render a bounded status snapshot.
- `app/db/migrations/versions/00xx_*.py`: migrate wake state constraints and new abort/progress fields.
- `app/agents/academic_planner/discord_wake_job.py`: handle Procrastinate-delivered task cancellation, persist an aborted outcome, and distinguish user abort from worker shutdown or failure.
- `app/agents/academic_planner/discord_harness.py`: surface an aborted terminal result/progress message instead of generic failed progress on expected cancellation.
- `app/connectors/discord.py`: add an `aborted` terminal progress phase/rendering or an explicit abort status edit path.
- `app/db/academic.py` and the current conversation services: add one owner/channel-scoped method that closes open academic discourse sessions with an explicit aborted final state, and expires only unresolved material continuations that would otherwise auto-resume.
- `tests/unit/test_host_coordinator.py`, `tests/unit/test_host_handoff.py`, `tests/unit/test_host_outbox.py`, `tests/unit/test_discord_handoff.py`, `tests/unit/test_discord_wake_store.py`, `tests/unit/test_discord_wake_job.py`, `tests/unit/test_agent_harness.py`, `tests/unit/test_academic_native_discord_harness.py`, and queue tests as needed.

## Implementation steps

1. Add `is_discord_abort_command(content: str) -> bool` in the deterministic command module, returning only `content.strip() == "ABORT"`. Reuse it at the host and backend boundaries. Do not case-fold, accept a mention, or place this rule in the model prompt.

2. Extend the host outbox request kind with `abort`, its state with `aborted`, and add active-row helpers:
   - find every earlier `pending`/`acknowledged` row by `(channel_id, author_id)` and abort timestamp;
   - mark those rows aborted with a safe reason code such as `user_abort`;
   - ensure `pending_rows()` does not replay aborted rows;
   - keep `prune_terminal()` treating aborted as terminal.

3. In `HostWakeCoordinator`, intercept ABORT before normal command/prose routing:
   - verify the channel and user through existing settings;
   - send an immediate acknowledgement for the ABORT message with a deterministic nonce;
   - under a per-owner/channel lock, cancel every registered earlier `process_message` task for that scope if it is still waiting on wake/handoff, then mark its outbox row aborted so replay cannot revive it;
   - edit the original wake/progress acknowledgement if one exists;
   - always submit the signed backend abort event as well, because handoff and cancellation can race;
   - return a bounded status such as `handled`.

4. Add a signed host-to-backend abort handoff on the existing localhost-only endpoint (or a sibling endpoint if separation is clearer):
   - define `DiscordHostAbortEvent` containing version, abort_message_id, channel_id, author_id, event_timestamp, handoff_timestamp, nonce;
   - canonicalize and sign it with the existing HMAC mechanism;
   - return a separate bounded response model containing only receipt state, safe activity label, safe tool status, and counts; never return raw args/results;
   - have the host edit the ABORT acknowledgement with that final receipt. If confirmation times out, terminally report `cancellation unconfirmed` rather than leaving the acknowledgement in a working state.

5. Extend durable wake persistence before adding cancellation behavior:
   - store the Procrastinate `job_id` returned by `defer_discord_wake` as `queue_job_id` before considering the wake safely enqueued;
   - add wake states `abort_requested` and `aborted`, timestamps for request/terminal cancellation, and safe activity fields (`activity_phase`, model turn, allowlisted tool name/activity, tool status, side-effect class, updated time);
   - keep existing rows valid and preserve their history;
   - handle the enqueue race: if ABORT marks a row before its queue job ID is attached, the enqueuer must either skip deferral or immediately cancel the newly created job before returning.

6. Add backend ABORT processing outside the model lock:
   - route authenticates exactly like `/handoff`;
   - refetches the Discord message and verifies that its trimmed content is exactly `ABORT`, its IDs/timestamp still match, and its acknowledgement belongs to this bot;
   - atomically selects all earlier queued/running wakes for the same channel/user, records the abort request, and excludes unrelated, later, or already terminal rows;
   - calls `procrastinate_app.job_manager.cancel_job_by_id_async(job_id, abort=True)` for each bound target: queued jobs become cancelled and their wake rows become `aborted`; running jobs become `abort_requested` until the worker confirms termination;
   - waits for a short configured bound below the host handoff timeout, refreshes all targets, and returns a terminal receipt even if cancellation remains unconfirmed.

7. Add safe activity tracking for status:
   - inject an execution-status sink into `NativeAcademicDiscordHandler`, keyed by the wake/message ID, and update durable status before performing slower Discord progress delivery;
   - record runtime check, model waiting, tool started, tool succeeded/failed, proposal persistence, confirmation/external write, reply delivery, and terminal state;
   - extend `NativeTool` with an allowlisted abort/side-effect classification such as `read_only`, `proposal_only`, `durable_local_write`, or `external_write`. Declare it for every exposed academic, career, and memory tool;
   - persist only the tool name/activity and classification, never args, results, prompt text, or hidden reasoning.

8. Use native queue cancellation and make worker termination durable:
   - in `DiscordWakeJob.__call__`, check abort before creating the service;
   - catch `asyncio.CancelledError` at the wake-job boundary, inspect the persisted abort request, synchronously mark the wake `aborted` with its last safe activity/tool status, then re-raise so Procrastinate records the job as aborted and does not retry it;
   - retain failure behavior for unrelated cancellation/shutdown and do not mislabel it as a user abort;
   - make `mark_completed`/`mark_failed` race-safe when ABORT arrives just as work finishes, so the receipt can say `completed before abort` instead of overwriting a terminal result.

9. Add a cooperative no-new-tools checkpoint and abort-specific progress cleanup:
   - pass a cheap `raise_if_abort_requested` callback into the Discord harness and check it before each new model turn and immediately before each tool handler. This backs up queue notification delivery and makes “no later tool calls” deterministic once the durable flag is visible;
   - distinguish expected user abort from unexpected parent cancellation;
   - emit/render terminal `aborted` status or a dedicated final edit: “Aborted. I was working on: <phase>. Tool status: <status>.”;
   - avoid sending normal final responses or confirmations after abort has been observed.

10. Close resumable conversation state without deleting history:
   - in the same owner/channel scope, close open `AcademicDiscourseSession` rows for learning focus, memory review, and agent clarification using a final state containing only `outcome="aborted"`, the abort Discord event ID, and timestamp;
   - expire unresolved inbound materials in `captured`/`awaiting_target` that would otherwise be auto-selected by the next message; do not touch material already linked to a pending or applying proposal;
   - if the current runtime exposes other owner/channel-scoped continuation rows at implementation time, close them through their existing repository transition (for example a career clarification that would auto-resume). Do not blanket-update rows lacking a verified owner/channel link;
   - leave already delivered pending proposals intact so they still require explicit `confirm` or `reject`.

11. Add idempotency and race handling:
   - replay of the same ABORT Discord message returns the same recorded outcome and reuses the same acknowledgement; a later distinct ABORT may truthfully say there is nothing active;
   - if the target completes before abort is recorded, reply that there was no active turn to stop or that the turn had already completed;
   - if the abort arrives before backend handoff, host outbox abort must prevent replay from later handing off the old message.

12. Keep all user-visible copy concise and honest:
    - immediate: `Abort received. I’m stopping the active Discord turn now.`
    - final examples:
      - `Aborted. I was waiting on the model; the model request was cancelled.`
      - `Aborted. I was running tool activity: course_data; cancellation was requested while that call was in flight, so its final external state is unknown.`
      - `Abort received, but there was no active Discord turn for you in this channel.`
   - when several older turns existed, include a bounded count such as `1 running turn stopped; 2 queued turns cancelled` while describing only the single running operation;
   - update the original turn's progress message to terminal `aborted`, and update the ABORT acknowledgement with the final receipt. Do not send a second normal model answer.

## Migration and compatibility

- Add a forward migration after the current head. The current working tree includes an untracked `0026_semantic_calendar_events.py`; inspect Alembic heads before choosing the revision number.
- Existing rows with `queued`, `running`, `completed`, and `failed` must remain valid.
- If changing `DiscordWakeInbound.state`, update all check constraints and repository constants together.
- Do not store raw Discord message text in new abort tables/columns.
- Keep the existing handoff API compatible for normal messages and interactions.
- Do not add a second runtime path or a legacy fallback. The exact ABORT path is the sole cancellation control for native Discord conversations.

## Validation

Run focused tests first, then broader relevant tests:

- `pytest tests/unit/test_host_coordinator.py`
- `pytest tests/unit/test_host_handoff.py`
- `pytest tests/unit/test_host_outbox.py`
- `pytest tests/unit/test_discord_handoff.py`
- `pytest tests/unit/test_discord_wake_store.py`
- `pytest tests/unit/test_discord_wake_job.py`
- `pytest tests/unit/test_agent_harness.py`
- `pytest tests/unit/test_academic_native_discord_harness.py`
- `pytest tests/unit/test_queue.py`
- Any handoff API tests present or newly added.

Acceptance scenarios:

- Exact `ABORT` from authorized user/channel sends an immediate acknowledgement and is not handed to Qwen as user prose.
- Lowercase `abort`, `ABORT now`, and unauthorized ABORT do not trigger cancellation.
- ABORT while the original request is still waking cancels host-side handoff and prevents replay.
- ABORT after backend queue acceptance but before worker start cancels the Procrastinate job, marks the wake aborted, and the worker never starts it.
- ABORT during a blocked model call cancels the harness task and produces an aborted status.
- ABORT during a tool call stops further tool calls and reports the last safe tool status without exposing args/results.
- ABORT cancels all earlier queued turns for the same owner/channel, not just the running one, and leaves unrelated/later work untouched.
- ABORT closes an open resumable clarification/memory session; the next ordinary message does not resume it.
- Duplicate gateway delivery or duplicate ABORT does not produce conflicting final states or duplicate receipts.
- A completed pending proposal remains pending and still requires explicit confirmation or rejection.
- Migration upgrade and downgrade tests pass from the current migration head, then run the repository's full unit suite and the relevant integration suite.
- Through the real private Discord channel, verify acknowledgement-before-cancellation, cancellation during a deliberately blocked model call, cancellation during a controlled tool call, no-active-turn behavior, and a post-ABORT fresh message. Do not declare the feature working if this end-to-end check cannot be completed.

## Known risks and unresolved decisions

- Python task cancellation is best-effort. Some external HTTP/database calls may complete before cancellation is delivered; the status message must say `unknown` when the code cannot prove cancellation.
- If the backend API is down and the original turn is already running in a worker, the host daemon cannot persist the abort until the backend is reachable. The immediate ABORT reply should say cancellation was requested, then edit/follow up when the backend confirms.
- Procrastinate names queued terminal jobs `cancelled` and running interrupted jobs `aborted`. Keep those infrastructure states intact, but normalize the LifeAgent wake state and product copy to `aborted` so the owner sees one term.
- Tool cancellation certainty depends on its declared side-effect class. A read-only/model wait can usually be reported cancelled; a durable local or external write interrupted before a terminal event must be reported as `cancellation unconfirmed` and must name the relevant safe inspection/recovery step.

## Fresh-session handoff

After review, execute this in a fresh Codex session:

`Read prompts/implement-discord-abort-command.md and execute it step by step. Validate the completed feature according to the plan and repository instructions.`
