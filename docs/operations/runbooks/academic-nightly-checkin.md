# Runbook: Academic nightly task check-in

Use this runbook when the evening course-task checklist is missing, stale,
duplicated, or cannot safely update Notion.

## Current architecture

The resident academic worker evaluates `ACADEMIC_END_OF_DAY_SCHEDULE` in
`APP_TIMEZONE` every minute and catches up only inside
`ACADEMIC_END_OF_DAY_CATCHUP_GRACE_MINUTES`. The configured default is the v2
checklist (`academic-nightly-checkin-v2`); the former free-form reflection is
not a fallback.

For each Toronto-local occurrence the worker:

1. refreshes the academic Notion catalog and requires a provably fresh result;
2. loads active, incomplete items dated for that local day from writable,
   real course calendars only;
3. asks the local model and a semantic critic whether each item is movable
   work, failing closed on uncertainty or model failure;
4. atomically opens an artifact-backed native conversation with the ordered,
   versioned checklist checkpoint; and
5. sends the first concrete task question, one item at a time.

The checkpoint and transcript are private artifacts. Relational lifecycle and
run metadata contain stable IDs and diagnostic codes, not task titles or owner
reply text. No session is opened when there are no eligible tasks.

An answer meaning “completed” authorizes only a guarded title change for the
current item to `Completed — <exact existing title>`. An incomplete answer only
prepares and previews a one-Toronto-calendar-day move. The move is applied only
after a separate natural confirmation in the same valid nightly session. Global
academic, LEARN, material, and career proposals still require exact
`confirm <proposal_id>` input.

Both writes use stable proposal and operation IDs, re-resolve the Notion target,
and check its exact title and edit version. Timed items preserve wall-clock time;
all-day precision and start/end duration are preserved. The bot acknowledges
before calling Notion, then reports the actual outcome and next task.

## Required configuration

```dotenv
APP_TIMEZONE=America/Toronto
ACADEMIC_END_OF_DAY_SCHEDULE=21:00
ACADEMIC_END_OF_DAY_CATCHUP_GRACE_MINUTES=30
DISCORD_ACADEMIC_PROACTIVE_USER_ID=123456789012345678
DISCORD_ACADEMIC_AUTHORIZED_USER_IDS=[123456789012345678]
DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED=true
```

The Discord bot/application settings, Notion token and Courses database ID,
local Qwen runtime, artifact store, and database must also be available. The
proactive owner must be allowlisted.

## Safe diagnosis

Check health and recent runs without selecting private artifact content:

```bash
curl --fail http://127.0.0.1:8000/health/ready | jq .
```

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, idempotency_key, status, error_code, started_at, finished_at, summary
FROM agent_runs
WHERE agent_name = '\''academic_nightly_checkin'\''
ORDER BY started_at DESC
LIMIT 10;"'
```

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT run_id, target, idempotency_key, status, attempt_count, error_code
FROM deliveries
WHERE idempotency_key LIKE '\''academic-eod-delivery:%'\''
ORDER BY created_at DESC
LIMIT 10;"'
```

For proposal/write state, inspect only stable IDs and statuses. Do not print the
proposal payload because it contains private task titles.

## Failure and recovery

- **Catalog refresh failed or stale:** no checklist is built from cached data and
  no Notion write occurs. Restore Notion connectivity, then wait for the next
  scheduled occurrence; do not manually replay an out-of-grace period.
- **Semantic model/critic unavailable:** uncertain items are excluded and there
  is no deterministic classifier fallback. Restore Qwen with
  `scripts/ollama_qwen_start.sh` and retry only inside the schedule grace window.
- **Another native conversation is open:** the scheduler retries inside grace.
  Finish/cancel the active conversation or allow it to expire.
- **A v1 conversation is still open after deployment:** the reply path closes it
  with an owner-visible “started before the checklist update” explanation and
  makes no change.
- **Notion target conflict:** the item changed after preview. The guarded write
  leaves it untouched, reports the conflict, records a terminal failed outcome,
  and proceeds safely. Do not edit the proposal payload or auto-retry it.
- **Expired proposal:** no write occurs. The checklist reports the failure and
  advances or closes; start a new current-day check-in instead of reviving it.
- **Timeout or uncertain connector result:** no automatic second write is issued.
  Inspect the Notion page and proposal-operation receipt before any operator
  action.
- **Duplicate Discord delivery/reply or worker replay:** stable delivery,
  proposal, and operation keys return the existing result. Do not delete those
  records; they are the idempotency proof.
- **Discord delivery failed after session creation:** retrying the same period
  reuses the checkpoint and idempotent delivery key. Never create a second
  session manually.

After configuration changes, deploy through:

```bash
scripts/lifeagent_host_runtime.sh deploy
```

For a controlled validation, schedule a nearby local minute with two fake or
safe course tasks. Verify the first Discord message names exactly one task,
reply with an incomplete natural sentence, verify the exact old/new-date
preview, reply “yeah sure,” observe the pre-write acknowledgement, and confirm
one guarded receipt followed by the second task. Also exercise a fixed exam or
deadline and verify it produces no proposal or Notion call.
