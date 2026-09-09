# Runbook: Source Failure

## Symptom

The finance health card shows `Attention` or `Failed` state. A finance run
reports a source failure in its summary or error code. The operations console's
`/settings/sources` page shows the finance source gate as `Attention` or
indicates an incomplete allowlist. Finance runs must never fall back to open web
search; they stop and report the specific source that failed, with a diagnostic
explaining why.

This runbook is for historical finance data and future troubleshooting if a
reviewed architecture re-enables the finance workflow. The current configured
runtime does not run scheduled finance execution.

Finance source failures may include: a reviewed public endpoint returning an
error, `FINANCE_EIA_MODE=api` without `EIA_API_KEY`, a source record being
disabled in the allowlist, endpoint fan-out or hostname validation rejecting a
request, or source data being stale or malformed.

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
WHERE allowlist_version = '\''finance-sources-2026.09-v2'\''
ORDER BY source_id;"'
```

Use the configured `FINANCE_SOURCE_ALLOWLIST_VERSION` in the query. The default
is `finance-sources-2026.09-v2`. Look at the `enabled` column: all sources should
be `true` for production use. If any are `false`, that source is disabled and
will cause finance runs to fail with a gate-violation error.

Check the reviewed endpoint registry. The page at `/settings/sources` shows the
same endpoint hosts, transport/parser kinds, freshness windows, request ceilings,
scope, and state without rendering credential-bearing URLs:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT
  source_id,
  endpoint_id,
  host,
  transport_kind,
  parser_kind,
  registry_version,
  enabled,
  expected_freshness_seconds,
  request_ceiling,
  ticker_scope,
  cik_scope
FROM finance_source_endpoints
WHERE allowlist_version = '\''finance-sources-2026.09-v2'\''
ORDER BY source_id, endpoint_id;"'
```

Check cache and watermark state when a source looks stale:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT
  source_id,
  endpoint_id,
  watermark_external_id,
  watermark_published_at,
  cached_artifact_key,
  last_retrieved_at,
  last_not_modified_at
FROM finance_source_cache_state
WHERE allowlist_version = '\''finance-sources-2026.09-v2'\''
ORDER BY updated_at DESC
LIMIT 20;"'
```

Confirm that every attempted child request has a metadata-only audit row:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT source_id, endpoint_id, requested_at, status_code, outcome, error_code
FROM finance_source_request_audits
WHERE allowlist_version = '\''finance-sources-2026.09-v2'\''
ORDER BY requested_at DESC
LIMIT 50;"'
```

This audit table intentionally excludes request URLs, headers, credentials, and
response bodies.

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

If any source has status `failed`, its diagnostic explains why (for example,
`timeout`, `malformed JSON`, `malformed XML`, `HTTP 429`, or hostname not
allowlisted). Note the `source_id` and diagnostic.

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

Inspect API logs for connector errors without exposing credentials or API
responses:

```bash
docker compose logs --tail=500 api 2>&1 | grep -i "error\|failed\|exception"
```

Look for lines mentioning specific sources or endpoint IDs and error classes
like `CONNECTOR_TRANSIENT`, `CONNECTOR_UNAUTHORIZED`, `payload_oversized`, or
`not_modified`. Do not paste full payloads, source text, API keys, or private
URLs into tickets or chat.

## Fix

**If EIA API mode is selected without a key:**

Either set `FINANCE_EIA_MODE=bulk` for the public baseline or configure
`EIA_API_KEY` after confirming the EIA API mode is intended. The system selects
bulk or API mode at startup and does not switch modes after a source failure.

**If legacy v1 credentials are missing or expired:**

Update the credential in the host environment or the secrets source used by
Docker Compose. Do not commit credentials to the repository. Then restart the
API so it loads the new credential:

```bash
docker compose up -d api
```

**If a source is disabled in the allowlist:**

Do not flip the `enabled` flag with a manual SQL UPDATE. Each source needs:
- `enabled = true`
- `approved_at` timestamp (when Richard approved the source)
- `approval_audit_id` pointing to an audit event record

Use the reviewed application approval workflow or a deliberate database migration
so the audit trail remains intact. This keeps a record of who approved the source
and when.

**If a reviewed endpoint is temporarily unavailable:**

Leave the failed source health row visible. The finance system does not
substitute another endpoint or fallback to web search. Partial registry failures
remain visible in source health and do not create a ninth logical source call.
In the current runtime, there is no scheduled finance retry; any future retry
path requires an explicit architecture change.

**If the source data is stale or malformed:**

Check the reviewed source documentation or endpoint/licence review notes. If an
official endpoint changed format, the adapter in `app/connectors/finance_sources/`
may need an update. Do not add arbitrary replacement URLs; a substitute endpoint
requires a new reviewed registry/version or migration. After fixing the adapter,
restart the API. Re-queuing finance work is not a current runtime operation.

**If a mapping is missing:**

For the initial v2 registry, SEC and company IR mappings cover LMT only, ETF
holdings cover IVV only, and technology feeds begin with CISA KEV only. Missing
ticker, CIK, issuer, or ETF mappings should stay visible as attention
diagnostics until a reviewed registry update adds the mapping.

## Expected health and Discord behavior

If a source fails, the finance health rule becomes `Attention` or `Failed`
depending on whether the run completed with a partial briefing or failed
entirely. The health diagnostic names the failing source and error code.

Discord alerting sends an attention alert mentioning the finance component and
the failing source. The alert does not include the source data, API errors,
credentials, or article excerpts; it provides only the source ID and failure
classification.

The operations console's `/settings/sources` page shows the finance source gate,
per-source health, reviewed endpoint hosts, transport/parser kinds, expected
freshness, request ceilings, scopes, and endpoint cache/health state. It must
not display API keys, cookies, full source URLs, raw article bodies, or licensed
full text.

## Verify

Check the allowlist again to confirm all sources are enabled:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT source_id, enabled, approved_at
FROM finance_approved_sources
WHERE allowlist_version = '\''finance-sources-2026.09-v2'\''
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

The exceptional v1 rollback documented for the original rollout was a historical
configuration change, not a destructive migration: set
`FINANCE_SOURCE_ALLOWLIST_VERSION=finance-sources-2026.09`, leave v2 audit and
endpoint records in place, and restore any required legacy v1 credentials. V1 is
not the configured default and is not a current executable runtime path.
