# Runbook: Source Failure

## Symptom

The finance health card shows `Attention` or `Failed` state. A finance run
reports a source failure in its summary or error code. The operations console's
`/settings/sources` page shows the finance source gate as `Attention` or
indicates an incomplete allowlist. Finance runs must never fall back to open web
search; they stop and report the specific source that failed, with a diagnostic
explaining why.

Finance source failures may include: a configured source API endpoint returning
an error, missing or expired authentication credentials, a source record being
disabled in the allowlist, or the source data being stale or malformed.

## Diagnosis

Check the finance allowlist to see which sources are enabled and approved:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  source_id, 
  name, 
  enabled, 
  approved_at,
  entitlement
FROM finance_approved_sources
WHERE allowlist_version = '\''finance-sources-2026.09'\''
ORDER BY source_id;"'
```

Look at the `enabled` column: all sources should be `true` for production use. If
any are `false`, that source is disabled and will cause finance runs to fail
with a gate-violation error.

Check the source health history. Each source gets a health check that records
its last successful fetch and any error diagnostics:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  source_id, 
  source_version,
  status, 
  checked_at, 
  diagnostic
FROM finance_source_health
ORDER BY checked_at DESC
LIMIT 20;"'
```

If any source has status `failed`, its diagnostic explains why (e.g., `HTTP 401
Unauthorized`, `timeout`, `malformed JSON`). Note the `source_id` and diagnostic.

Check the latest finance runs to see which ones failed and why:

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
WHERE agent_name = '\''finance'\''
ORDER BY started_at DESC
LIMIT 10;"'
```

If a run has status `failed`, the `error_code` column names the failure class
(e.g., `source_gate_violation`, `source_fetch_error`). The `summary` may
provide additional detail.

Check the finance health check row to see the aggregated state and rule:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  check_name, 
  state, 
  rule, 
  diagnostic, 
  checked_at
FROM health_checks
WHERE check_name = '\''finance'\''
ORDER BY checked_at DESC
LIMIT 1;"'
```

Inspect worker logs for connector errors without exposing credentials or API
responses:

```bash
docker compose logs --tail=500 worker-finance 2>&1 | grep -i "error\|failed\|exception"
```

Look for lines mentioning specific sources by name or error classes like
`CONNECTOR_TRANSIENT` or `CONNECTOR_UNAUTHORIZED`.

## Fix

**If credentials are missing or expired:**

Update the credential in the host environment or the secrets source used by
Docker Compose. Do not commit credentials to the repository. Then restart the
API and finance worker so they load the new credential:

```bash
docker compose up -d api worker-finance
```

**If a source is disabled in the allowlist:**

Do not flip the `enabled` flag with a manual SQL UPDATE. Each source needs:
- `enabled = true`
- `approved_at` timestamp (when Richard approved the source)
- `approval_audit_id` pointing to an audit event record

Use the reviewed application approval workflow or a deliberate database migration
so the audit trail remains intact. This keeps a record of who approved the source
and when.

**If a vendor API is temporarily unavailable:**

Leave the failed source health row visible and allow the scheduled finance run
to retry automatically. The finance system does not substitute another source or
fallback to web search. The run will fail until the vendor recovers.

**If the source data is stale or malformed:**

Check the vendor's API documentation to understand what changed. If the vendor
deprecated an endpoint or changed the response format, the source adapter in
`app/connectors/finance_sources/` may need an update. After fixing the adapter,
restart the worker and re-queue the finance run.

## Expected health and Discord behavior

If a source fails, the finance health rule becomes `Attention` or `Failed`
depending on whether the run completed with a partial briefing or failed
entirely. The health diagnostic names the failing source and error code.

Discord alerting sends an attention alert mentioning the finance component and
the failing source. The alert does not include the source data, API errors,
credentials, or article excerpts; it provides only the source ID and failure
classification.

The operations console's `/settings/sources` page shows the finance source gate
state. If any source is disabled or unapproved, the gate is incomplete and the
page displays an `Attention` or `Failed` state.

## Verify

Check the allowlist again to confirm all sources are enabled:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT source_id, enabled, approved_at
FROM finance_approved_sources
WHERE allowlist_version = '\''finance-sources-2026.09'\''
AND enabled = false;"'
```

This query should return zero rows. Check the source health status:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT source_id, status
FROM finance_source_health
ORDER BY checked_at DESC
LIMIT 8;"'
```

All sources should have status `healthy`. Verify the finance health check:

```bash
curl http://127.0.0.1:8000/health/ready | jq '.checks[] | select(.name == "finance")'
```

The finance check should have state `healthy`. Open the operations console at
`/settings/sources` and confirm all eight sources show green/healthy status.
