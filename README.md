# Social+

Social+ is a finite social communication platform. Phases 1–6 of [implementation_plan.md](implementation_plan.md) are implemented: account identity, relationships, communities, threaded discussions, moderation, an inbox-first chat experience, finite update windows, and notification/account-data settings. The same plan schedules file/media attachments and audio/video calling as Phases 8–9, and the commercial, trust, and Xyberos work as Phases 10–17. The pre-commercial bridge work those phases depend on is also landed: one `domain.action` event vocabulary, an append-only platform audit log, a transactional outbox, per-actor idempotency keys, and scope predicates that fail closed.

## Stack decisions

- Web: FastAPI, Jinja2, and HTMX. HTMX enhances the profile save; standard forms remain functional without JavaScript. Because a boosted link or form can only replace the piece of the page it was pointed at, never the shell around it, a redirect on an HTMX request is answered with an `HX-Redirect` header instead — so an expired session sends the browser to the sign-in page rather than swapping it into a page the old session rendered. Alpine.js is intentionally not included until a real client-side interaction needs it.
- UI shell: every page renders inside one base template — a sticky top bar, a left rail of destinations, and a centered column of cards — with the mobile tab bar taking over below 761px. The top bar's avatar is an account menu: a native `<details>` disclosure that opens Profile, Settings, and a log-out form, so it needs no JavaScript to open or to submit. It opts out of the header's `hx-boost`, because these are account navigations that replace the whole shell — logging out would otherwise swap only `#main-content` and leave the rail and the open menu on screen. The stylesheet keeps the existing palette; the layout is what is feed-first, since the dashboard still names its count, window end, and completion. Type is loaded over Inter's light 300–450 axis and the sheet uses three weights only — 400 for body text, 425 for navigation, buttons, and labels, 450 for headings, names, and badges — so hierarchy comes from size and colour rather than weight.
- Landing page: `/dashboard` is where a signed-in visitor lands (login, registration, and `/` all redirect there). It carries both halves of the finite window: a "New post" entry that opens the compose view, and the batch of posts from followed people and joined communities. `/updates` is a permanent redirect to it, so saved links and in-flight cursors keep working. Chat moved to `/conversations`, one click away behind the rail.
- Post view: one view renders both kinds of post — a personal update and a community post — so `/posts/{id}` and `/communities/{slug}/posts/{id}` differ only in the byline, the wording of a removal, and the actions the reader may take. `templates/partials/post_view.html` holds the card and `templates/partials/comments_section.html` holds the composer and thread; each route keeps its own visibility rules.
- Workspace: `/workspace` is the one place the reader sees and manages everything they published — personal updates and community posts in a single filterable list, each row carrying the author's own withdraw action and, for a personal update, the control that changes who may read it. It is a keyset window (50 per page, "showing N of M", never an unbounded dump) and it keeps a row for a post whose community the reader has left, without a link, because that community page answers 404 to a non-member. The profile page carries identity and the account fields and links here rather than listing posts.
- Mobile: Flutter is deferred until the web MVP is validated. Keep product rules in backend services; add a versioned JSON API before a mobile client is built.
- Persistence: PostgreSQL with explicit Alembic migrations. The application never creates production tables at startup.
- Identity: opaque server-managed session tokens, stored only as SHA-256 digests; cookies are HttpOnly, SameSite=Lax, and Secure in production. Forms use session-bound CSRF tokens.
- Identity: email verification is disabled for the MVP. Registration signs users in immediately; enable verification before opening registration publicly.
- Recovery: one-time, hashed, 30-minute password reset tokens delivered by SMTP. Responses do not reveal whether an account exists.
- Relationships: follows and blocks are unique directed edges. A block prevents follows in either direction and removes follows already present between the two accounts.
- Communities: public communities are discoverable and joinable by signed-in users. Private communities do not appear in discovery and return a non-revealing 404 to non-members; owners and moderators can invite users by username. Owners alone can grant or revoke moderator roles; moderators can remove ordinary members. Administrative changes are audited.
- Discussions: community members can create bounded text posts and threaded comments. Post reads respect community privacy and block relationships. Authors can remove their own content; owners/moderators can remove content, lock discussions, review reports, and leave moderation events. Exact duplicate posts and rapid posting are rate-limited.
- Chat: the inbox holds persisted one-to-one and small-group text conversations, read state, and authenticated WebSocket delivery. Direct-message blocks stop new sends while preserving history. Group messages remain shared by active participants; membership additions, removals, and departures are auditable, and removed members lose live access without deleting history. A conversation opens on its first unread message — the newest one when nothing is unread — and follows arriving messages only while the reader is already at the end, so scrolling back is never interrupted. Every signed-in page also holds `/ws/inbox`, which carries only the reader's own unread total so the Chat badge updates without a reload. WebSocket fan-out is process-local, so use one app worker until shared fan-out is added. Conversations also carry 1:1 and group audio and video calls (see [Calls](#calls)). Presence, media on posts, and end-to-end message encryption are not in the current build.

## Interface and themes

The shell is server-rendered and theme-aware without a flash of the wrong palette. A reader
chooses **System**, **Light**, or **Dark** from the switch in the top bar (it is present on every
page, signed in or out) and from Appearance in settings. `system` leaves `data-theme` off the
`<html>` element so a `prefers-color-scheme` block decides; an explicit choice renders
`data-theme="light"` or `"dark"` with the first byte of HTML. The choice lives on the account
(`users.theme`, migration `0017_user_theme`) and in an HttpOnly cookie for signed-out pages, so
the login screen matches what the reader will see afterwards. The menu is a plain form post and
works without JavaScript; `static/js/theme.js` upgrades it to an instant switch and previews the
settings radios before saving. `backend/tests/test_theme.py` covers the cookie, the account
preference, the redirect guard, and the rendered attribute.

The stylesheet is organised around semantic tokens (`--surface`, `--ink`, `--accent`,
`--accent-solid`, `--on-accent`, `--focus-ring`, and the shadow roles) so a theme redefines
roles rather than rules. Both palettes meet WCAG AA on the text they carry; the dark palette
keeps the brand's pale green for emphasis and uses a near-white ink for body copy.

Icon surfaces get their own roles so they sit with the rest of the palette instead of
competing with it: `--icon-bg`/`--icon-ink` paint navigation and notification glyphs,
`--avatar-bg`/`--avatar-ink` paint identity marks, and an unread notification swaps to
`--signal-chip`/`--signal-ink` so the badge matches the row it belongs to rather than
introducing a second accent. The rail's current page inverts its icon to a solid
`--accent-solid` disc, so the selected item reads even where the rail tint is close to the page
background. Every glyph pair clears 4.5:1 against its own disc in both themes.

Smaller interface rules that came out of the same pass: the rail marks the current page with an
inset accent bar as well as a tint, cards lift on hover instead of everywhere, buttons have a
disabled state, headings balance their lines, focus rings come from a token that is visible on
both surfaces, and `prefers-reduced-motion` turns the page-arrival animation off.

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
├── services/               # authorization, communities, content helpers, messaging,
│                           # call signaling, notifications, updates, audit, outbox, idempotency
├── templates/              # Jinja2 templates, grouped the way api/routes/ is
│   ├── base.html           # the shell every page extends
│   ├── partials/           # macros and fragments shared across pages
│   ├── auth/ account/ chat/ communities/ people/ posts/
│   └── home.html dashboard.html notifications.html
└── static/
    ├── css/styles.css      # one stylesheet, opened by its palette tokens
    └── js/
        ├── shell.js        # the shell socket and its live unread badge
        └── chat.js         # the chat client
```

Rules of thumb:

- Route modules handle transport concerns (validation, forms, redirects) and delegate visibility, membership, and notification rules to `services/`.
- `services/` never imports `api/`, and `models/` imports neither. Request plumbing stays in `api/` so domain rules stay testable without HTTP.
- Templates are named after the route that renders them and live under their domain: `communities/detail.html`, `posts/new.html`. Anything two pages share goes in `templates/partials/` as a macro — `page_heading`, `post_compose`, `auth_layout`, `post_view`, `comments_section`, `comment_tree` — so a heading or a composer changes in one place.
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

Steps 3-4 run the application on the host interpreter. To run the same application as a container instead:

```powershell
docker compose up -d --build app     # builds the Dockerfile and serves http://localhost:8010
```

Attachment bytes need object storage. Start it and create the bucket once:

```powershell
docker compose up -d object-storage
$env:PYTHONPATH = "backend"
.\.venv\Scripts\python.exe -c "from app.core.storage import bootstrap_bucket; print(bootstrap_bucket())"
```

That creates the bucket and allows browser uploads from `APP_BASE_URL` (plus both loopback names
outside production). The CORS rule is not optional: a browser posts attachment bytes straight to
storage, so without it every upload fails in the browser *after* the bytes have been stored.

That service is moto, an S3 API implementation intended for development and CI. It keeps state
in memory, so attachments do not survive a restart of it, and it does not enforce the upload
policy's size condition — the application re-checks the size on finalize regardless. Point
`S3_ENDPOINT_URL` at a real bucket for anything beyond local work. To exercise malware scanning
as well, start the optional scanner and set `CLAMAV_HOST=localhost`:

```powershell
docker compose --profile scanning up -d    # first start downloads virus signatures, so it is slow
```

The container publishes 8010 because 8000 belongs to the local `uvicorn` workflow, and the Compose project is pinned as `soccom` in `docker-compose.yml` so renaming the working directory cannot fork the stack into a second, empty volume. It runs as an unprivileged user and carries a health check against `/healthz`.

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
- Security headers (`nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, and a `Content-Security-Policy` limited to same-origin connections plus the HTMX bundle, Google Fonts, the configured object-storage origin that browsers upload attachments to, and `blob:` images for an attachment the reader already received) are set on every response; production also sends `Strict-Transport-Security`. The generated API docs (`/docs`, `/docs/oauth2-redirect`, `/redoc`) get a separate policy that also allows the jsDelivr assets and the inline bootstrap script Swagger UI and ReDoc require, so a blank docs page means that policy was tightened too far. A route may set its own policy, which is how attachment delivery serves stored media under a narrower one. The base template switches off HTMX's inline indicator stylesheet, which this policy would block and which no template here uses.
- `GET /metrics` exposes `soccom_http_requests_total`, `soccom_http_request_duration_seconds`, `soccom_uptime_seconds`, `soccom_db_pool_connections`, `soccom_outbox_unpublished_events`, and `soccom_idempotency_keys_in_progress` in the Prometheus text format. Routes are labelled by their template with path parameters substituted back out, and unmatched requests share one placeholder, so no path parameter can multiply a series. A failed database read drops the queue gauges instead of failing the scrape, because metrics matter most while the database is down. It is exportable rather than public: expose it only to your scraper at the edge, since aggregate request and saturation data is still operational data.
- `ops/` holds the beta evidence and procedures. `alerts.yml` carries rules for error rate, latency, pool saturation, stuck idempotency keys, outbox backlog, and restore-rehearsal staleness; `runbooks.md` says what to do for each and lists the owners this phase still needs; `rehearse-restore.ps1` dumps the database, restores it into a scratch database, and compares every table's row count and the migration revision; `loadtest.py` measures p95 latency and error rate against documented budgets.
- Accessibility is asserted rather than assumed: `backend/tests/test_accessibility.py` parses every rendered page and fails on a missing document language, an unlabelled control, an image without `alt`, a button with no accessible name, or a positive `tabindex`.

## Bridge layer

The integration hooks that keep the commercial phases from reworking the social core. Their three tables arrive in migration `0010_bridge_infrastructure`; `backend/tests/test_bridge.py` covers each rule below.

- **Event vocabulary.** `core/events.py` owns every `domain.action` name, including the commercial names reserved for later phases. Audit rows and outbox rows validate against it, so a typo fails at the write site instead of storing an event nothing can read.
- **Platform audit log.** `platform_audit_events` is append-only and records action, scope, actor, target, before/after state, reason, and request id. Community and moderation actions write it alongside their existing typed rows through `services/communities.py`, conversation membership changes through `services/messaging.py`, and account deletion and ownership transfer through `routes/account.py`. `scope_id` and `target_id` carry no foreign key on purpose: an audit row outlives the actor or target it describes. The one exception is retention rather than integrity — removing a tenant (a community with no active member left) removes its scoped rows and leaves a platform-scoped `community.deleted` record.
- **Transactional outbox.** `services/outbox.py` writes an event in the same transaction as its state change and never commits by itself, so an event cannot exist without the change it announces. Nothing publishes yet: `published_at` stays NULL until a durable publisher and its pruning jobs arrive.
- **Idempotency keys.** Send `Idempotency-Key: <8-128 characters of A-Za-z0-9._:->` on a mutating request, or the same value in an `idempotency_key` form field. A retry that reuses a key with the same content replays the stored response instead of repeating the change; the same key with different content answers `409`; a malformed key answers `400`; keys are scoped per account, so another account's key neither blocks nor reveals anything. Keys are recorded but not expired yet — pruning arrives with the outbox publisher. The chat client presents one per message (`static/js/chat.js`); the remaining mutating endpoints adopt them as their clients gain retry behavior.
- **Scope predicates.** `core/scoping.py` gives every scope-carrying table one `(scope_type, scope_id)` predicate, `scoped_select`/`scoped_get` for reads, and `ScopeViolation` for a mismatch, which `api/middleware.py` answers with the same non-revealing 404 as a missing row. `business` is reserved for Phase 11, and `platform` covers records that must outlive a tenant.

Ids in audit and outbox payloads are strings, and a payload carries only what a consumer needs to act on: the report reason, for example, stays in `content_reports`, where account deletion can redact it.

## Attachments

Attachments extend conversations (Phase 8) and, since `0018_post_media`, a post's photos,
video, and audio. Chat files and post media share one pipeline; what differs is the scope they
are issued under, the ceilings that apply, and what happens to the bytes afterwards.

The flow has three steps, and each enforces its own rule:

1. `POST /conversations/{id}/attachments` checks that the caller is an active participant, then
   applies the declared type against the allowlist, the per-file ceiling, and the per-account
   quota, and returns a short-lived single-key upload target. The key is derived from the scope
   and the attachment id, so a client cannot choose where its bytes land. Presenting the same
   `Idempotency-Key` returns the same target instead of creating a second one.
2. The browser posts the bytes straight to object storage. The presigned POST policy asks the
   storage service to enforce the size ceiling; because not every S3-compatible service honours
   that condition, `POST /attachments/{id}/finalize` re-checks the stored size anyway. Finalize
   then detects the type from the bytes and rejects a mismatch with the declaration, strips
   image metadata by re-encoding the pixels, runs the scan, and only then promotes the object to
   `scanned`. A replayed finalize is a no-op rather than a second record.
3. `GET /attachments/{id}/download` re-authorizes every read against the conversation, the
   attachment's scope, and the block rules. An unknown id, an unfinished upload, a rejected
   upload, and an unauthorized reader all answer the same non-revealing 404. Allowlisted images
   may render inline under `default-src 'none'; sandbox`; everything else is sent as a
   `Content-Disposition: attachment` download. No bucket is public and no URL is permanent.

There is one stored copy of an attachment. Delivery is confirmed by the reader's page, not by
the download alone: once the browser has the bytes stored locally it posts
`POST /attachments/{id}/received`, which records the reader in `attachment_deliveries`
(migration `0015_attachment_deliveries`). When every other participant in the conversation has
a row there, the object is removed from storage. The row stays behind as a tombstone, so the
transcript still names the file and says `Attachment expired` rather than losing the slot, and
the account's quota is released. A reader that fetched the bytes but could not keep them — no
storage, a private window — never confirms, so the file survives for everyone else. The owner's
own confirmation is not a delivery: there is nothing to release until the people it was sent to
have it.

Because the server copy is gone, the reader's browser keeps its own copy in IndexedDB, keyed by
account so a second sign-in on the same machine cannot read it. Images are fetched once through
`static/js/chat.js` and rendered from that local blob, which is why an expired bubble still
opens for the person who received it. A web page cannot link to a file saved in someone's
Downloads folder — a `file:///` navigation is blocked from an `https` page and the save path is
never exposed — so this local copy is the closest the application can get. Anyone without that
copy, including the sender, sees the tombstone.

Each transition is durable, so a scanner outage leaves an upload in `pending_scan` and tells the
client to retry instead of promoting unverified bytes. Abandoned uploads are reaped when the
next target is issued, and deleting an account removes its objects before its rows. Markup and
SVG are refused outright rather than scanned, because no scan verdict makes them safe to serve
from the application's origin.

Two things outside the application have to be right for uploads to work, and both fail in ways
that look like a broken attachment rather than a configuration problem:

- The bucket must allow the application's origin, because the browser posts bytes straight to
  storage. `bootstrap_bucket()` applies that rule; a real deployment sets it on its bucket.
- The application's `Content-Security-Policy` must name the storage origin in `connect-src`, or
  the browser refuses the request before it leaves the page. That origin is added automatically
  from `S3_ENDPOINT_URL`, and only that origin is added.

Two deliberate boundaries: attachments are **not** end-to-end encrypted, because scanning and
moderation need readable bytes (Decision 15) — they are encrypted at rest by the storage
provider and access-controlled here; and a message carries text plus at most
`ATTACHMENT_MESSAGE_LIMIT` attachments. A message may be an attachment alone, with no text:
`0014_attachment_only_messages` relaxed the body-length check, and the route still refuses a
message that has neither.

### Media on a post

A post's media is issued against the author's **account** rather than a conversation, because
the post that will own the file does not exist yet. `POST /posts/media` returns the same kind
of short-lived target, the browser posts the bytes straight to storage, and
`POST /attachments/{id}/finalize` verifies and scans them. `POST /posts` then binds the ids it
was given, and refuses an upload that is not verified, not the caller's, already claimed, or
issued for a conversation — so an upload cannot be walked from one scope into another. A draft
the composer drops is released immediately by `DELETE /posts/media/{id}`; once a post names the
file, only the post decides what happens to it.

Three things make media usable rather than merely storable:

- **The allowlist names containers, not just types.** `detect_family` reads the header: a JPEG,
  PNG, GIF, or WEBP for images; `ftyp` for MP4 and M4A; EBML carrying a `webm` DocType for WebM;
  `OggS`; a RIFF `WAVE`; and an ID3 or MPEG frame sync for MP3. The declared type must belong to
  the detected family, which is what lets `video/mp4` and `audio/mp4` share one container
  honestly instead of guessing between them from a header. Matroska is refused rather than
  relabelled, because no browser plays it from this pipeline.
- **Ceilings are per kind.** `POST_IMAGE_MAX_BYTES`, `POST_VIDEO_MAX_BYTES`, and
  `POST_AUDIO_MAX_BYTES` (10 MB, 50 MB, 25 MB by default) apply at target issuance and again at
  finalize against what actually arrived. The composer prints the same three numbers beside the
  picker, from the same settings, so the page and the server cannot disagree. Chat attachments
  keep the single `ATTACHMENT_MAX_BYTES` ceiling.
- **Large files are never held whole.** An image is decoded and re-encoded, so it is buffered;
  video and audio are read through `iter_chunks`, hashed on the way past, and streamed to the
  scanner, so finalizing a 50 MB clip costs one pass and no 50 MB allocation. The same read path
  answers `Range` requests, which is what makes seeking in a published video a 206 with a
  `Content-Range` rather than a re-download.

Read authorization follows the post, not the file. A personal update's media is readable by
whoever `can_read_personal_post` admits — its author, its audience, and never a blocked pair —
and a community post's media needs a visible community and a non-blocked author. A removed post
answers 404 for its files even to its author, because the reason the post is gone should stay
gone. Unlike a chat attachment, post media is **not** released once a reader has it: a chat file
exists to be received by the people it was addressed to, while a post is read by whoever arrives
later, so its bytes stay until the account removes them. That is also why the account quota was
raised to 1 GB.

A personal update may now be media alone. `0018_post_media` relaxed `ck_post_shape` so an empty
body is legal; the route still refuses a post with neither text nor media, and duplicate
detection only compares words, so two photo posts in a day are two posts rather than one sent
twice.

Still deferred: transcoding, image derivatives (such as a video's poster frame), resumable
uploads, voice messages, and media in exports or email.

## Calls

A call is an extension of a conversation: 1:1 and group, audio or video, for the people
already in that conversation. The caller presses the call control in the conversation header;
every eligible participant gets an explicit **Answer**/**Decline** panel, and nothing ever
auto-answers. Muting, switching the camera off, and leaving are per participant. Calls live in
`calls` and `call_participants` (migration `0016_calls`) and are covered by
`backend/tests/test_calls.py`.

### What the server owns

A call record exists before any SDP is exchanged, so every later packet is authorized against a
row instead of against a client-chosen id. The state machine is `ringing -> accepted -> ended`,
with `declined`, `missed`, `busy`, `failed`, and `cancelled` recorded as the end reason, and a
per-participant outcome (`invited`, `joined`, `declined`, `left`, `missed`) with join and leave
times. Ringing expires after `CALL_RING_TIMEOUT_SECONDS`; expiry is lazy, evaluated by the next
call request in that conversation, and the caller's page also ends its own ring on the same
timeout. When a call ends without a participant having answered, that participant gets a
`call` notification ("Missed call from Alice") unless `notify_calls` is off.

The page shows an `Answer`/`Decline` panel while ringing, then an in-call timer and the names of
whoever is connected. Signaling rides the existing authenticated conversation WebSocket:
`POST` sets the state, `/calls/ice` hands out ICE configuration, and `call_signal` packets carry
offers, answers, and ICE candidates to one named participant. Each packet is authorized (both
sides joined, same call, same conversation, no block between them), capped at 64 KB, and
rate-limited per participant per call (`CALL_SIGNAL_LIMIT_PER_MINUTE`). An unreadable packet is ignored, so
heartbeats and older clients keep working.

### Confidentiality

The application never receives call media. Group calls are a **mesh**: every participant holds
one `RTCPeerConnection` per other participant, and each of those is its own DTLS-SRTP connection
between two browsers. The server is not in the media path at all — there is no SFU, no relay it
controls, and no recording path anywhere in the codebase. A TURN relay, when configured, sees
only ciphertext it cannot read.

Because the signaling channel is authenticated but not trusted with keys, every call shows
**safety numbers** derived from the DTLS fingerprints that protect it (sorted, SHA-256, 16
decimal digits, `static/js/safety-number.js`). A 1:1 call shows one number; a group call lists
one per other participant, because each pair has its own keys. Both sides compute the same
number, so comparing it out of band is what makes "nobody is in the middle" checkable rather
than claimed. `backend/tests/test_safety_number.py` runs the shipped module under Node and
shows that a fingerprint substituted during signaling changes the number.

### Boundaries the server enforces

- **Participation.** Only active conversation members can ring, accept, or be invited; anyone
  else sees the same non-revealing 404 as a missing call.
- **Blocks are absolute.** A blocked pair cannot ring each other, and blocking during a call
  removes both accounts from it (ending a direct call, leaving a group call running for the
  rest).
- **Who can call me.** `accept_calls` in settings removes the account from new invites; the
  setting never interrupts a call already in progress.
- **Busy.** An account already joined to an open call is not invited, so a second call cannot
  arrive on top of the first.
- **Ceiling.** A call holds at most `CALL_MAX_PARTICIPANTS` (default 8) participants, checked at
  ring time and again at join time, because a mesh costs each participant `n - 1` uplinks. A
  group larger than the ceiling is refused with a clear message instead of being partially
  invited. `CALL_CONCURRENT_LIMIT` caps open calls per worker, and `CALL_START_LIMIT_PER_MINUTE`
  rate-limits ring attempts.
- **Fair use.** Rounds are bounded in size, signals are bounded in number, and every refusal is
  counted in `soccom_call_rejections_total`.

### TURN

`GET /calls/ice` returns `WEBRTC_ICE_SERVERS` (STUN by default) plus, when
`WEBRTC_TURN_URLS` and `WEBRTC_TURN_SECRET` are set, short-lived TURN credentials in the coturn
REST convention: username `"<expiry>:<user id>"`, credential `base64(HMAC-SHA1(secret,
username))`, valid for `WEBRTC_TURN_TTL_SECONDS`. A TURN URL without a secret is not offered —
a static credential is worse than no relay.

For development, `docker compose --profile calls up -d` starts coturn with the shared secret
`local-turn-secret`; set `WEBRTC_TURN_URLS=turn:localhost:3478?transport=udp` and
`WEBRTC_TURN_SECRET=local-turn-secret` on the app. The Compose service uses host networking
because a relay advertises the address it allocated on, and Docker Desktop does not implement
host networking: on Windows or macOS use a hosted relay with the same shared secret, or run
coturn natively. A public deployment terminates `turns:` over TLS and is sized by relay egress,
which is the main cost driver once a relay exists.

A relay-only path is an operator check rather than an automated one:

1. point `WEBRTC_TURN_URLS` at the relay and call between two networks that cannot reach each
   other directly (mobile data to a wired network is the usual reproduction);
2. in `chrome://webrtc-internals` (or `about:webrtc` in Firefox), confirm the selected candidate
   pair is `relay` on both sides;
3. in the coturn log, confirm one allocation per participant and no key material or media;
4. the safety numbers still match, and `soccom_call_signals_total` keeps rising while media
   flows — the relay, not the application, is carrying the call.

### Operational facts

Metrics: `soccom_calls_started_total{media,kind}`, `soccom_calls_ended_total{reason}`,
`soccom_call_signals_total{signal}`, `soccom_call_rejections_total{reason}`, plus the
`soccom_calls_active` and `soccom_call_participants_joined` gauges and the
`soccom_call_ring_duration_seconds` histogram (ring to first accept, the server's view of join
latency). A client-reported connection failure lands in `soccom_calls_ended_total{reason="failed"}`,
which is the drop rate; relay usage share belongs to the TURN server's own metrics, not the
application. Media quality is a browser concern: the client samples `getStats` during video
calls and turns the camera off after two consecutive samples above 15% packet loss, keeping
audio.

The conversation page is the only page whose `Permissions-Policy` allows the camera and
microphone. A refused permission, a missing device, a device already in use, an unsupported
browser, and a failed peer connection each have an explicit message; nothing silently falls back
to another media kind. Closing the page, losing the socket, or dropping the last tab leaves the
call on the server side too.

Support matrix for the client: the current evergreen versions of Chrome, Edge, Firefox, and
Safari. The mesh uses only `RTCPeerConnection`, `getUserMedia`, and DTLS-SRTP, so no
encoded-transform support is required; one page per account takes part in a call. Group calls
need one uplink per other participant, and iOS Safari suspends a call when its tab is
backgrounded, which the app reports as a connection failure rather than pretending the call
continues.

**Deferred:** a selective forwarding unit with SFrame for ceilings above the mesh budget, live
device switching, and multi-device call participation (one page per account takes part, as
above). There is no server-side recording path in the codebase, and no client-side recording
exists either.

## Personal updates

A post belongs to a community or to an account. The second shape arrived in migration
`0012_personal_posts` and is covered by `backend/tests/test_personal_posts.py`: the schema
states both shapes in `ck_post_shape`, so a community post is titled and a personal update is
text with an optional title.

Publishing happens on `/posts/new`, the compose view the dashboard's "New post" entry and the
workspace both open; publishing lands on the new post's own view. A personal update then
appears in four places:

- `/posts/{id}` — the post view: the post card plus its comment thread, the same view a
  community post opens in (`templates/partials/post_view.html` and
  `templates/partials/comments_section.html` render both kinds, and
  `templates/partials/comment_tree.html` renders the replies).
- `/people/{id}` — the author's page in the directory. For an author the reader does not
  follow it is also the discovery path for an update shared with everyone, and it narrows to
  the same rows the post view would open.
- `/dashboard` — the `people` source, which is exactly the list of accounts the reader follows
  plus the reader's own posts, so a post published from the landing page reads back there; the
  `communities` source therefore never includes a personal update. The list is narrowed by the
  audience rule, so a follower's window never includes someone else's `only me` update.
- `/workspace` — the author's own list, where the row carries the withdraw action and the
  control that changes the audience afterwards.

Every refusal is the same non-revealing 404 a missing post returns (`find_personal_post`).
Removing an update leaves the author a tombstone and hides it from everyone else.

### Who can see an update

Migration `0013_post_audience` added `posts.audience`, one value of `AUDIENCES`
(`app/models/content.py`), chosen on the compose view and changeable later from the workspace:

- `followers` — the author and their followers, read in the followers' dashboard window and on
  the author's page under People. It is also the column default, so an update written before
  `0013` keeps the audience it always had.
- `only_me` — the author alone. It stays out of every follower's window, dashboard included.
- `everyone` — any signed-in account, follower or not. This is not a firehose: the dashboard is
  still the accounts the reader follows, so the choice widens who may read a post the reader
  reached on purpose instead of broadcasting it.

`can_read_personal_post` (`app/services/authorization.py`) is the one read rule and
`visible_personal_posts_condition` is that same rule as a SQL predicate, so a list query and a
single-row read cannot drift apart. A block stays absolute and hides an `everyone` update just
as it hides a `followers` one. A community post always carries `followers`, because a
community post's audience is its community, and `ck_post_audience_scope` states that in the
schema.

Changing the audience is `POST /posts/{post_id}/audience`: the author alone, a 403 to a reader
who can see the post but did not write it, a 422 for a value outside `AUDIENCES`, and the
non-revealing 404 for anyone the read rule already keeps out. The change is journalled as
`post.audience.changed`, because who may read an update is a privacy boundary even though the
row only records the value it now holds.

Three boundaries are deliberate:

- **No reporting or moderation.** `content_reports` is community-scoped and a moderator role
  belongs to a community, so a page the author owns has no queue to report into. A personal
  update is readable only by the people its author's audience choice names. A platform-level
  report path is a product decision that is not made yet, not an oversight to patch locally.
- **No lock.** Locking exists for moderators, and there is none on a personal page; the author
  removes an update instead. The author can also remove a comment they did not write, because
  the thread is theirs.
- **Account scope, not community scope.** Audit and outbox rows for a personal update carry
  `account:<author>` rather than a community, which is what keeps them out of a community's
  trail when the author has no community at all.
