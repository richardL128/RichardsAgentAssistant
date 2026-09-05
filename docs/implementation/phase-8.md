# Phase 8 - Reliability, backup, and operating runbooks

## Contract and acceptance criteria

Phase 8 completes the local operating layer for LifeAgent. The deployment must
survive ordinary host restarts, worker termination, connector failures, and
database recovery drills without losing run history, audit history, artifact
references, or delivery idempotency guarantees.

The plan's acceptance tests require that:

1. a disposable PostgreSQL restore from an encrypted host backup preserves
   `agent_runs`, `audit_events`, and artifact references;
2. killing a worker during a non-side-effecting graph step resumes or retries
   safely after restart;
3. killing a worker after a delivery intent is recorded does not create a
   duplicate outbound delivery. In the implemented product surface this means
   Discord deliveries; GitHub remains read-only and inline GitHub comments are
   still out of scope as documented in Phases 3 and 4; and
4. simulated Ollama, PostgreSQL, stalled-worker, Discord, GitHub, and Notion
   failures produce health state and Discord failure-alert behavior matching
   the operations runbooks.

## Implemented

### Database backup and recovery

- Added `scripts/backup_database.sh`: a host-native script that creates
  encrypted PostgreSQL backups using `pg_dump --format=custom` with `age`
  encryption. The script accepts environment overrides for database URL,
  output directory, age recipient, and retention days. Default retention is
  30 daily backups.
- Added `scripts/restore_database.sh`: a safety-checked restore script that
  decrypts an age-encrypted backup and restores it into a target PostgreSQL
  database. The script requires explicit confirmation of the target database
  name and refuses to restore into the current running `lifeagent` database
  unless both `--confirm-target-db lifeagent` and `--allow-current-database`
  are passed.
- Added `scripts/com.richard.lifeagent.backup.plist`: a macOS launchd user
  agent that triggers backups daily at 03:00 America/Toronto time. Logs are
  written to `~/Library/Logs/lifeagent-backup.log` and
  `~/Library/Logs/lifeagent-backup.err.log`.
- Documented backup configuration in `docs/operations/backup-restore.md`:
  recipient and database credential configuration outside the repo at
  `/Users/richardliu/.config/lifeagent/backup.env` and age identity keys at
  `/Users/richardliu/.config/age/keys.txt`.

### Artifact retention

- Added `app/queue/tasks.py` artifact retention periodic task:
  `artifact_retention_periodic` runs every day at 03:30 America/Toronto
  (configured via `artifact_retention_schedule`). The task identifies expired
  artifacts based on the `ARTIFACT_RETENTION_DAYS` setting (default: 30).
- Retention uses `_iter_artifact_sidecars(...)` to enumerate metadata sidecars,
  loads each candidate with `ArtifactStore.get_metadata(...)`, checks expiry
  through `ArtifactStore.retention_candidate(...)`, and then deletes the payload
  plus sidecar when eligible.
- Each prune attempt records `audit_events` through
  `_append_artifact_prune_audit(...)` with `action=artifact.prune` and results
  such as `prune_requested`, `pruned`, or `prune_failed`.

### Connector diagnostics and health checks

- Health checks are implemented in `app/health/checks.py` and evaluate:
  - **Database connectivity and schemas:** `database`, `procrastinate`,
    `shared_schema`, `checkpoints`, `code_review_schema` checks confirm
    PostgreSQL and Procrastinate queue schema health.
  - **Queue depth and failures:** `queue` check reports active job count and
    terminal failure count, becoming `attention` if failures exist.
  - **Artifact storage:** `artifacts` check verifies the artifact root is a
    writable directory.
  - **Ollama model availability:** `ollama` check probes
    `http://host.docker.internal:11434/api/tags`, confirms the configured
    model is installed, and verifies the model's digest matches the configured
    value. State is `attention` (not `failed`) if Ollama is unavailable, because
    Ollama is optional during local bootstrap.
  - **Connector configuration:** `connector_configuration` checks that Discord,
    GitHub, and Notion credentials are either all present or consistently absent,
    preventing partial credential sets from causing runtime errors.
  - **Connector liveness (async):** `check_github_installation_token`,
    `check_discord_authentication`, and `check_notion_authentication` probe each
    connector's API endpoint to verify authentication and basic liveness. These
    run async during the shared-services periodic task.
- All health check names correspond to component names in the operations
  console cards: `shared_services`, `finance`, `code_review`, `academic_planner`.
- Health states are persisted to `health_checks` table with a `check_name`,
  `state` (healthy/attention/failed), `rule` (component-specific rule or
  diagnostic key), and `diagnostic` (human-readable explanation).

### Operations runbooks

- Added seven scenario-specific runbooks under `docs/operations/runbooks/`:
  - `model-unavailable-slow.md`: diagnose and recover from Ollama unavailability,
    missing models, digest mismatches, or performance degradation.
  - `queue-backlog.md`: resolve worker crashes, queue depth buildup, and stalled
    jobs by inspecting heartbeats and restarting workers.
  - `duplicate-delivery.md`: reconcile idempotent delivery intent and prevent
    duplicate Discord messages. GitHub inline comments remain out of scope for
    this implementation.
  - `failed-database-migration.md`: recover from Alembic migration failures by
    restoring from backup or fixing the migration script.
  - `source-failure.md`: handle finance source API failures, disabled sources,
    missing credentials, and stale source data.
  - `github-rate-limit.md`: respond to GitHub API rate limits and authorization
    errors in code review discovery and review runs.
  - `notion-ambiguity.md`: resolve ambiguous academic facts by updating Notion
    source data or answering clarification questions through Discord.
- Each runbook includes: symptom description, diagnosis steps with concrete
  SQL queries and CLI commands, fix procedures, expected health check state
  transitions, and verification steps.
- Runbook index is maintained at `docs/operations/runbooks/README.md` with
  common Compose commands for quick health inspection.

### GitHub Actions CI

- Added `.github/workflows/ci.yml` GitHub Actions workflow:
  - **Quality job:** runs on Ubuntu, performs linting (`ruff check`), format
    checking (`ruff format --check`), and type checking (`pyright`), then runs
    unit and acceptance tests (`pytest tests/unit tests/acceptance`). Installs
    Playwright Chromium for browser-based acceptance tests. Runs on `push` and
    `pull_request` to `main`.
  - **Integration job:** runs on Ubuntu with a real `postgres:16.4-bookworm`
    service. Runs integration tests (`pytest tests/integration`) with stubbed
    Ollama, Discord, GitHub, and Notion APIs. Database URL is configured to
    use the local PostgreSQL service. Runs on `push` and `pull_request` to
    `main`.
  - **Docker acceptance job:** is opt-in through `workflow_dispatch` with
    `run_docker_acceptance=true` and runs
    `tests/integration/test_phase8_reliability.py` against Docker Compose.

### Docker Compose reliability baseline

- `compose.yaml` health checks and restart policies:
  - `postgres` service has `healthcheck` with `pg_isready` command, 5-second
    interval, 5-second timeout, 12 retries, and 5-second start period. Uses
    `restart: unless-stopped` so Docker restarts the container if it exits.
  - `api` service has `healthcheck` with a Python HTTP client probing
    `/health/live` endpoint, 10-second interval, 5-second timeout, 6 retries,
    and 10-second start period. `depends_on` API with `condition:
    service_healthy` ensures the API waits for PostgreSQL before starting.
    Uses `restart: unless-stopped`.
  - Worker services (`worker-code-review`, `worker-academic-planner`,
    `worker-finance`) depend on a healthy API. Use `restart: unless-stopped`.
  - Worker entrypoints call `/usr/local/bin/lifeagent-idle-worker` which
    `exec`s into the Procrastinate queue worker. This allows `SIGTERM` signals
    from Docker `stop` to reach the queue worker directly, enabling graceful
    shutdown and job resumption on restart.
- Artifact and PostgreSQL data are persisted in named volumes.

### Documentation and configuration

- Backup restore procedure documented in `docs/operations/backup-restore.md`
  including launchd installation, manual backup/restore commands, and restore
  drill examples.
- Decisions confirmed by Richard on 2026-09-05: local `age`-encrypted backups,
  a daily 03:00 launchd schedule, GitHub Actions CI with a real PostgreSQL
  service, and one Markdown runbook per scenario under
  `docs/operations/runbooks/`. Recipient configuration remains outside the
  repo, retention is 30 days, and CI uses stub external services.

## Acceptance evidence

Phase 8 closeout produced the following evidence:

1. **Backup/restore integration:** The gated test was invoked with
   `LIFEAGENT_RUN_DOCKER_ACCEPTANCE=1`, but this host lacks `age`, `age-keygen`,
   `pg_dump`, and `pg_restore`, so the disposable encrypted restore drill could
   not run here. Unit coverage verifies script arguments and restore safety
   checks; the real round trip remains deferred below.

2. **Worker resilience:** The isolated Docker Compose acceptance test passed.
   It verifies that the worker entrypoint `exec`s into Procrastinate, kills the
   worker during a non-side-effecting shared-services task, restarts it, and
   observes the expected recovered health result.

3. **Delivery idempotency:** The isolated Docker Compose SIGKILL acceptance
   test passed. It kills the worker after the durable intent and stub Discord
   acceptance but before receipt persistence, restarts the worker, and verifies
   repeated POST attempts use the same enforced nonce. The final database state
   is one `sent` delivery with one recorded attempt, while the Discord stub
   observes one logical message. GitHub inline comments remain
   unimplemented/read-only by design.

4. **Health state transitions:** Simulated failures (Ollama unavailable,
   PostgreSQL down, stalled workers, Discord/GitHub/Notion API errors) causes
   the appropriate health checks to transition to `attention` or `failed`.
   Discord alerting sends an idempotent durable alert when policy requires it,
   using the component name, state, error code, retry count, and run id. When
   the underlying issue is fixed, the next health evaluation transitions the
   check back to `healthy`.

5. **CI coverage:** The quality job now installs Node 22.14.0 and builds the
   gitignored Tailwind and HTMX assets before Playwright tests. A closeout run
   began with both generated assets absent, rebuilt them successfully, and
   passed all 300 non-browser unit/acceptance cases. Chromium launch itself is
   blocked by the managed macOS sandbox (`bootstrap_check_in` permission
   denied), while the same browser test had already passed locally after the
   identical asset build. Repository validation otherwise reports clean Ruff,
   format, and Pyright checks and 324 passing non-browser tests. The normal
   integration suite uses its real disposable PostgreSQL service.

The full gated reliability run completed with 11 passing tests and the one
backup/restore prerequisite failure described above. The two destructive
Compose worker-kill cases both passed and cleaned up their isolated containers
and volumes.

## Deferred external verification

- Live backup scheduling requires Richard to install the documented launchd
  entry on the Mac host, configure database and age recipient values in
  `/Users/richardliu/.config/lifeagent/backup.env`, and configure the age
  identity documented under `/Users/richardliu/.config/age/keys.txt`.
- The disposable encrypted backup/restore acceptance drill must be rerun after
  installing `age`, `age-keygen`, `pg_dump`, and `pg_restore` on the host. This
  closeout does not claim that drill passed.
- Live Discord, GitHub, and Notion authentication checks require real
  credentials (bot token, GitHub App private key, Notion API token) in the
  deployment environment. Automated tests use stubbed responses and assert that
  no credentials are persisted in logs or health diagnostics.
- Host-native Ollama is intentionally not started in CI. CI stubs model
  availability at a stub endpoint and leaves performance validation to local
  Compose runs.
- A live recovery drill on Richard's running system remains required after the
  launchd and credential setup: create an encrypted backup, restore it into a
  disposable database, and perform worker termination/restart under realistic
  load to validate the documented recovery objectives. The passing automated
  Compose tests used isolated projects and volumes, never the live dev stack.

## Final phase status

Phase 8 is the last planned implementation phase. Once the acceptance tests
above pass in the repository-level validation and Richard commits the phase,
the planned LifeAgent implementation is complete.
