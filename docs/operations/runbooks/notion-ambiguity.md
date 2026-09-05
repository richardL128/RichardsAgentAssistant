# Runbook: Notion Ambiguity

## Symptom

The academic planner sends a Discord clarification question to Richard instead
of publishing a completed plan. The clarification asks about ambiguous data such
as a deadline with multiple possible interpretations, a time zone mismatch, or
conflicting weights for a course. The operations console shows the academic
planner card as `Attention` with a `waiting_for_approval` state.

Ambiguous facts must not automatically become hard schedule constraints. The
planner identifies ambiguous data in Notion, asks for clarification through
Discord, and waits for Richard's response before finalizing the schedule.

## Diagnosis

Query assessments (assignments, quizzes, exams) that are in the `ambiguous`
state:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  id, 
  course_id, 
  title, 
  due_at, 
  fact_state, 
  ambiguity_reason, 
  source_url,
  updated_at
FROM assessments
WHERE fact_state = '\''ambiguous'\''
ORDER BY updated_at DESC;"'
```

The `ambiguity_reason` column explains what is ambiguous (e.g., `multiple
deadlines in the source`, `no time zone specified`, `conflicting due dates`).
The `source_url` points to the Notion page where the ambiguity exists.

Query fixed time commitments (events, office hours, workshops) that are
ambiguous:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  id, 
  course_id, 
  title, 
  starts_at, 
  ends_at, 
  fact_state, 
  ambiguity_reason, 
  source_url,
  updated_at
FROM fixed_commitments
WHERE fact_state = '\''ambiguous'\''
ORDER BY updated_at DESC;"'
```

Inspect pending approvals waiting for Richard's clarification:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  id, 
  operation, 
  state, 
  expires_at, 
  redacted_preview
FROM approval_requests
WHERE state = '\''pending'\''
ORDER BY created_at DESC;"'
```

The `redacted_preview` column shows a safe summary of what is being asked (names
and courses are included, but sensitive data is redacted). The `expires_at`
timestamp shows when the approval request expires.

Check the Discord messages sent by the academic planner to see clarification
questions:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  run_id, 
  idempotency_key, 
  status, 
  error_code, 
  external_url
FROM deliveries
WHERE channel = '\''discord'\''
  AND idempotency_key LIKE '\''academic-%'\''
ORDER BY created_at DESC
LIMIT 20;"'
```

Check the academic planner health check to see the current state:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  check_name, 
  state, 
  rule, 
  diagnostic, 
  checked_at
FROM health_checks
WHERE check_name = '\''academic_planner'\''
ORDER BY checked_at DESC
LIMIT 1;"'
```

Check worker logs for clarification questions or approval request messages:

```bash
docker compose logs --tail=300 worker-academic-planner 2>&1 | grep -i "ambiguous\|approval\|clarif"
```

## Fix

**To resolve an ambiguity:**

1. Open the Notion page at the `source_url` from the ambiguous assessment or
   commitment.
2. Update the source text so that the deadline, time zone, weight, or other
   ambiguous field has exactly one clear value.
3. Do not manually update the `fact_state` or `ambiguity_reason` in the
   database. The planner's Notion sync process will detect the corrected source
   and re-process the fact.
4. Trigger a new academic planner run to pick up the corrected data. This
   happens automatically at the next scheduled time (default: 08:00 and 21:00
   America/Toronto) or can be done manually by enqueueing a new run through the
   reviewed application path.

**To respond to an approval request:**

The planner sends clarification questions to Discord. Read the question and
reply with the disambiguated value. The planner's approval processor watches for
Richard's response and records the approval. The next plan excludes the still-ambiguous
facts but includes newly confirmed facts.

If an approval request expires before Richard responds (default TTL: 24 hours),
the next academic planner run will ask again for the same clarification.

**If Notion token authentication fails:**

The clarification process cannot continue if the planner cannot read Notion.
Update the `NOTION_TOKEN` and Notion database IDs outside the repository
(they should never be stored in the repo). Then restart the API and worker:

```bash
docker compose up -d api worker-academic-planner
```

Check the logs to verify the new token works:

```bash
docker compose logs --tail=100 worker-academic-planner 2>&1 | grep -i "error\|auth\|notion"
```

## Expected health and Discord behavior

When the planner encounters an ambiguous fact, it asks a clarification question
through Discord instead of making an arbitrary choice. The clarification message
includes the ambiguous field, the source Notion page, and what Richard's response
should be.

This is normal behavior, not a failure. The academic planner health check shows
`Attention` with rule `waiting_for_approval` to indicate the planner is waiting
for input.

Discord alerting does not send a failure alert for clarifications. An alert is
only sent if the planner's processing itself fails due to a connector error (e.g.,
Notion API unavailable) or a database error. In that case, the health rule would
be something like `connector_unauthorized` or `processing_failed`, not
`waiting_for_approval`.

Once Richard answers the clarification and the next planner run executes, the
health state transitions to `healthy` if all ambiguities are resolved.

## Verify

Confirm the ambiguous facts have been resolved in Notion by reading the page:

```bash
# Use a web browser to open the source_url from the ambiguous fact
# Verify the deadline, time zone, weight, or other field is now unambiguous
```

Trigger a new academic planner run to process the corrected data:

```bash
# Either wait for the next scheduled time (08:00 or 21:00 America/Toronto)
# or manually enqueue a new run through the application API
```

After the new run completes, check the academic planner health:

```bash
curl http://127.0.0.1:8000/health/ready | jq '.checks[] | select(.name == "academic_planner")'
```

The health state should be `healthy` and the rule should no longer be
`waiting_for_approval`. Verify the plan published to Discord includes only
confirmed facts:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT COUNT(*) as ambiguous_count
FROM assessments
WHERE fact_state = '\''ambiguous'\'';"'
```

This query should return zero rows for a healthy state.
