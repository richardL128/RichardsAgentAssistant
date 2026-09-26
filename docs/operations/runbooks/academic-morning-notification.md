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
agenda schedule. Host code owns source refresh, Toronto-local day boundaries,
coverage, dates, links, ordering, rendering, and delivery. Qwen receives bounded
text from each selected event and produces a grounded event overview plus an
optional description. A second critic-checked stage composes one bounded spoken
task phrase per eligible Courses, Jobs, and Misc item; the schedule table uses
separate bounded field inference. Host code adds authoritative dates and links,
preserves accepted phrases independently, and falls back to the trusted title
when an individual phrase is unavailable. Model stages use one repair attempt.

The schedule is configured by:

```dotenv
APP_TIMEZONE=America/Toronto
ACADEMIC_MORNING_SCHEDULE=08:00
ACADEMIC_MORNING_CATCHUP_GRACE_MINUTES=30
ACADEMIC_SCHEDULE_ICAL_URL=https://calendar.google.com/calendar/ical/.../private-.../basic.ics
ACADEMIC_SCHEDULE_ICAL_TIMEOUT_SECONDS=10
ACADEMIC_SCHEDULE_ICAL_MAX_BYTES=1048576
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
- delivery key prefix: `planner-morning-four-v3:YYYY-MM-DD:HHMM:v1`;
- category keys: `planner-morning-four-v3:YYYY-MM-DD:HHMM:v1:<category>:v1`;
- health row: `academic_morning`.

The course window starts at midnight on the intended Toronto-local date and
contains today plus the following seven local dates. Jobs, misc, and the exact
reserved `Classes + Tutorials + Labs` calendar use interval overlap with today.
Completed rows are excluded. Every active real course appears once, including
courses with no selected work; reserved `misc` and schedule rows are not real
courses. The schedule row is a category marker only; its events come from the
secret, read-only Google iCal feed rather than a seeded Notion database.

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

Expected safe phase names include `source_refresh`,
`event_001.evidence_collection`, `event_001.semantic_interpretation`,
`event_001.semantic_validation`, `spoken_composition.courses`,
`spoken_composition.jobs`, `spoken_composition.misc`, `manifest_creation`, and
`delivery`, with the event ordinal increasing per selected event. Diagnostics
contain phase/count state, semantic status, and safe error codes only, never
event source text, model prompts, or responses.

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT run_id, channel, target, idempotency_key, status, attempt_count, error_code, external_url
FROM deliveries
WHERE idempotency_key LIKE '\''planner-morning-four-v3:%:%:v1'\''
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

The normal morning messages require fresh Notion and Google iCal syncs
immediately before rendering. Missing sharing, invalid required properties,
an inaccessible or malformed iCal feed, partial source failure, or stale data
is reported inside the affected category's unavailable embed.
Independently fresh categories still render; stale cached rows never do.

## Semantic availability and cache

Qwen analyzes one selected event at a time from bounded, event-local textual
properties, supported Notion page-body blocks, or bounded Google iCal
description/location fields. It does not receive files,
relations, attachment/OCR content, external posting research, credentials, or
raw vendor envelopes. The normalized title is always a citable evidence fragment.
For course work, the title alone can support a valid overview; an empty page
body means there are no extra details, not that there is no work. The model does
not own due dates, times, urgency, or completion state. Those facts are rendered
after semantic interpretation from synchronized host data.
The critic validates activity intent independently from overview/description, so
one rejected component does not erase another supported component.

A cached event-semantic result is reusable only when the event fingerprint,
Notion edit time, model identity, model configuration version, and prompt
version all match. If Ollama is unavailable, the deadline expires, output is
invalid, or the critic rejects the repair, every selected course event still
appears with its trusted title and host-owned date; it must not become a quiet
course. Spoken task
composition records accepted and degraded items separately so one rejected
phrase does not erase accepted phrases for other events. Do not recover by
guessing schedule fields or applying keyword rules.

The grounded morning-summary migration removes the legacy
`not_substantive` runtime status. Rows with that legacy semantic/cache payload
are cleared so the next fresh run recomputes them under the current prompt
version. The rollout advances generation to `calendar-event-semantics-v4` and
the critic to `calendar-event-semantics-critic-v3`, so matching older `valid`
cache rows are also recomputed once through normal prompt-version mismatch. A
`valid` event may have an overview, `description_present=false`, and no
description.

The complete versioned four-embed manifest is stored before the first Discord
send. On retry, the worker loads that manifest and sends only categories whose
ordinals are not already recorded as delivered.

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
4. Verify exactly four Discord embed deliveries in category order with
   `planner-morning-four-v3:YYYY-MM-DD:HHMM:v1:<category>:v1` keys, titles at
   most 256 characters, descriptions at most 4,096, and mentions disabled.
5. Replay the same period and verify delivered categories are not sent again.
6. Temporarily break Notion sharing and separately reject the iCal request in a
   mocked/source-failure environment
   and verify the affected category is an unavailable embed, fresh independent
   categories still render, and no stale rows appear.
7. Add course and interview fixtures at Toronto-local boundaries and across a
   DST transition. Verify interval overlap, the seven-following-days course
   horizon, and completed-item exclusion.
8. Exercise a validated description, a title-only valid overview with no
   description, one rejected spoken phrase, and an Ollama failure. Verify Qwen
   prose appears only for accepted citations, the rejected item falls back to
   its trusted linked title, every course event includes its host-owned date,
   and independently valid phrases survive the failure.
9. Force a failure after one category, replay the same period, and verify
   delivery resumes at the next category from the stored manifest.
10. Restore the real Notion sharing and iCal source, then confirm the next
    period returns healthy.
