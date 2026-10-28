# Operational runbooks

Beta-phase procedures for the Social+ backend. Each section names the alert or symptom that
starts it, what to do first, how to diagnose, and what to record afterwards.

Before anything else, these three commands answer most questions:

```powershell
curl.exe -s http://127.0.0.1:8000/healthz          # liveness: is the process answering at all
curl.exe -s http://127.0.0.1:8000/readyz           # readiness: can it reach PostgreSQL
curl.exe -s http://127.0.0.1:8000/metrics          # counters, latency, pool, queue depth
```

Everything logged carries a request id, and every 500 response returns it in `X-Request-ID`
and in the JSON body, so a user report can be traced to a log line without guessing.

## Service down

Alert: `SocialPlusServiceDown`.

1. Confirm the process is running and the port is bound: `Get-NetTCPConnection -LocalPort 8000 -State Listen`.
2. If it is up but not scrapeable, the scrape target or network policy is wrong, not the app.
3. If it is down, check the last log lines for an exception at import time. A configuration
   error in `APP_ENV=production` fails fast on purpose: `COOKIE_SECURE`, `SMTP_HOST`,
   `SMTP_STARTTLS`, an `https://` `APP_BASE_URL`, and a PostgreSQL `DATABASE_URL` are all
   validated at startup.
4. Restart, then watch `/readyz` before sending traffic back.

Record: start time, end time, cause, and whether data was lost. A restart alone is not a fix.

## Database unreachable

Alert: `SocialPlusReadinessFailing`. Symptom: `/readyz` returns `503 {"status": "unavailable"}`.

1. Do not restart the application. Liveness is deliberately dependency-free, so a restart
   only loses in-flight work.
2. Check the database itself: `docker compose ps postgres` and `docker compose logs postgres`.
3. Confirm credentials and host from the running configuration. The development instance
   listens on 5433 (see `docker-compose.yml`); a wrong port is the most common cause.
4. Drain traffic or take the instance out of rotation until `/readyz` recovers, then
   confirm no request returned a partial write: every write path commits in one
   transaction, so a failure means the change did not happen rather than half happening.

Record: duration, whether any writes were accepted during the outage, and which alerts fired.

## Elevated errors or latency

Alert: `SocialPlusServerErrorRateHigh` or `SocialPlusRequestLatencyHigh`.

1. Use the metrics endpoint to see which status class and which route moved:
   `social_plus_http_requests_total{status="5xx"}` and
   `social_plus_http_request_duration_seconds` are labelled by route template, so no path
   parameter can hide the culprit.
2. Pull the failing request ids from the responses and search the logs for
   `"event": "request.failed"`. An unhandled failure logs a traceback with the same id.
3. A 403/404 spike is usually authorization or visibility doing its job — check
   `"event": "request.scope_denied"` for scope violations, which are answered as 404 by
   design so they reveal nothing about another tenant's data.
4. A pure latency problem with a healthy pool is usually an unindexed query; a latency
   problem alongside pool saturation is the next section.

Record: which route, whether users saw it, and the fix.

## Pool exhaustion

Alert: `SocialPlusDatabasePoolSaturated`.

1. Read `social_plus_db_pool_connections{state=...}`: `checked_out` near `size + overflow` is the
   condition, and `overflow` above zero means the pool already grew past its base size.
2. Look for one slow route in the latency histogram. The usual cause is a query that scans
   without an index once a table has real data.
3. Check for a leaked session: every request-scoped session is closed by the dependency, so
   a leak means something opened a session outside it.
4. Raise the pool only after the cause is known — a bigger pool moves the queue rather than
   removing it.

Record: the query or route responsible, and the pool settings before and after.

## Migration failure

Symptom: a deploy fails on `alembic upgrade head`, or `/readyz` fails right after a schema
change because the application expects a table that does not exist yet.

1. Read the current revision: `alembic -c backend/alembic.ini current`.
2. Read the failure. A partially applied migration is rolled back by the transactional DDL,
   so the database is normally still on the previous revision.
3. To step back: `alembic -c backend/alembic.ini downgrade -1`. Every migration in this
   repository implements `downgrade()` and the rehearsal below exercises it.
4. Never fix forward by editing a migration that has already run in a shared environment:
   add a new revision instead.
5. Rehearse any new revision on a scratch database before production:
   `$env:SOCIAL_PLUS_MIGRATION_TEST_DATABASE_URL = "postgresql+psycopg://social+:social+@localhost:5433/social+_test"` then
   `pytest backend/tests/test_migrations.py`.

Record: revision before and after, and whether any deploy was half-applied.

## Restore from backup

Alert: `SocialPlusRestoreRehearsalIsStale`. Run the rehearsal monthly and after any backup
configuration change:

```powershell
powershell -ExecutionPolicy Bypass -File ops/rehearse-restore.ps1
```

The script dumps the source database, restores it into a scratch database, and compares the
row counts of every table between the two. It refuses to touch a database whose name does
not look like a scratch target.

A real restore follows the same path with the production dump in place of the live dump:
restore into a scratch database first, verify counts and the newest rows, and only then
decide whether to promote it. Record the date, the dump size, the source, and any table
whose counts differ — a difference is either expected churn or a genuine gap.

Backups are taken and retained by the operator's platform, not by this application: account
deletion does not rewrite existing backups, and that is documented in the retention decision
in `implementation_plan.md`.

## Outbox backlog

Alert: `SocialPlusOutboxBacklogIsNotDraining`.

1. `social_plus_outbox_unpublished_events` only grows until a publisher ships. Until then this
   alert is expected and should stay muted; the gauge exists so the backlog is visible
   rather than silently unbounded.
2. After a publisher exists, a stalled backlog means the publisher is failing or holding a
   transaction open. Check its logs and whether its claim query is committing.
3. Never delete outbox rows to clear the gauge: they are the only record that a state
   change is still waiting to be announced.

Record: backlog size and the reason it was not draining.

## Stuck idempotency keys

Alert: `SocialPlusIdempotencyKeysStuck`.

1. A key in `in_progress` means a request reserved it and never completed, so a retry
   presenting the same key answers `409` until the reservation clears.
2. Find them:
   `SELECT scope, key, endpoint, created_at FROM idempotency_keys WHERE state = 'in_progress' ORDER BY created_at;`
3. Correlate with logs by endpoint and time. A reservation created by a request that then
   crashed stays behind only if the transaction committed, which the reservation path is
   designed to avoid; a large number here means something is committing early.
4. Deleting a reservation lets the client retry, but do it knowingly: the original attempt
   may have applied its change. Prefer surfacing the outcome to the affected account.

Record: how many, which endpoint, and the root cause.

## Deploying a release

1. Rehearse on a scratch database: migrations, restore, and the load test in this directory.
2. Apply migrations before starting the new application version, and keep the previous image
   available so a rollback is a redeploy rather than a rebuild.
3. Roll back the application first if it is failing; roll back the schema only if the
   previous version cannot run against it, using `alembic downgrade -1`.
4. Watch `/readyz` and the error-rate alert for ten minutes after the switch.

## Granting or revoking the platform moderator role

Personal updates have no community moderator, so their reports go to `/moderation`, and only
`users.is_moderator` opens it. Nothing in the application grants the flag — deliberately, so no
account can promote itself or another. An operator with database access sets it:

```sql
-- Grant. Every page load reads the flag, so the role is live on the next request.
UPDATE users SET is_moderator = true WHERE username = 'the-operator-account';

-- Revoke.
UPDATE users SET is_moderator = false WHERE username = 'the-operator-account';
```

Grant it to a named human, not a shared account: `platform_audit_events.actor_user_id` is what
makes a removal or suspension attributable. Check the current holders with:

```sql
SELECT username, display_name, is_active FROM users WHERE is_moderator ORDER BY username;
```

## Reinstating a suspended account

A moderator can end an account from the report queue, and the queue deliberately cannot reverse
that. Reinstatement is an operator decision, made after reviewing the audit trail:

```sql
SELECT created_at, action, actor_user_id, target_id, reason
FROM platform_audit_events
WHERE action = 'account.suspended' AND target_id = '<user-id>'
ORDER BY created_at DESC;

UPDATE users SET is_active = true WHERE id = '<user-id>';
```

Sessions were not deleted when the account was suspended, and `authenticated_user` refuses an
inactive account, so old cookies stop working; after reinstatement the account can sign in again
with its existing password. The suspension record stays in the audit log either way.

## Roles to assign before the pilot

The plan's exit criteria for this phase require named owners. Fill these in before inviting
users, and keep them current:

- **Beta owner:** _unassigned_ — owns the pilot cohort and the decision to widen access.
- **Operations owner:** _unassigned_ — owns alerts, deploy, backup, and restore rehearsal.
- **Moderation owner:** _unassigned_ — owns the report queue, removal decisions, and appeals.
- **Support path:** _unassigned_ — owns the address users report problems to, and the
  request-id and account-deletion handling that follows from a report.
