# Soccom

Soccom is a finite social communication platform. Phases 1–6 of [implementation_plan.md](implementation_plan.md) are implemented: account identity, relationships, communities, threaded discussions, moderation, an inbox-first chat experience, finite update windows, and notification/account-data settings. The same plan schedules file/media attachments and audio/video calling as Phases 8–9, and the commercial, trust, and Xyberos work as Phases 10–17. The pre-commercial bridge work those phases depend on is also landed: one `domain.action` event vocabulary, an append-only platform audit log, a transactional outbox, per-actor idempotency keys, and scope predicates that fail closed.

## Stack decisions

- Web: FastAPI, Jinja2, and HTMX. HTMX enhances the profile save; standard forms remain functional without JavaScript. Alpine.js is intentionally not included until a real client-side interaction needs it.
- Mobile: Flutter is deferred until the web MVP is validated. Keep product rules in backend services; add a versioned JSON API before a mobile client is built.
- Persistence: PostgreSQL with explicit Alembic migrations. The application never creates production tables at startup.
- Identity: opaque server-managed session tokens, stored only as SHA-256 digests; cookies are HttpOnly, SameSite=Lax, and Secure in production. Forms use session-bound CSRF tokens.
- Identity: email verification is disabled for the MVP. Registration signs users in immediately; enable verification before opening registration publicly.
- Recovery: one-time, hashed, 30-minute password reset tokens delivered by SMTP. Responses do not reveal whether an account exists.
- Relationships: follows and blocks are unique directed edges. A block prevents follows in either direction and removes follows already present between the two accounts.
- Communities: public communities are discoverable and joinable by signed-in users. Private communities do not appear in discovery and return a non-revealing 404 to non-members; owners and moderators can invite users by username. Owners alone can grant or revoke moderator roles; moderators can remove ordinary members. Administrative changes are audited.
- Discussions: community members can create bounded text posts and threaded comments. Post reads respect community privacy and block relationships. Authors can remove their own content; owners/moderators can remove content, lock discussions, review reports, and leave moderation events. Exact duplicate posts and rapid posting are rate-limited.
- Chat: the signed-in destination is the inbox, with persisted one-to-one and small-group text conversations, read state, and authenticated WebSocket delivery. Direct-message blocks stop new sends while preserving history. Group messages remain shared by active participants; membership additions, removals, and departures are auditable, and removed members lose live access without deleting history. WebSocket fan-out is process-local, so use one app worker until shared fan-out is added. Presence, calls, media, and end-to-end encryption are not in the current build.

## Backend layout

The backend is one deployable app split into transport, domain, and persistence layers. Domain boundaries live inside those layers rather than in separate packages, so adding a domain adds files instead of a new service to operate.

```text
backend/app/
├── main.py                 # assembly: logging, middleware, routers, ASGI entry point
├── core/                   # configuration and infrastructure primitives
│   ├── config.py           # settings and production validation
│   ├── database.py         # engine, session factory, and Base
│   ├── security.py         # password hashing, tokens, and rate limiting
│   ├── observability.py    # request ids and structured logging
│   ├── events.py           # the shared domain.action event vocabulary
│   ├── scoping.py          # tenant scope predicates that fail closed
│   └── mail.py             # SMTP delivery
├── api/                    # HTTP transport
│   ├── middleware.py       # security headers, request context, unhandled-error handler
│   ├── deps.py             # browser sessions, CSRF, authentication, rate limits
│   ├── responses.py        # template rendering, form errors, and redirects
│   ├── router.py           # router aggregation
│   └── routes/             # health, pages, identity, account, people, chat,
│                           # communities, content, moderation, notifications
├── models/                 # SQLAlchemy models per domain, re-exported from package root
├── schemas/                # Pydantic contracts for the JSON endpoints
├── services/               # authorization, communities, messaging, notifications, updates,
│                           # audit, outbox, idempotency
├── templates/              # Jinja2 templates
└── static/                 # stylesheet and the chat client
```

Rules of thumb:

- Route modules handle transport concerns (validation, forms, redirects) and delegate visibility, membership, and notification rules to `services/`.
- `services/` never imports `api/`, and `models/` imports neither. Request plumbing stays in `api/` so domain rules stay testable without HTTP.
- `app.main:app` stays the single ASGI entry point; a new domain is a new `api/routes/<domain>.py` router added to `api/router.py`, plus its model and service modules.

## Local setup (Windows PowerShell)

1. Copy `.env.example` to `.env` and review the development-only settings.
2. Start PostgreSQL and Mailpit: `docker compose up -d postgres mailpit`.
3. Create the virtual environment and install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   Direct dependency ranges are maintained in `requirements.in`; `requirements.txt` pins the tested environment.

4. Apply database migrations and start the app:

   ```powershell
   $env:PYTHONPATH = "backend"
   .\.venv\Scripts\alembic.exe -c backend\alembic.ini upgrade head
   .\.venv\Scripts\uvicorn.exe app.main:app --app-dir backend --reload
   ```

5. Open `http://localhost:8000`. Password reset messages appear at `http://localhost:8025`.

The Compose credentials are for local development only. Production settings require PostgreSQL, HTTPS, Secure cookies, and SMTP with STARTTLS. The built-in IP rate limiter is process-local; enforce rate limits at the edge as well, especially when running multiple workers. HTMX is pinned to a CDN release in the base template; self-host that asset before public launch.

## Checks

```powershell
$env:PYTHONPATH = "backend"
pytest backend\tests
```

The tests use an isolated in-memory SQLite database. Apply and verify Alembic migrations against PostgreSQL as part of local setup and CI.

### Migration rollback rehearsal

`backend\tests\test_migrations.py` upgrades a scratch PostgreSQL database to head, downgrades it to base, and upgrades it again, so a migration with a broken `downgrade()` fails the suite. It is skipped unless a connection string is supplied, and it refuses to run against any database whose name lacks `test`, because it drops every application table.

```powershell
$env:PYTHONPATH = "backend"
$env:SOCCOM_MIGRATION_TEST_DATABASE_URL = "postgresql+psycopg://soccom:soccom@localhost:5433/soccom_test"
pytest backend\tests\test_migrations.py
```

Create the `soccom_test` database once against the Compose PostgreSQL instance before running the rehearsal. The test leaves that database at head.

## Operations

- `GET /healthz` is liveness only and has no dependencies, so a database outage never triggers a restart.
- `GET /readyz` is readiness: it runs `SELECT 1` and answers `503 {"status": "unavailable"}` when the database is unreachable, so traffic can be drained without killing the process.
- Every request carries an `X-Request-ID`. An upstream value is reused when it is 8-64 characters of `[A-Za-z0-9._-]`; otherwise the app generates one. The id is returned in the `X-Request-ID` response header, included in the JSON body of an unhandled 500, and attached to every log record written while that request is in flight.
- Logs are structured JSON whenever `APP_ENV` is not `development`; set `LOG_JSON` to override either way. Request logs carry method, path, status, duration, client, and request id. `/static` responses are not logged and the health probes log at `DEBUG`, and `uvicorn.access` is silenced because the middleware logs requests itself.
- Security headers (`nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, and a `Content-Security-Policy` limited to same-origin connections plus the HTMX bundle and Google Fonts) are set on every response; production also sends `Strict-Transport-Security`. The generated API docs (`/docs`, `/docs/oauth2-redirect`, `/redoc`) get a separate policy that also allows the jsDelivr assets and the inline bootstrap script Swagger UI and ReDoc require, so a blank docs page means that policy was tightened too far.

## Bridge layer

The integration hooks that keep the commercial phases from reworking the social core. Their three tables arrive in migration `0010_bridge_infrastructure`; `backend/tests/test_bridge.py` covers each rule below.

- **Event vocabulary.** `core/events.py` owns every `domain.action` name, including the commercial names reserved for later phases. Audit rows and outbox rows validate against it, so a typo fails at the write site instead of storing an event nothing can read.
- **Platform audit log.** `platform_audit_events` is append-only and records action, scope, actor, target, before/after state, reason, and request id. Community and moderation actions write it alongside their existing typed rows through `services/communities.py`, conversation membership changes through `services/messaging.py`, and account deletion and ownership transfer through `routes/account.py`. `scope_id` and `target_id` carry no foreign key on purpose: an audit row outlives the actor or target it describes. The one exception is retention rather than integrity — removing a tenant (a community with no active member left) removes its scoped rows and leaves a platform-scoped `community.deleted` record.
- **Transactional outbox.** `services/outbox.py` writes an event in the same transaction as its state change and never commits by itself, so an event cannot exist without the change it announces. Nothing publishes yet: `published_at` stays NULL until a durable publisher and its pruning jobs arrive.
- **Idempotency keys.** Send `Idempotency-Key: <8-128 characters of A-Za-z0-9._:->` on a mutating request, or the same value in an `idempotency_key` form field. A retry that reuses a key with the same content replays the stored response instead of repeating the change; the same key with different content answers `409`; a malformed key answers `400`; keys are scoped per account, so another account's key neither blocks nor reveals anything. Keys are recorded but not expired yet — pruning arrives with the outbox publisher. The chat client presents one per message (`static/chat.js`); the remaining mutating endpoints adopt them as their clients gain retry behavior.
- **Scope predicates.** `core/scoping.py` gives every scope-carrying table one `(scope_type, scope_id)` predicate, `scoped_select`/`scoped_get` for reads, and `ScopeViolation` for a mismatch, which `api/middleware.py` answers with the same non-revealing 404 as a missing row. `business` is reserved for Phase 11, and `platform` covers records that must outlive a tenant.

Ids in audit and outbox payloads are strings, and a payload carries only what a consumer needs to act on: the report reason, for example, stays in `content_reports`, where account deletion can redact it.
