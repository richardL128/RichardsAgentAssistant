# Runbook: Duplicate Delivery

## Symptom

A Discord message or other implemented external delivery appears twice for the
same event. The operations detail page shows multiple delivery rows for the same
run with the same idempotency key. A delivery status is recorded as `uncertain`
or `pending` after the delivery timeout expires, indicating the confirmation was
never received.

Duplicate deliveries violate idempotency guarantees: the same event should
result in exactly one outbound message, not multiple.

## Diagnosis

Query for delivery rows that share the same channel and idempotency key. Only
one row per channel/key pair should exist:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  channel, 
  idempotency_key, 
  COUNT(*) as duplicate_count, 
  array_agg(status) as statuses,
  array_agg(external_url) as urls
FROM deliveries
GROUP BY channel, idempotency_key
HAVING COUNT(*) > 1
ORDER BY duplicate_count DESC;"'
```

If the query returns rows, those are duplicates. Note the `channel`,
`idempotency_key`, and the `statuses` array. If any status is `sent`, the
provider likely received at least one delivery.

Inspect all incomplete or failed deliveries to identify uncertain sends:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 
  id, 
  run_id, 
  channel, 
  idempotency_key, 
  status, 
  attempt_count, 
  last_attempt_at, 
  error_code, 
  external_url
FROM deliveries
WHERE status IN ('\''pending'\'', '\''sending'\'', '\''uncertain'\'', '\''failed'\'')
ORDER BY created_at DESC
LIMIT 30;"'
```

For each uncertain or failed delivery, reconcile with the provider. For current
Discord deliveries, manually check the target channel to see if a message exists
at the `external_url` ID. Historical finance or academic delivery keys may still
appear in persisted data, but they are not current executable worker paths.

Check the health row tied to delivery failures:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT run_id, check_name, state, rule, diagnostic
FROM health_checks
WHERE run_id IS NOT NULL
  AND rule IN ('\''delivery_incomplete'\'', '\''required_delivery_failed'\'')
ORDER BY checked_at DESC
LIMIT 10;"'
```

If a run is tied to a non-healthy delivery state, resolve the delivery status
before retrying the run.

## Fix

Do not automatically re-run the job or requeue the delivery. First determine
whether the provider actually received the message:

**For Discord deliveries:** Open the Discord channel in the client or browser
and search by message ID. If the message with ID matching `external_url` exists
in the channel, the delivery succeeded and should be marked `sent`. Do not send
again. Update the delivery status only through a reviewed application endpoint
or migration, recording the provider's confirmation.

**For historical finance or academic deliveries:** Treat the rows as historical
audit data unless a future architecture explicitly re-enables those workflows.
If an identical idempotency key exists with status `sent`, the delivery
completed and retrying would create a duplicate.

If the provider did not receive the message:
- Do not manually delete the delivery row.
- Allow the existing `procrastinate_job` to retry using its configured retry
  policy. The job should reuse the same idempotency key and delivery intent.
- The delivery system checks the idempotency key before creating a new message,
  preventing duplicates on retry.

Only restart the API after reconciliation, and only if queue processing is
stalled:

```bash
docker compose restart api
```

## Expected health and Discord behavior

If a delivery remains uncertain or fails, the run's health state becomes
`Attention` for `delivery_incomplete` or `Failed` for `required_delivery_failed`,
depending on whether the delivery was optional or required for the run. Discord
alerting uses a durable idempotency key with the run ID, delivery status, and
error code; it must never include the message body, target user IDs, or delivery
credentials.

Once all delivery rows have status `sent`, the run's delivery health rule
transitions to healthy.

## Verify

Check for duplicates again to confirm the issue is resolved:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT channel, idempotency_key, COUNT(*)
FROM deliveries
GROUP BY channel, idempotency_key
HAVING COUNT(*) > 1;"'
```

This query should return zero rows. Verify the delivery status for the affected
run:

```bash
docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT channel, idempotency_key, status, external_url
FROM deliveries
WHERE run_id = '\''<RUN_ID>'\''
ORDER BY created_at;"'
```

All deliveries for that run should have status `sent` and a valid `external_url`
pointing to the provider's message.
