# Runbook: Academic Morning Notification

## Symptom

The morning academic to-do message is missing, duplicated, late, or reports an
operational failure. The operations console may show
`health_checks.check_name = 'academic_morning'` as `Attention` or `Failed`, with
a rule such as `run_overdue`, `required_delivery_failed`, or
`delivery_incomplete`.

## Current architecture

The academic morning notification is the only executable automatic academic
schedule. It is deterministic and model-free. Messages from authorized owners
in the configured private Discord channel are the only Qwen path; this
scheduled job must never load Qwen.

The schedule is configured by:

```dotenv
APP_TIMEZONE=America/Toronto
ACADEMIC_MORNING_SCHEDULE=08:00
ACADEMIC_MORNING_CATCHUP_GRACE_MINUTES=30
```

`ACADEMIC_MORNING_SCHEDULE` is interpreted in `APP_TIMEZONE`. The 30-minute
default grace window allows short startup catch-up and prevents stale late-day
delivery.

Each occurrence uses stable identities:

- run agent: `academic_morning_notification`;
- run schedule: `academic-morning`;
- run idempotency key: `academic-morning:YYYY-MM-DD:HHMM:v1`;
- delivery idempotency key: `academic-morning-delivery:YYYY-MM-DD:HHMM:v1`;
- health row: `academic_morning`.

## Diagnosis

Check the latest health row:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT check_name, state, rule, checked_at, last_success_at, next_due_at, diagnostic
FROM health_checks
WHERE check_name = '\''academic_morning'\''
ORDER BY checked_at DESC
LIMIT 1;"'
```

Inspect the run and its delivery:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, idempotency_key, status, error_code, started_at, finished_at, summary
FROM agent_runs
WHERE agent_name = '\''academic_morning_notification'\''
ORDER BY started_at DESC
LIMIT 10;"'
```

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT run_id, channel, target, idempotency_key, status, attempt_count, error_code, external_url
FROM deliveries
WHERE idempotency_key LIKE '\''academic-morning-delivery:%'\''
ORDER BY created_at DESC
LIMIT 10;"'
```

If there is no run after the grace deadline, inspect queue state for the current
task names:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, task_name, status, attempts, scheduled_at
FROM procrastinate_jobs
WHERE task_name IN (
  '\''lifeagent.schedule.academic_morning_notification'\'',
  '\''lifeagent.academic_morning_notification'\''
)
ORDER BY scheduled_at DESC
LIMIT 20;"'
```

## Source freshness requirement

The normal morning message requires a fresh complete Notion sync immediately
before planning. Missing Courses sharing, missing nested Assessments calendars,
missing required title/date properties, partial source failure, or stale sync
must be reported as an operational condition. Do not convert any of those states
into an empty or light-day message.

## Expected health behavior

- Before the due time and during the grace window, an absent run is not overdue.
- After the grace deadline, an absent run is `attention` with `run_overdue`.
- A failed or attention run remains unhealthy until the next successful period.
- A succeeded run without a delivery is unhealthy.
- A failed delivery is unhealthy.
- An uncertain delivery is `attention` and must be reconciled before replay.
- A sent or acknowledged delivery is healthy, and `next_due_at` advances to the
  next occurrence plus the grace window.

## Controlled acceptance check

Use a controlled trigger in a non-production or explicitly approved live window:

1. Set `ACADEMIC_MORNING_SCHEDULE` to a nearby `APP_TIMEZONE` minute and keep
   `ACADEMIC_MORNING_CATCHUP_GRACE_MINUTES=30`.
2. Start `postgres`, `api`, and `worker-academic-planner`.
3. Verify the worker records one run with the expected
   `academic-morning:YYYY-MM-DD:HHMM:v1` key.
4. Verify exactly one Discord delivery with the corresponding
   `academic-morning-delivery:YYYY-MM-DD:HHMM:v1` key.
5. Replay the same period and verify no second Discord message is sent.
6. Temporarily break Notion sharing or use a mocked/source-failure environment
   and verify the job reports setup/source failure instead of a normal plan.
7. Restore the real Notion sharing and confirm the next period returns healthy.

Do not test by enabling any scheduled Qwen/model job; no such job is part of the
current runtime.
