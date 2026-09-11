# Runbook: Queue Backlog

## Symptom

The operations console shows the shared-services card as `Attention` or `Failed`
with a diagnostic message mentioning `queue`. Recent activity entries show runs
in `Queued` or `Running` state that are not progressing. The queue health check
diagnostic reports high depth (`queue depth N`) or terminal failures (`terminal
failures M`). A stalled worker appears as a `procrastinate_job` with status
`doing` whose corresponding Procrastinate heartbeat timestamp is older than the
`QUEUE_STALLED_AFTER_SECONDS` threshold (default: 120 seconds).

When the queue is backlogged, current Discord-triggered API work can stop
progressing. The scheduled model-free academic morning notification can also be
delayed. Scheduled finance, code-review, and academic Qwen/model paths are not
executable in the current architecture.

## Diagnosis

Check the status of the current runtime containers first:

```bash
docker compose ps api postgres
```

Look for container state: if the API shows `Exit` or `Exited`, the runtime
crashed. If it shows `Up`, the API is running. If it shows `Paused`, it is
stopped by the Compose orchestrator.

Check recent API logs for queue crash messages or errors:

```bash
docker compose logs --tail=200 api
```

Look for `ERROR`, `Exception`, or `Traceback` messages that indicate why the
queue processor stopped.

Inspect the queue depth and identify stalled workers. The query shows all active
and stuck jobs with their worker heartbeat timestamps:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT
  jobs.id,
  jobs.queue_name,
  jobs.task_name,
  jobs.status,
  jobs.attempts,
  jobs.scheduled_at,
  workers.id as worker_id,
  EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - workers.last_heartbeat)) as heartbeat_age_seconds
FROM procrastinate_jobs AS jobs
LEFT JOIN procrastinate_workers AS workers ON workers.id = jobs.worker_id
WHERE jobs.status IN ('\''todo'\'', '\''doing'\'')
ORDER BY jobs.id;"'
```

If a job has status `doing` and `heartbeat_age_seconds` is greater than 120,
that worker is stalled. Inspect the corresponding run to understand what was
being processed:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT run_id, node_name, attempt, status, diagnostic, started_at, ended_at
FROM run_steps
WHERE run_id = '\''<RUN_ID_FROM_STALLED_JOB>'\''
ORDER BY created_at DESC;"'
```

Count the total queue depth by status to confirm backlog size:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT status, queue_name, COUNT(*) as count
FROM procrastinate_jobs
GROUP BY status, queue_name
ORDER BY queue_name, status;"'
```

If there are terminal failures, count them:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT status, COUNT(*) as count
FROM procrastinate_jobs
WHERE status IN ('\''failed'\'', '\''aborted'\'')
GROUP BY status;"'
```

## Fix

Restart the API if the embedded queue processor has stopped or the API
container crashed:

```bash
docker compose restart api worker-academic-planner
```

If the API was down, bring up the current runtime services:

```bash
docker compose up -d postgres api worker-academic-planner
```

Do not start legacy code-review or finance model workers; the academic worker
runs only durable Discord academic jobs, the scheduled model-free academic
morning notification, and model-free/ingestion work. There is no configured
scheduled model worker.

Do not manually delete `procrastinate_jobs` rows to clear a queue backlog.
Deleting rows erases the job definition and retry history without creating an
audit record. The queue is the source of truth for scheduling and delivery
guarantees. If a job is permanently invalid after you have inspected the
corresponding run and determined no recovery is possible, create a deliberate
remediation record through application code or a reviewed database migration.

### Obsolete retired scheduled jobs

During the morning-notification rollout, old failed scheduled-job rows may
remain from retired architectures. Preserve those rows, their Procrastinate
event history, and the audit trail. Never delete them and never cancel current
`lifeagent.academic_morning_notification` jobs.

For the retired academic planner schedule, inspect only
`lifeagent.academic_planner` rows:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, queue_name, task_name, status, attempts, scheduled_at
FROM procrastinate_jobs
WHERE task_name = '\''lifeagent.academic_planner'\''
  AND queue_name = '\''academic_planner'\''
  AND status IN ('\''failed'\'', '\''aborted'\'')
ORDER BY scheduled_at, id;"'
```

For the retired scheduled code-review job, inspect only
`lifeagent.code_review_daily` rows on the retired `code_review` queue:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, queue_name, task_name, status, attempts, scheduled_at
FROM procrastinate_jobs
WHERE task_name = '\''lifeagent.code_review_daily'\''
  AND queue_name = '\''code_review'\''
  AND status IN ('\''failed'\'', '\''aborted'\'')
ORDER BY scheduled_at, id;"'
```

If every returned row is verified obsolete and failed, paste only those exact
job IDs into the transaction below and set the matching task, queue, and audit
action. It locks each selected row, verifies it still matches the retired failed
task, marks it `cancelled`, and appends an audit event per job in one
transaction.

```bash
docker compose exec -T api python - <<'PY'
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import AuditEvent
from app.db.session import Database

verified_job_ids = [
    123,  # replace with inspected failed/aborted job IDs
]
expected_task_name = "lifeagent.academic_planner"
expected_queue_name = "academic_planner"
audit_action = "queue.legacy_academic_planner_cancelled"

# For retired code-review cleanup, use:
# expected_task_name = "lifeagent.code_review_daily"
# expected_queue_name = "code_review"
# audit_action = "queue.legacy_code_review_cancelled"

database = Database(get_settings())
try:
    with Session(database.engine) as session, session.begin():
        for job_id in verified_job_ids:
            row = session.execute(
                text(
                    """
                    SELECT id, queue_name, task_name, status
                    FROM procrastinate_jobs
                    WHERE id = :job_id
                    FOR UPDATE
                    """
                ),
                {"job_id": job_id},
            ).mappings().one_or_none()
            if (
                row is None
                or row["task_name"] != expected_task_name
                or row["queue_name"] != expected_queue_name
                or row["status"] not in {"failed", "aborted"}
            ):
                raise RuntimeError(f"job {job_id} is not a verified failed retired scheduled job")

            session.execute(
                text("UPDATE procrastinate_jobs SET status = 'cancelled' WHERE id = :job_id"),
                {"job_id": job_id},
            )
            session.add(
                AuditEvent(
                    actor="operator",
                    action=audit_action,
                    target_type="procrastinate_job",
                    target_id=str(job_id),
                    result="cancelled",
                )
            )
finally:
    database.dispose()
PY
```

Then verify the cleanup did not touch current morning-notification jobs and
changed only the retired task names:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT task_name, status, COUNT(*) AS count
FROM procrastinate_jobs
WHERE task_name IN (
  '\''lifeagent.academic_planner'\'',
  '\''lifeagent.code_review_daily'\'',
  '\''lifeagent.academic_morning_notification'\'',
  '\''lifeagent.schedule.academic_morning_notification'\''
)
GROUP BY task_name, status
ORDER BY task_name, status;"'
```

For `lifeagent.academic_material_ingestion`, job arguments must contain only
`assessment_page_id` and `source_fingerprint`. A 403 is normally an expired
Notion URL and is recovered by the job's fresh page read. Repeated OCR failures
require `tesseract --version` inside the academic worker; repeated embedding
failures require the configured local embedding model to appear in Ollama's
`/api/tags`. Last-good active chunks remain available while a newer version is
pending or failed.

## Expected health and Discord behavior

Once the API runtime resumes queue processing, the queue depth should decrease
over time. The queue health check evaluates every five minutes and reports the
count of active jobs and terminal failures. A backlog of `todo` jobs with no
Procrastinate worker assigned is normal; jobs become `doing` as processing
claims them.

If terminal failures are present, the queue health state becomes `attention`
until those jobs are resolved. The health check records:

```
queue depth N; terminal failures M
```

If jobs remain stuck (status `doing` with stale heartbeats) and queue depth is
not decreasing, Discord alerting sends an attention alert naming the `queue`
component with its diagnostic message.

The alert does not include job arguments, model outputs, or run details; it
provides only the queue depth and failure count.

## Verify

```bash
docker compose ps api postgres worker-academic-planner
```

Confirm both services show `Up` status. Then check queue depth:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT status, COUNT(*) as count
FROM procrastinate_jobs
GROUP BY status;"'
```

Queue depth should be decreasing. The majority of jobs should be in `succeeded`
status (completed) rather than `todo` or `doing`. Check the health state:

```bash
curl http://127.0.0.1:8000/health/ready | jq '.status'
```

Once all jobs complete and terminal failures are zero, the queue health check
should transition to `healthy` at the next five-minute evaluation window.
