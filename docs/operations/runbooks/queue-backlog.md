# Runbook: Queue Backlog

## Symptom

The operations console shows the shared-services card as `Attention` or `Failed`
with a diagnostic message mentioning `queue`. Recent activity entries show runs
in `Queued` or `Running` state that are not progressing. The queue health check
diagnostic reports high depth (`queue depth N`) or terminal failures (`terminal
failures M`). A stalled worker appears as a `procrastinate_job` with status
`doing` whose corresponding worker's `last_heartbeat` timestamp is older than
the `QUEUE_STALLED_AFTER_SECONDS` threshold (default: 120 seconds).

When the queue is backlogged, finance briefings, code reviews, and academic
plans wait in the queue without being processed.

## Diagnosis

Check the status of all worker containers first:

```bash
docker compose ps worker-code-review worker-academic-planner worker-finance
```

Look for container state: if you see `Exit` or `Exited`, the worker crashed. If
you see `Up`, the container is running. If you see `Paused`, it is stopped by
the Compose orchestrator.

Check recent worker logs for crash messages or errors:

```bash
docker compose logs --tail=100 worker-code-review
docker compose logs --tail=100 worker-academic-planner
docker compose logs --tail=100 worker-finance
```

Look for `ERROR`, `Exception`, or `Traceback` messages that indicate why the
worker stopped.

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

Restart one or all workers depending on the failure mode. If a specific worker
container has crashed, restart it:

```bash
docker compose restart worker-code-review
docker compose restart worker-academic-planner
docker compose restart worker-finance
```

If the API is healthy but no workers are running, bring them all up:

```bash
docker compose up -d worker-code-review worker-academic-planner worker-finance
```

Do not manually delete `procrastinate_jobs` rows to clear a queue backlog.
Deleting rows erases the job definition and retry history without creating an
audit record. The queue is the source of truth for scheduling and delivery
guarantees. If a job is permanently invalid after you have inspected the
corresponding run and determined no recovery is possible, create a deliberate
remediation record through application code or a reviewed database migration.

## Expected health and Discord behavior

Once workers restart and begin processing jobs, the queue depth should decrease
over time. The queue health check evaluates every five minutes and reports the
count of active jobs and terminal failures. A backlog of `todo` jobs with no
worker assigned is normal; jobs become `doing` as workers pick them up.

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
docker compose ps worker-code-review worker-academic-planner worker-finance
```

Confirm all workers show `Up` status. Then check queue depth:

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
