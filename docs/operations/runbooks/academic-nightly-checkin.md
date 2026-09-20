# Runbook: Academic nightly check-in

Use this runbook when the evening reflection prompt is missing, late,
duplicated, or cannot open a durable conversation.

## Current architecture

The resident academic worker evaluates `ACADEMIC_END_OF_DAY_SCHEDULE` in
`APP_TIMEZONE` every minute. It may catch up only inside
`ACADEMIC_END_OF_DAY_CATCHUP_GRACE_MINUTES`. Each local occurrence uses:

- run agent: `academic_nightly_checkin`;
- run schedule and period namespace: `academic-end-of-day`;
- period key: `academic-end-of-day:YYYY-MM-DD:HHMM:v1`;
- delivery key: `academic-eod-delivery:YYYY-MM-DD:HHMM:v1`;
- health row: `academic_end_of_day`.

The job sends one host-rendered Discord prompt and opens an artifact-backed
native conversation for `DISCORD_ACADEMIC_PROACTIVE_USER_ID`. It does not call
Qwen until that authorized owner replies. A reply may update academic
learning-focus memory or prepare a calendar proposal, but it cannot bypass exact
confirmation. A skip command closes the conversation without a write.

## Required configuration

```dotenv
APP_TIMEZONE=America/Toronto
ACADEMIC_END_OF_DAY_SCHEDULE=21:00
ACADEMIC_END_OF_DAY_CATCHUP_GRACE_MINUTES=30
DISCORD_ACADEMIC_PROACTIVE_USER_ID=123456789012345678
DISCORD_ACADEMIC_AUTHORIZED_USER_IDS=[123456789012345678]
DISCORD_ACADEMIC_MESSAGE_CONTENT_ENABLED=true
```

The Discord bot token, academic channel, application ID, and host handoff secret
must also be configured. The proactive owner must be in the authorized-user
list; otherwise health reports `setup_attention` and no prompt is sent.

## Diagnosis

Inspect health without exposing conversation content:

```bash
curl --fail http://127.0.0.1:8000/health/ready | jq .
```

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT check_name, state, rule, checked_at, last_success_at, next_due_at, diagnostic
FROM health_checks
WHERE check_name = '\''academic_end_of_day'\''
ORDER BY checked_at DESC
LIMIT 1;"'
```

Inspect recent runs and durable delivery state:

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

If no run exists after the grace deadline, inspect both queue task names:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, task_name, status, attempts, scheduled_at
FROM procrastinate_jobs
WHERE task_name IN (
  '\''lifeagent.schedule.academic_nightly_checkin'\'',
  '\''lifeagent.academic_nightly_checkin'\''
)
ORDER BY scheduled_at DESC
LIMIT 20;"'
```

## Recovery

- `setup_attention`: configure the proactive owner and required Discord handoff
  settings, ensure the owner is allowlisted, then recreate the worker.
- `native_conversation_busy`: finish, cancel, or let the existing conversation
  expire. The scheduler retries only while the occurrence remains in grace.
- `schedule_late`: do not replay the stale prompt; verify the next occurrence.
- failed or uncertain delivery: inspect the durable delivery row before retrying
  so an already accepted Discord message is not duplicated.

After changing `.env`, use the canonical deployment path:

```bash
scripts/lifeagent_host_runtime.sh deploy
```

For a controlled check, temporarily set the schedule to a nearby local minute,
keep the proactive owner allowlisted, verify one delivery and one
`awaiting_user` conversation, reply `skip`, and confirm that no Notion or memory
write occurs.
