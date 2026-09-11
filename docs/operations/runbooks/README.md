# LifeAgent Operations Runbooks

These runbooks cover the Phase 8 operating scenarios for the local
single-Mac Compose deployment. They assume commands are run from the repository
root and that the current stack uses the service names in `compose.yaml`:
`postgres`, `api`, and `worker-academic-planner`.

The current configured runtime includes two native macOS LaunchAgents plus the
Compose API, PostgreSQL, and academic worker. Qwen-powered work is triggered
only by messages from authorized owners in the configured private Discord
channel. The automatic academic morning
notification is scheduled and executable, but it is model-free. Scheduled
academic Qwen, code-review Qwen, and finance Qwen workflows are not executable
runtime paths.

For first-time setup, see [Getting started](../getting-started.md).

For a quick console check, open `http://127.0.0.1:8000/` after the API is
healthy. The shared-services card is backed by the persisted
`health_checks.check_name = 'shared_services'` row. Historical component rows
such as `finance`, `code_review`, and `academic_planner` may still appear in the
database, but they do not imply standalone services can be started.

## Scenario Index

- [Model unavailable or slow](model-unavailable-slow.md)
- [Academic morning notification](academic-morning-notification.md)
- [Discord cold wake](discord-cold-wake.md)
- [Queue backlog](queue-backlog.md)
- [Duplicate delivery](duplicate-delivery.md)
- [Failed database migration](failed-database-migration.md)
- [Source failure](source-failure.md)
- [GitHub rate limit](github-rate-limit.md)
- [Notion ambiguity](notion-ambiguity.md)

## Common Commands

Check the stack:

```bash
docker compose ps
curl --fail http://127.0.0.1:8000/health/ready
docker compose logs --tail=200 api
```

Inspect the latest persisted health rows:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT check_name, state, rule, checked_at, last_success_at, next_due_at, diagnostic
FROM health_checks
ORDER BY checked_at DESC;"'
```

Inspect recent runs, steps, and deliveries:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT id, agent_name, status, error_code, started_at, finished_at, summary
FROM agent_runs
ORDER BY started_at DESC
LIMIT 20;"'
```

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT run_id, node_name, attempt, status, diagnostic, started_at, ended_at
FROM run_steps
ORDER BY created_at DESC
LIMIT 30;"'
```

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT run_id, channel, target, idempotency_key, status, attempt_count, error_code, external_url
FROM deliveries
ORDER BY created_at DESC
LIMIT 30;"'
```
