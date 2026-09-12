# Runbook: Combined Planner Morning Notification

## Symptom

The combined academic to-do and interview-reminder message is missing,
duplicated, late, or reports an operational failure. The operations console may
show
`health_checks.check_name = 'academic_morning'` as `Attention` or `Failed`, with
a rule such as `run_overdue`, `required_delivery_failed`, or
`delivery_incomplete`.

## Current architecture

The combined planner morning notification is the only executable automatic
planner schedule. Host code owns source refresh, study allocation, the exact
Toronto-local calendar window, dates, ordering, citation checks, rendering, and
delivery. Qwen receives only bounded text from each selected calendar event and
may produce a cited overview plus an optional substantive description.

The schedule is configured by:

```dotenv
APP_TIMEZONE=America/Toronto
ACADEMIC_MORNING_SCHEDULE=08:00
ACADEMIC_MORNING_CATCHUP_GRACE_MINUTES=30
CALENDAR_SEMANTIC_EVENT_TIMEOUT_SECONDS=180
CALENDAR_SEMANTIC_TOTAL_TIMEOUT_SECONDS=600
CALENDAR_SEMANTIC_PROMPT_MAX_CHARS=16000
```

`ACADEMIC_MORNING_SCHEDULE` is interpreted in `APP_TIMEZONE`. The 30-minute
default grace window allows short startup catch-up and prevents stale late-day
delivery.

Each occurrence uses stable identities:

- run agent: `academic_morning_notification`;
- run schedule: `academic-morning`;
- run idempotency key: `academic-morning:YYYY-MM-DD:HHMM:v1`;
- delivery key prefix: `planner-morning-delivery-v2:YYYY-MM-DD:HHMM:v1`;
- ordered part keys: `planner-morning-delivery-v2:YYYY-MM-DD:HHMM:v1:NNN`;
- health row: `academic_morning`.

The event window starts at midnight on the intended scheduled Toronto-local
date and ends inclusively 10 days and 12 hours later. It applies equally to
active, non-archived course and Jobs/Interviews rows. Completed course rows are
included and marked; date-less rows remain outside the inventory and continue
through existing diagnostics.

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

Inspect the run and its delivery manifest/progress:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, idempotency_key, status, error_code, artifact_key, started_at, finished_at, summary
FROM agent_runs
WHERE agent_name = '\''academic_morning_notification'\''
ORDER BY started_at DESC
LIMIT 10;"'
```

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT node_name, attempt, status, diagnostic, started_at, ended_at
FROM run_steps
WHERE run_id = (
  SELECT id FROM agent_runs
  WHERE agent_name = '\''academic_morning_notification'\''
  ORDER BY started_at DESC LIMIT 1
)
ORDER BY created_at;"'
```

Expected safe phase names include `source_refresh`, `evidence_collection`,
`semantic_interpretation`, `semantic_validation`, `manifest_creation`, and
`delivery`. Diagnostics contain phase/count state only, never event source text,
model prompts, or responses.

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT run_id, channel, target, idempotency_key, status, attempt_count, error_code, external_url
FROM deliveries
WHERE idempotency_key LIKE '\''planner-morning-delivery-v2:%:___'\''
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

The normal morning message requires a fresh complete academic Notion sync
immediately before planning, followed by a Jobs/Interviews sync. Missing Courses
sharing, missing nested Assessments calendars, missing required title/date
properties, partial academic source failure, or stale academic sync must be
reported as an operational condition. A career sync failure is disclosed in the
same message without hiding a valid academic plan.

## Semantic availability and cache

Qwen analyzes one selected event at a time from bounded, event-local textual
properties and supported Notion page-body blocks. It does not receive files,
relations, attachment/OCR content, external posting research, credentials, or
raw vendor envelopes. A separate critic must accept the cited result before an
overview or description is rendered.

A cached result is reusable only when the event fingerprint, Notion edit time,
model identity, model configuration version, and prompt version all match. If
Ollama is unavailable, the deadline expires, output is invalid, or the critic
rejects the repair, the event's trusted title/date metadata still appears and
the briefing reports one aggregate semantic-details condition. Do not recover
by extracting a `Description` property or applying keyword rules.

The complete rendered part manifest is stored before the first Discord send.
On retry, the worker loads that manifest and sends only ordinals not already
recorded as delivered; it does not re-sync sources and repartition content.

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
4. Verify one or more ordered Discord deliveries with corresponding
   `planner-morning-delivery-v2:YYYY-MM-DD:HHMM:v1:NNN` keys and no part over
   2,000 characters.
5. Replay the same period and verify delivered parts are not sent again.
6. Temporarily break Notion sharing or use a mocked/source-failure environment
   and verify the job reports setup/source failure instead of a normal plan.
7. Add course and interview fixtures at, immediately before, and immediately
   after the window endpoint. Verify only in-window metadata renders and a
   completed course row remains visible.
8. Exercise a validated description, a no-description decision, and an Ollama
   failure. Verify Qwen prose appears only for accepted citations and metadata
   survives the failure.
9. Force a failure after part one, replay the same period, and verify delivery
   resumes at part two from the stored manifest.
10. Restore the real Notion sharing and confirm the next period returns healthy.
