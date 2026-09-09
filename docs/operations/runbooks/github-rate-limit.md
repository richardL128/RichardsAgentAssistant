# Runbook: GitHub Rate Limit

## Symptom

Code review discovery slows down, skips repositories, or fails with rate-limit
errors. Code review runs show status `Attention` or retry repeatedly. The
operations console's code review card shows `run_overdue` or `waiting_for_retry`
state. API logs may mention `rate_limit` or `CONNECTOR_TRANSIENT` errors with
HTTP 429 status.

This runbook is for historical code-review data and future troubleshooting if a
reviewed architecture re-enables GitHub discovery. The current configured
runtime does not launch a separate GitHub discovery service.

GitHub's API enforces rate limits based on the authenticated app or token.
Discovery against a large organization can exceed the rate limit if the polling
interval is too aggressive.

## Diagnosis

Check code-review runs to see recent failures and error codes:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  id, 
  status, 
  error_code, 
  started_at, 
  finished_at, 
  summary
FROM agent_runs
WHERE agent_name IN ('\''code_review'\'', '\''code_review_daily'\'', '\''code_review_ingest'\'')
ORDER BY started_at DESC
LIMIT 15;"'
```

If the `error_code` column shows `connector_transient` or `rate_limit`, GitHub
is rate-limiting the requests.

Check the repository discovery state to see how many repositories have been
discovered and whether discovery is stalled:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  scope, 
  discovery_version, 
  page, 
  discovered_count, 
  discovery_complete, 
  last_full_name, 
  last_error_code,
  updated_at
FROM repository_discovery_state
ORDER BY updated_at DESC;"'
```

If `discovery_complete` is `false` and `last_error_code` is `rate_limit` or
`connector_transient`, discovery is blocked by rate limiting. The `page` column
shows which page of results was being fetched when the error occurred.

Check the queue to see how many code-review jobs are queued and their status:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  status, 
  COUNT(*) as count, 
  MIN(attempts) as min_attempts,
  MAX(attempts) as max_attempts
FROM procrastinate_jobs
WHERE queue_name = '\''code_review'\''
GROUP BY status
ORDER BY status;"'
```

If many jobs have status `failed` or high attempt counts, rate limiting is
preventing retries from succeeding.

Inspect API logs for rate-limit diagnostics:

```bash
docker compose logs --tail=500 api 2>&1 | grep -i "rate\|429\|transient"
```

Check the GitHub App configuration to confirm credentials are valid. If
authorization fails, the diagnostics will show `CONNECTOR_UNAUTHORIZED`:

```bash
docker compose logs --tail=300 api 2>&1 | grep -i "unauthorized\|forbidden"
```

Check the code-review health check:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  check_name, 
  state, 
  rule, 
  diagnostic, 
  checked_at
FROM health_checks
WHERE check_name = '\''code_review'\''
ORDER BY checked_at DESC
LIMIT 1;"'
```

## Fix

**If rate limiting is the issue:**

GitHub's rate limits are transient and reset on a schedule. Allow the queue's
built-in retry policy to naturally backoff and retry after the rate-limit window
closes. The default retry behavior uses exponential backoff.

If rate limiting is persistent, reduce the discovery call rate by increasing the
minimum interval between API calls or reducing the page size:

```bash
GITHUB_MIN_CALL_INTERVAL_SECONDS=5 GITHUB_DISCOVERY_PAGE_SIZE=25 docker compose up -d api
```

This increases the minimum interval from the default 2 seconds to 5 seconds and
reduces the page size from 50 to 25 repositories per request. These changes slow
discovery but respect rate limits better.

**If authorization is invalid:**

The GitHub App credential has likely expired or been revoked. Update the GitHub
App private key and installation ID outside the repository (they should never be
stored in the repo). Then restart the API to load the new credentials:

```bash
docker compose up -d api
```

Do not replace the GitHub App with a personal access token. The permission
boundary enforced by the GitHub App is part of the security baseline.

**If discovery is fully stuck:**

Check the `repository_discovery_state` table for the current cursor position
(`page`, `last_full_name`). If discovery has not progressed for hours despite
retries, you may need to manually reset the discovery state to retry from the
beginning. This should be done only as a last resort, after confirming the
underlying issue is fixed:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
UPDATE repository_discovery_state 
SET page = 1, last_full_name = NULL, discovery_complete = false
WHERE scope = '\''github-installation'\'';"'
```

After resetting, discovery will restart from page 1 only if a future reviewed
runtime path re-enables it. Do not start a separate GitHub discovery service;
it is not part of the current Compose stack.

## Expected health and Discord behavior

GitHub rate limits are transient. The code-review health check runs every five
minutes and evaluates the most recent run status. If rate limiting is occurring
but the queue is still processing (retries are happening), the health state
becomes `Attention` with a diagnostic message naming the rate limit or transient
error.

Discord alerting sends an attention alert when the health state is non-healthy.
The alert names the code-review component and error code but does not include
repository names, file diffs, commit SHAs, or GitHub tokens.

Once the rate-limit window closes and retries succeed, the health check should
transition to `healthy` at the next five-minute evaluation.

## Verify

Check the discovery state again to confirm it is progressing:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  page, 
  discovered_count, 
  updated_at
FROM repository_discovery_state
WHERE scope = '\''github-installation'\'';"'
```

The `page` and `discovered_count` should be increasing over time. Check the code
review queue to confirm jobs are decreasing:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT status, COUNT(*)
FROM procrastinate_jobs
WHERE queue_name = '\''code_review'\''
GROUP BY status;"'
```

The count of `todo` and `doing` jobs should decrease as they complete. Check
the health state:

```bash
curl http://127.0.0.1:8000/health/ready | jq '.checks[] | select(.name == "code_review")'
```

Once discovery completes and retries stop failing, the code-review health check
should transition to `healthy`.
