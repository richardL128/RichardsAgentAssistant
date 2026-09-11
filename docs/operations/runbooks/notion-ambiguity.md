# Runbook: Notion Ambiguity

## Symptom

The academic planner sends a Discord clarification question to Richard instead
of publishing a completed plan. The clarification asks about ambiguous data such
as a deadline with multiple possible interpretations, a time zone mismatch, or
conflicting weights for a course. The operations console shows the academic
planner card as `Attention` with a `waiting_for_approval` state.

This runbook covers deterministic academic planning and Discord clarification
workflow. Assessment material may improve Discord-triggered guidance, but it never
silently resolves an ambiguous deadline or conflicting weight.

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

Check API logs for clarification questions or approval request messages:

```bash
docker compose logs --tail=300 api 2>&1 | grep -i "ambiguous\|approval\|clarif"
```

Material extraction problems are separate from typed-fact ambiguity. Inspect
only safe status metadata (never raw text or signed URLs):

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT assessment_id, source_kind, extraction_status, extraction_error_code, updated_at
FROM academic_documents
WHERE active = TRUE OR extraction_status IN ('\''failed'\'', '\''partial'\'', '\''ocr_required'\'')
ORDER BY updated_at DESC
LIMIT 50;"'
```

An expired signed URL is recovered by replaying the identifier-only ingestion
job; the worker re-fetches the assessment page before downloading. OCR or
embedding failures retain the previous active document version. Fix the local
dependency, then let Procrastinate retry—do not copy a signed URL into a job or
database row.

## Fix

**To resolve an ambiguity:**

1. Open the Notion page at the `source_url` from the ambiguous assessment or
   commitment.
2. Update the source text so that the deadline, time zone, weight, or other
   ambiguous field has exactly one clear value.
3. Do not manually update the `fact_state` or `ambiguity_reason` in the
   database. The planner's Notion sync process will detect the corrected source
   and re-process the fact.
4. Let the next scheduled sync reprocess the corrected page, or use the scoped
   `/academic/sync` endpoint. Do not edit database fact state manually.

**To respond to an approval request:**

The planner sends clarification questions to Discord. Use the Quiz, Assignment,
Tutorial, Lab, Studying Block, or Ignore button on the relevant message.
LifeAgent changes that message to a queued state immediately, removes its
buttons, and processes the guarded Notion write through the API runtime.
Multiple clarification messages can be queued without one slow Notion request
blocking the next Discord interaction.

If an approval request expires before Richard responds (default TTL: 24 hours),
a future re-enabled planner runtime should ask again for the same clarification
through its reviewed application path.

**If Notion token authentication fails:**

The clarification process cannot continue if the planner cannot read Notion.
Update the `NOTION_TOKEN` and Notion database IDs outside the repository
(they should never be stored in the repo). Then restart the API:

```bash
docker compose up -d api
```

Check the logs to verify the new token works:

```bash
docker compose logs --tail=100 api 2>&1 | grep -i "error\|auth\|notion"
```

## Expected health and Discord behavior

When the planner encounters an ambiguous fact, it asks a clarification question
through Discord instead of making an arbitrary choice. The clarification message
includes the ambiguous field, the source Notion page, and what Richard's response
should be.

This is normal behavior, not a failure. The academic planner health check shows
`Attention` with rule `waiting_for_approval` to indicate the planner is waiting
for input.

LifeAgent edits the original clarification message when the decision is applied,
ignored, conflicts with a newer Notion edit, or fails after retries. Discord
message-edit retries use a separate queued job, so a temporary Discord outage
does not repeat or change the completed Notion write.

Once the queued clarification is applied, the next plan includes the newly
confirmed fact and health returns to `healthy` if all ambiguities are resolved.

## Verify

Confirm the ambiguous facts have been resolved in Notion by reading the page:

```bash
# Use a web browser to open the source_url from the ambiguous fact
# Verify the deadline, time zone, weight, or other field is now unambiguous
```

Confirm the scheduled worker is available:

```bash
docker compose ps worker-academic-planner
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
