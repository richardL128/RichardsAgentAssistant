# Runbook: Model Unavailable Or Slow

## Symptom

The operations console shows the shared-services health card as `Attention`
with `ollama=attention`. The `/health/ready` readiness endpoint returns
`attention` with a diagnostic message such as `Ollama unavailable`, `configured
Ollama model is not installed`, or `configured Ollama model digest does not
match`.

When the model is unavailable, agents cannot generate code review feedback,
academic plans, or finance briefings. Active queue jobs may pile up in `doing`
status while waiting for model calls to succeed.

## Diagnosis

Check the API readiness endpoint to confirm the health state:

```bash
curl http://127.0.0.1:8000/health/ready | jq .
```

Look for `status: "attention"` or `status: "failed"` and the corresponding
`ollama` diagnostic message in the `checks` array.

Verify Ollama is running on the macOS host:

```bash
ollama ps
ollama list
```

If Ollama is running, check whether it can respond to requests:

```bash
curl http://127.0.0.1:11434/api/tags | jq '.models[] | {name, digest}'
```

Verify that Docker containers can reach the Ollama endpoint. Run a test from
inside the API container:

```bash
docker compose exec api python -c "
import httpx
try:
    response = httpx.get('http://host.docker.internal:11434/api/tags', timeout=5)
    print(f'Status: {response.status_code}')
    print(response.json())
except Exception as e:
    print(f'Error: {e}')
"
```

Check the procrastinate queue to see if jobs are accumulating while waiting for
model responses. A growing count of `doing` jobs with old timestamps suggests
model processing is stalled:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT status, COUNT(*) as count
FROM procrastinate_jobs
GROUP BY status
ORDER BY status;"'
```

Look at the `shared_services` health check row in the database to see what the
health system recorded:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT check_name, state, rule, diagnostic, checked_at
FROM health_checks
WHERE check_name = '\''shared_services'\''
ORDER BY checked_at DESC
LIMIT 1;"'
```

## Fix

Start Ollama on the host if it is not running:

```bash
ollama serve
```

If Ollama is running but the model is missing, download the configured model
using the exact name specified in the environment or `compose.yaml` (default:
`qwen3-32gb:latest`):

```bash
ollama pull qwen3-32gb:latest
```

If the model is present but responding slowly, reduce concurrency to one worker
and one Ollama process to avoid contention:

```bash
WORKER_CONCURRENCY=1 OLLAMA_MAX_CONCURRENCY=1 docker compose up -d api worker-code-review worker-academic-planner worker-finance
```

If you intentionally changed the model version and the digest now differs from
the configured value, update both `OLLAMA_MODEL` and `OLLAMA_MODEL_DIGEST` in
the deployment environment together after benchmarking the new model's
performance.

## Expected health and Discord behavior

The shared-services periodic task runs every five minutes. After you fix Ollama
or restore the model, the next periodic run should record the ollama check as
`healthy` and update the shared-services health state accordingly.

If the shared-services health state transitions to `healthy`, the health row's
`rule` field will reflect `ollama=healthy` alongside any other component states.
The operations console card will show `Healthy` status.

An Ollama-unreachable `attention` state is UI health only. It should not send a
Discord failure alert by itself. The shared-services alert policy sends an
alert only when overall shared-services health is `failed`, when an agent health
rule is `run_overdue` or `waiting_for_retry`, or when the queue check is
`attention` because work is failing or stalled. If one of those escalation
conditions is present and Ollama is also unhealthy, the alert uses the
allowlisted failure-alert payload: component, state, error code, retry count,
and run ID only. It must not include model outputs, prompts, or other sensitive
content.

## Verify

```bash
curl http://127.0.0.1:8000/health/ready | jq '.status'
```

Confirm the response is `"healthy"` or, if another non-model check is still
degraded, confirm the `ollama` diagnostic is no longer the cause. Then verify
the latest health check:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT state, diagnostic
FROM health_checks
WHERE check_name = '\''shared_services'\''
ORDER BY checked_at DESC
LIMIT 1;"'
```

The diagnostic should no longer mention `ollama=attention`.

If the row still shows only `ollama=attention`, verify that no delivery was
created for a shared-services alert:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, idempotency_key, status, error_code
FROM deliveries
WHERE idempotency_key LIKE '\''shared-services-alert:%'\''
ORDER BY created_at DESC
LIMIT 10;"'
```

No new alert delivery should appear for an Ollama-only attention state.
