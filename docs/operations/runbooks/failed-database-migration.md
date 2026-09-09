# Runbook: Failed Database Migration

## Symptom

The `api` container fails to start or remains in an unhealthy state after
startup. Container logs show `alembic upgrade head` failing with a SQL error or
schema validation error. The `/health/ready` endpoint is unavailable or returns
a failed database schema state.

When the migration fails, the current API plus PostgreSQL runtime is offline.

## Diagnosis

Check the API container status and logs:

```bash
docker compose ps api postgres
docker compose logs --tail=500 api 2>&1 | grep -A 20 -i "error\|migration\|alembic"
```

Look for lines mentioning `alembic`, `SQL`, `constraint`, or `column` errors.

Verify the PostgreSQL database is accepting connections:

```bash
docker compose exec -T postgres sh -lc 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

The response should be `accepting connections`.

Check the Alembic migration history to see which migrations have been applied:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT version_num, installed_on 
FROM alembic_version;"'
```

If the table does not exist, migrations have never run. If the version is lower
than expected, some migrations failed partway through.

Run the migration manually to see the full error message:

```bash
docker compose run --rm api alembic upgrade head 2>&1
```

This command attempts to apply all pending migrations and prints the error if
one fails. Note the migration file name and the specific SQL error.

Inspect the schema state to understand what partially succeeded. For example, if
a column addition failed, check whether the column exists:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT table_name, column_name 
FROM information_schema.columns 
WHERE table_schema = '\''public'\'' 
ORDER BY table_name;"' | head -50
```

## Fix

**Step 1: Stop the API to prevent processing against a partially migrated schema**

```bash
docker compose stop api
```

There are no separate legacy agent services in the current Compose runtime to
stop or restart.

**Step 2: Inspect the failing migration**

Read the migration file that is failing. Migration files are stored in
`app/db/migrations/versions/` and are named by sequence number and description.
Understand what the migration is trying to do and why it might be failing (e.g.,
a unique constraint violation, a non-null constraint on an existing table, a
type mismatch).

**Step 3: Repair or roll back**

If the migration is repairable by adding logic to handle edge cases or updating
the data before applying the schema change, create a fix in the migration file,
then run the upgrade again:

```bash
docker compose run --rm api alembic upgrade head
```

If the migration created partial state that cannot be safely repaired without
data loss, restore from the latest verified backup to get back to a clean
schema state. Create a disposable restore target first:

```bash
createdb lifeagent_restore
scripts/restore_database.sh \
  --backup-file /Users/richardliu/Backups/LifeAgent/lifeagent-YYYYMMDDTHHMMSS-0400.dump.age \
  --target-database-url 'postgresql://lifeagent:lifeagent@localhost:5432/lifeagent_restore' \
  --confirm-target-db lifeagent_restore \
  --dry-run
```

If the dry run succeeds, perform the restore:

```bash
scripts/restore_database.sh \
  --backup-file /Users/richardliu/Backups/LifeAgent/lifeagent-YYYYMMDDTHHMMSS-0400.dump.age \
  --target-database-url 'postgresql://lifeagent:lifeagent@localhost:5432/lifeagent_restore' \
  --confirm-target-db lifeagent_restore
```

Verify the restore succeeded by checking row counts:

```bash
psql 'postgresql://lifeagent:lifeagent@localhost:5432/lifeagent_restore' -c "
SELECT 'agent_runs' as table_name, COUNT(*) as count FROM agent_runs
UNION ALL
SELECT 'audit_events', COUNT(*) FROM audit_events
UNION ALL
SELECT 'deliveries', COUNT(*) FROM deliveries;"
```

After validating the restored database, you can either:

- **Option A:** Fix the failing migration and retry on the original database.
- **Option B:** Switch the application to use the restored database by updating
  the `DATABASE_URL` in the Compose environment, then retry the migration on
  the original database separately.

Do not overwrite the original `lifeagent` database without explicit manual
confirmation and a reviewed restore command.

**Step 4: Restart the API**

Once the migration succeeds, start the API and verify it becomes healthy:

```bash
docker compose up -d api
sleep 10
curl http://127.0.0.1:8000/health/ready | jq '.status'
```

Wait for the status to become `healthy` before treating the runtime as back
online.

## Expected health and Discord behavior

If the API fails to start due to a schema error, the operations console is
inaccessible and in-app health alerting cannot run. There is no Discord alert
for this scenario from the application. Treat the API being down as a
host-level incident that requires manual intervention.

Once the migration succeeds and the API is healthy, all health checks resume
their normal five-minute evaluation cycle.

## Verify

Check the migration history to confirm all migrations applied:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT COUNT(*) as migration_count FROM alembic_version;"'
```

Check the API health endpoint:

```bash
curl http://127.0.0.1:8000/health/ready | jq '.'
```

Database and schema checks should report state `healthy`; Ollama may remain
`attention` if the host-native model server is intentionally offline. Verify all
services are running:

```bash
docker compose ps
```

All services should show `Up` status. For the current architecture this means
`postgres` and `api` only.
