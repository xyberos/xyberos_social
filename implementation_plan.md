# Implementation Plan: Finite Social + Commercial Platform

This plan turns the product and architecture direction in [plan.md](plan.md) and the commercial architecture in [plan1.md](plan1.md) into one incremental delivery sequence. It prioritizes proving the defining behavior, user-controlled finite content, before investing in a broad social feature set or any commercial surface.

Sections 1-7 describe the social core and the remaining beta work; Phases 4-6 are already implemented in this repository. Phases 8-9 schedule the communication features the MVP deliberately left out — file and media attachments first, then audio/video calling — because they extend the conversation surface users already have. Phases 10-17 incorporate the commercial, trust, and Xyberos work from [plan1.md](plan1.md), with a small set of integration hooks pulled forward so the commercial layer can be added without reworking shipped schema. The commercial platform is sequenced after the social core, but it is not designed as a bolt-on: Phase 11's Commercial Profile, Phase 13's public index, and Phase 16's capability layer are the same primitives the social domains already use.

## 1. Delivery Principles

- Ship a usable web experience before adding mobile or desktop clients.
- Start with a modular monolith and PostgreSQL as the source of truth.
- Enforce privacy and authorization in backend use cases, not only in the UI or feed code.
- Make every feed-like result bounded, explain its scope and count, and require an explicit user action to continue.
- Defer Redis, background workers, event buses, search services, and AI until a measured product need requires them.
- Treat security, moderation, accessibility, and operational visibility as part of the feature work, not a final polish phase.
- **Business is the commercial container, not the listing.** Commercial activity hangs off a Commercial Profile and its Business, so one business can sell products, offer services, host events, and take bookings without creating a separate seller identity for each activity.
- **Marketplace is a derived read model.** The public index is eventually consistent and non-authoritative by design; checkout re-verifies against the source offering before any money moves.
- **The platform does not settle money.** A licensed payment provider authorizes, settles, refunds, and handles chargebacks. The platform owns discovery, communication, pricing state, availability, negotiation, transaction metadata, and reviews.
- **Verification is a set of capability signals, not a boolean.** Identity, ownership, phone, location, payment, business registration, and transaction history are separate trust signals that gate specific commercial permissions.
- **AI never holds privileged access.** Xyberos reaches social and commercial state only through the capability and policy layer, treats marketplace and community content as untrusted input, and recommends rather than sanctions.
- **Commercial objects are tenant-scoped and always audit-logged.** A business's records are readable and mutable only within that business scope, and privileged changes carry actor, before/after state, and request id.
- **Media bytes never live in the relational database, and an unvalidated upload is never downloadable.** Objects are stored outside PostgreSQL, referenced by metadata rows, and served only through access-checked paths. Call media is end-to-end encrypted between participants and never recorded by default; attachment bytes are deliberately server-readable so they can be scanned, and are therefore encrypted at rest and access-controlled rather than end-to-end encrypted.

## 2. Recommended MVP

The first release should feel like a communication product first: users land in their inbox, with community discussions and finite social updates as separate destinations. Chat messages remain chronological conversation history; finite-window rules apply to social updates, not ordinary message history.

### Include

- Account registration, login, logout, password recovery, and profile basics.
- A chat-first signed-in shell with persisted one-to-one and small-group text conversations, chronological messages, and read state.
- A small relationship model: follow and block. Defer friend requests until a specific use case requires mutual relationships.
- Communities with create, join, leave, visibility, and basic owner/moderator roles.
- Text posts and threaded comments; reactions can follow after the core workflow.
- A finite updates view scoped to followed accounts and joined communities.
- User-selected time range and item limit, a clear completion state, and explicit "load another batch" behavior.
- Report content, moderator removal/locking, and an auditable moderation log.
- Essential notification records for replies and moderation outcomes; defer realtime delivery.
- Account export and deletion behavior defined before beta, even if export initially supports a limited format.

### Defer

- Presence, typing indicators, message end-to-end encryption, polls, channels, and disappearing messages. File/media attachments and audio/video calling are also excluded from the MVP, but they are scheduled as Phases 8-9 rather than deferred indefinitely; Phase 9 designs its calls as end-to-end encrypted, and the device-key model it introduces is the prerequisite for later message encryption.
- Multi-node WebSocket fan-out; introduce Redis only when deployment scale requires it.
- AI/Xyberos, semantic search, recommendation systems, and personalized ranking.
- Redis, distributed event infrastructure, microservices, and multiple client applications.

This MVP is intentionally narrower than the feature inventory in [plan.md](plan.md) and [plan1.md](plan1.md). It keeps the central product promise testable and avoids making realtime messaging, media safety, or AI infrastructure prerequisites for learning from users.

### Commercial MVP boundary (later, not now)

When the social platform is validated, the smallest commercial release that proves the commercial model is:

- One Commercial Profile per account and one Business per profile.
- Offering create/update/publish for a single offering type first, then the rest.
- A derived public index with categories and one discovery surface.
- A single transaction model end to end, whichever offering type is chosen first.
- One payment provider in one currency.
- Verified transaction reviews, seller risk signals, and an operator review queue.

Explicitly out of the first commercial release: multi-currency, multiple payment providers, payout/settlement features, automated dispute resolution, automated sanctions, price/ranking experimentation, and AI-initiated commercial actions.

## 3. Decisions to Lock Before Implementation

Record these as short architecture decision records (ADRs), with a decision, alternatives, and consequences:

1. **Runtime and deployment:** decided for the MVP: Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Jinja2, and HTMX. Alpine.js is reserved for interactions that need client-side state. Hosting remains a beta-phase decision.
2. **Identity:** decided for Phase 1: normalized unique email and username, Argon2id password hashes, email verification disabled for the controlled MVP, 14-day opaque server-managed sessions stored by digest, session-bound CSRF protection, and one-time email reset tokens. Browser cookies are HttpOnly/SameSite=Lax and Secure in production; 2FA is deferred. Enable verification before public registration.
3. **Visibility and authorization:** public communities and posts are discoverable; private communities and their content are hidden from non-members. Only members can post/comment. Blocks prevent follows and hide each other's content. Owners control roles; moderators manage ordinary members and content.
4. **Chat boundary:** direct/group messages are chronological conversation history, not community posts or feed items. Only active conversation participants can read/send. A block prevents starting or sending direct messages; existing direct history remains readable. Group messages are shared with all active group participants, even if two participants block each other. Group membership changes are explicit/auditable; departures and owner removals revoke access without deleting history.
5. **Finite-window semantics:** define whether a window is a snapshot or a live query, its maximum size, stable ordering, cursor format, and what "complete" means when items are deleted or permissions change.
6. **Deletion and retention:** choose soft-delete versus hard-delete behavior, retention periods, backup implications, and which audit records must remain.
7. **Moderation operations:** define report handling, moderator permissions, appeals or reversals, and audit-log access.
8. **Commercial identity:** every commercial object is owned by a Business under a Commercial Profile. Account types are `INDIVIDUAL`, `BUSINESS`, `ORGANIZATION`, `VENUE`, `CREATOR`, `SERVICE_PROVIDER`, and `SELLER`. Formal company registration is not a prerequisite for marketplace participation; permissions are earned through verification signals and transaction history.
9. **`Offering` as the common commercial object:** `Product`, `Service`, `Event`, and `Place` are specializations of one `Offering` owned by a Business, so marketplace discovery operates on a single model while each type keeps its own transaction flow.
10. **Derived public index:** marketplace and news discovery read from a derived index that is rebuildable from the audit log and outbox. Index reads are non-authoritative; price, availability, and eligibility are re-verified against the source offering during checkout.
11. **Payments:** a licensed provider handles authorization, settlement, refunds, chargebacks, and compliance. PayMongo is the initial provider for the Philippine market, transacting in PHP. The platform stores provider and transaction metadata only, never card data. Confirm the provider contract, prohibited-category list, receipt/issuance responsibility, and refund/chargeback handling before any live payment.
12. **Trust and risk model:** verification is a set of independent signals, not a `VERIFIED` flag. Social reputation and commercial reputation stay separate. Risk engines (`BusinessRiskEngine`, `UserRiskEngine`) produce recommendations; the policy layer decides; irreversible sanctions (suspension, seizure, delisting) require an operator, a reason code, and an appeal path.
13. **Commercial data isolation:** every commercial row carries a business scope and every read/write is authorized at the data-access layer and fails closed. Cross-business access returns non-revealing errors.
14. **Client surface:** one server-rendered web application until workflows are validated. The monorepo `apps/` split (desktop, mobile, terminal) and Flutter clients stay deferred; domain code stays in one deployable backend using module boundaries.
15. **Media storage and delivery:** object bytes live in S3-compatible object storage, never in PostgreSQL. PostgreSQL stores metadata only (owner, scope, object key, mime type, size, checksum, scan state, deletion state). Uploads are validated and scan-promoted before any download is possible, delivery is always access-checked, and possession of a URL is never treated as authorization. Attachments are explicitly **not** end-to-end encrypted, because server-side scanning and moderation require readable bytes; they are encrypted at rest and access-controlled instead.
16. **Calling model (end-to-end encrypted):** calls are an extension of conversations, signaled over the existing authenticated WebSocket channel rather than a separate unauthenticated signaling surface. 1:1 calls are peer-to-peer — DTLS-SRTP directly between participants, with TURN used only as a ciphertext relay when direct connectivity fails. Group calls encrypt media client-side (SFrame over the WebRTC encoded-transform API) so the media server forwards frames it cannot decrypt. Keys are generated on participant devices, derived by ephemeral ECDH, ratcheted per sender, and rekeyed on every join and leave. The signaling channel is treated as untrusted, so the design is only honest when users can verify a per-call fingerprint. No server-side recording path exists in the codebase, no SFU is written in-house, and call metadata remains server-visible.
17. **Group call ceiling:** group calls are capped at a configured maximum participant count, enforced server-side at ring time and again at join time, set to the minimum of the per-worker authenticated WebSocket connection budget, the per-participant signaling rate budget, and the media server's E2EE room limit. Concurrent group calls per worker are capped as well, and the ceiling is published as a per-worker guarantee because it cannot be enforced globally until shared WebSocket fan-out exists. Raising it requires shared fan-out plus a larger E2EE-capable media server — not a configuration change alone.

Object storage (Phase 8) and an E2EE-capable TURN/media-server path (Phase 9) are the only infrastructure dependencies this plan accepts up front, because those features cannot exist without them; keep storage S3-compatible and the media path provider-neutral. The remaining vendor choices — search engine, event bus, CDN — stay open until a measured need appears. Payments is the other real dependency: the provider contract should be chosen before the transaction phases.

## 4. Implementation Sequence

### Phase 0: Product and technical foundation

**Deliverables:** one-page MVP specification, user journeys, ADRs for the decisions above, initial data model, API contract, threat model, and a working local development environment.

**Work:**

- Confirm the target user and the single primary journey: join communities, choose a bounded update window, review it, and finish.
- Sketch the home, community, post/comment, controls, and completed-window states, including empty, loading, error, and permission-denied states.
- Define initial entities and ownership: users, profiles, relationships, communities, memberships, posts, comments, reports, moderation events, and feed-window state if persistence is needed.
- Define how migrations, configuration, secrets, test data, and local services will work.
- Write a threat model covering account takeover, unauthorized content access, spam, malicious uploads (if later enabled), and moderator abuse.

**Exit criteria:** MVP scope and visibility rules are agreed; a developer can start the application and run tests locally; every MVP endpoint has an owner and authorization rule.

### Phase 1: Application skeleton and identity

**Status:** Implemented in the current application.

**Deliverables:** deployable application shell, database migrations, registration/login/logout, profile read/update, and account recovery path.

**Work:**

- Set up formatting, linting, type checks where applicable, unit/integration tests, CI, structured logs, and health checks.
- Add database migration tooling and constraints; avoid relying on startup-time schema creation in deployed environments.
- Implement password hashing, session lifecycle, input validation, rate limits on authentication endpoints, and safe error responses.
- Add authorization tests for user-owned profile changes and account boundaries.

**Exit criteria:** a new user can register, verify or recover an account according to the selected policy, authenticate, update a profile, and revoke their session. CI runs migrations and the test suite against a real test database.

### Phase 2: Relationships and community participation

**Status:** Implemented in the current application.

**Deliverables:** follow/block relationships, community creation and membership, and role-checked community administration.

**Work:**

- Enforce uniqueness and valid relationship transitions at the database and application layers.
- Implement public/private community access and membership changes according to the visibility ADR.
- Centralize authorization checks so list, detail, mutation, and search-like endpoints cannot disagree.
- Add audit records for privileged community role changes.

**Exit criteria:** integration tests prove that blocked users and non-members cannot bypass visibility rules through alternate endpoints; users can follow accounts and join/leave eligible communities.

### Phase 3: Posts, comments, and moderation

**Status:** Implemented in the current application.

**Deliverables:** text posts, threaded comments, reporting, moderator actions, and audit history.

**Work:**

- Add text posts and threaded comments with bounded input sizes, stable timestamps, soft removal, and indexes for expected queries.
- Enforce community membership, private-community visibility, and block filtering on post/comment reads and mutations.
- Implement report submission, moderator queues, remove/lock actions, and append-only moderation-event records.
- Add basic anti-abuse controls: endpoint rate limits, posting cooldowns, account-age policy if justified, and duplicate/report review signals.
- Keep moderation actions explainable and reversible where appropriate; do not make automated AI moderation a launch dependency.

**Exit criteria:** members can complete a discussion workflow; non-members and blocked users cannot bypass visibility rules; unauthorized users cannot moderate; reports and moderation decisions are attributable and covered by tests.

### Phase 4: Chat-first messaging

**Status:** Implemented in the current application. The signed-in landing page is the inbox; direct and small-group text conversations persist in PostgreSQL and deliver live updates over authenticated WebSockets. WebSocket fan-out is process-local and must move to shared infrastructure before multi-worker deployment.

**Deliverables:** persisted one-to-one/small-group messaging and an inbox-first signed-in experience.

**Work:**

- Add conversations, participants, messages, and per-participant read state with database constraints and stable message ordering.
- Build direct-message start from People and explicit small-group creation/invitation; keep community membership separate from chat participants.
- Make the signed-in landing page the inbox. On desktop show conversation list and active conversation; on mobile make Chat a primary navigation destination.
- Persist each message before acknowledging or broadcasting it. Add authenticated WebSocket delivery for live updates; initially support one application process and document that multi-worker deployments require shared fan-out infrastructure.
- Enforce participant-only reads/sends, membership checks on every message operation, block behavior, input/rate limits, and a clear policy for removed group members.
- Do not claim end-to-end encryption. Define retention/deletion and notification behavior before beta.

**Exit criteria:** users can start a DM, create a small group, exchange persistent messages, see read state, and reconnect without losing messages; non-participants cannot read/send; direct blocks prevent new direct delivery; removed group members lose HTTP/WebSocket access without losing transcript history; chat is the default signed-in destination.

### Phase 5: Finite content windows

**Status:** Implemented in the current application at `/updates`. Each window fixes its time bounds when started; explicit keyset batches use stable `(created_at, id)` ordering, exclude posts created after the window end, and reapply current visibility, block, and moderation rules on every request. Continuation is never automatic.

**Deliverables:** a backend window/query service and a UI that makes scope, count, limit, and completion explicit.

**Work:**

- Build retrieval from followed accounts and joined communities, applying authorization, blocks, mutes/filters that are in MVP scope, and deduplication before returning items.
- Use stable ordering and cursor-based pagination rather than offset pagination. Bound the maximum requested page size server-side.
- Decide and implement snapshot or live-window semantics. If snapshot semantics are chosen, ensure later inserts do not silently extend an already completed window.
- Return metadata such as applied scope, generated time, returned count, and continuation state. Do not claim an exact remaining count unless it can be computed correctly under permissions and concurrent changes.
- Make continuation explicit: no automatic page fetch, autoplay, or algorithmic insertion. A completed window remains complete until the user requests another one.
- Add controls for source, time range, and maximum items; show empty, filtered-to-zero, loading, error, and completed states.

**Exit criteria:** API and UI tests prove that every request is bounded, ordering is stable across pages, blocked/private content is excluded, limits cannot be bypassed, and no content loads automatically after completion.

### Phase 6: Notifications, controls, and data lifecycle

**Status:** Implemented in the current application. `/settings` centralizes update-window defaults, notification preferences, and account data actions. Replies and report decisions create preference-aware in-app notifications; the inbox and `/api/v1/notifications` use bounded cursor pagination. Account export is a private JSON download.

**Deletion and retention decision:** Account deletion is immediate and requires the current password plus typing `DELETE`. It revokes sessions and reset tokens, removes relationships/memberships/received notifications, replaces email and username with non-identifying values, clears profile text, and disables sign-in. Authored posts, comments, and conversation history remain attributed to "Deleted account"; submitted report reasons are redacted. Owned communities transfer to a moderator or oldest active member; if no other active member exists, the community and its content/audit history are removed. Database backups remain subject to the operator's backup-retention policy; this application does not rewrite existing backups.

**Work:**

- Add in-app notifications for replies and essential account/community events, with read state, category controls, and a finite list API.
- Add quiet mode and the minimum controls needed to support the MVP promise. Keep notification delivery separate from feed retrieval.
- Implement and document export and deletion workflows, including dependent content, moderation records, and backup retention.
- Add user-visible confirmation and status for destructive account actions.

**Exit criteria:** users can control or clear notification categories, request their supported data export, and initiate deletion with documented timing and consequences.

### Phase 7: Beta readiness and learning

**Deliverables:** production-like beta deployment, operational runbooks, privacy/security test evidence, and a feedback loop.

**Work:**

- Test backup restoration, migration rollback/recovery, rate limits, authorization boundaries, and expected load.
- Add error monitoring, request IDs, service health metrics, database pool monitoring, and alerts for actionable failures.
- Run accessibility checks and test keyboard navigation, screen-reader labels, and responsive layouts.
- Pilot with a small invited cohort; provide report/block and support paths before increasing access.
- Review abuse reports and user feedback on control clarity and whether completion feels trustworthy.

**Exit criteria:** restore and incident procedures are rehearsed; no unresolved critical/high security issues; beta owners and support/moderation responsibilities are assigned.

### Bridge work: integration hooks to land before any commercial phase

These are small, additive changes to the shipped social core that keep Phases 10-17 from requiring a rewrite. They ship as ordinary refactors with the existing test suite, not as a new phase.

- **Generalize the audit log.** Extend the existing community/moderation audit pattern into one append-only platform audit log recording actor, action, target, before/after state, reason, and request id. Existing community and moderation events become rows in it or remain as typed views over it.
- **Introduce request ids and idempotency keys.** Every mutating endpoint accepts and records an idempotency key; every request carries a request id into logs and audits. Payments, bookings, and order mutations depend on this later, so prove it on social mutations first.
- **Reserve a transactional outbox.** Add the outbox table and the rule that an event is written in the same transaction as its state change, and never published before that transaction commits. No publisher/worker is required yet.
- **Fix the domain event vocabulary.** Adopt the `domain.action` naming convention (`message.created`, `post.reported`, `business.verification.completed`, `offering.published`, `order.completed`) so social and commercial events share one namespace.
- **Enforce tenant/business scoping at the data-access layer.** Add the scope predicate to the repository/session helpers now, even with a single tenant, so crossing scopes fails closed when commercial tables arrive.
- **Sketch module boundaries, not services.** Landed: the backend is one deployable app with `core/` (config, database, security, observability, mail), `api/` (middleware, dependencies, responses, and one route module per domain), `models/`, `schemas/` (JSON contracts), and `services/` (domain rules shared across routes). Keep future domains — `social`, `feed`, `trust`, `business`, `offerings`, `marketplace`, `transactions`, `xyberos` — as modules and route files inside that layout. Do not create `packages/` or separate `services/` deployables yet.
- **Keep the index rebuildable.** Any new derived read (unread counts, completion state) must be reconstructible from authoritative rows, because Phase 11 extends this to the public index.

### Phase 8: Media and file attachments

**Status:** Not started. Extends the Phase 4 conversation surface.

**Deliverables:** an object-storage-backed attachment pipeline, attachments in direct and small-group conversations, and reuse of the same pipeline for post and comment media once chat proves it.

**Work:**

- Keep every byte in S3-compatible object storage and store metadata only in PostgreSQL (owner, scope, object key, mime type, declared and detected size, checksum, scan state, created_at, deletion state).
- Use a two-step upload: an authenticated request issues a short-lived, scoped upload target, then an idempotent finalize call verifies the object exists, compares detected type and size against the declaration, strips image metadata, and only then attaches it to a message. Unfinished uploads expire.
- Gate download on scan state. Objects move `PENDING → SCANNED | REJECTED`, nothing is downloadable while `PENDING`, and malware scanning runs before promotion. Allowlist mime types, cap per-file size, attachment count per message, and per-account quota, and surface quota failures as explicit errors.
- Make delivery authorization-checked: no public bucket and no permanent unauthenticated URL. Serve through the application or short-lived signed URLs, and re-check conversation participation, community visibility, and block rules at download time.
- Render safely: inline only for a narrow image (later possibly PDF) allowlist, everything else sent with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`, served from a separate origin with a restrictive CSP, and SVG/HTML rejected outright.
- Rate limit uploads and reject oversize bodies before buffering them into memory.
- Delete objects with their content. Message deletion, moderation removal, and account deletion revoke or remove the object and its derivatives per the retention ADR, while noting that operator backups are not rewritten.
- Extend the messaging contract: attachment metadata is persisted and broadcast only after the object is finalized and scan-clean; the client shows upload progress, retries on failure, and never autoplays audio or video attachments inline.
- State the deliberate boundary in code and docs: attachments are not end-to-end encrypted, because scanning and moderation require readable bytes (Decision 15). Do not describe attachments with the confidentiality language used for calls.

**Exit criteria:** only authorized participants can download an attachment, proven by tests that attempt direct object access, guessable keys, and cross-conversation reads; `PENDING` and `REJECTED` objects are never downloadable; declared-type spoofing, oversize uploads, and quota exhaustion are rejected server-side and cannot be bypassed by a modified client; replayed finalize calls do not create duplicates; deleting a message or an account removes access to the underlying object.

**Deferred within this phase:** voice messages, video transcoding and adaptive streaming, image derivatives and CDN optimization, resumable multipart uploads, and media inside emails or account exports.

### Phase 9: Audio and video calling

**Status:** Not started. Depends on Phase 8 for the media pipeline conventions and on the Phase 4 WebSocket channel for signaling. End-to-end encryption (Decision 16) and the group ceiling (Decision 17) are prerequisites, not follow-ups: the topology, key handling, and admission control are decided and tested as part of this phase.

**Deliverables:** end-to-end-encrypted 1:1 audio and video calls between conversation participants; group calls with an enforced participant ceiling and client-side media encryption; persisted call records with missed-call notifications; a documented TURN/media-server deployment; and a tested browser support matrix.

**Work:**

*Confidentiality design (Decision 16)*

- Treat end-to-end encryption as a design requirement rather than an option. Keys are generated on participant devices and never leave them; the server, the TURN relay, and the group media server must be unable to decrypt call media, and no server-side recording path may exist anywhere in the codebase.
- Keep 1:1 calls peer-to-peer with TURN as a fallback relay only. DTLS-SRTP between peers is already end-to-end, and a TURN relay forwards ciphertext it cannot read, so 1:1 calls are genuinely encrypted without extra machinery — verify this with a relay-only test rather than assuming it.
- Make group media unreadable to the media server: encrypt frames client-side with SFrame over the WebRTC encoded-transform API (`RTCRtpScriptTransform`) so the server forwards ciphertext instead of decode-and-forward media. Do not invent a format; implement a reviewed one.
- Derive per-call keys by ephemeral ECDH (P-256 or X25519) between participants, wrap per-sender frame keys for each recipient, and ratchet per sender. Record the exact construction in the ADR, including how the first participant's keys reach the second.
- Treat the signaling channel as untrusted and prove otherwise per call: publish a per-call fingerprint/safety number derived from the exchanged public keys, with a compare-out-of-band flow. Key exchange rides the authenticated WebSocket, so verification is what makes the encryption claim honest rather than a marketing statement.
- Rekey on every participant join and leave so a departed participant cannot decrypt later media. Per-recipient key wrapping grows roughly with the square of the participant count, which is the real reason the participant ceiling exists.
- State the limits plainly in user-facing copy and in the docs: the server still sees call metadata (who, whom, when, duration, participant list, connection metadata) and, for group calls, packet sizes and timing. End-to-end encryption does not hide traffic analysis, and it makes server-side content moderation and server-side recording impossible by construction.

*Signaling and the group call ceiling (Decision 17)*

- Signal over the existing authenticated WebSocket channel (offer, answer, ICE candidates, key-wrapping envelopes), authorized by conversation participation and the same block rules as messaging. There is no separate unauthenticated signaling endpoint, and signaling carries no call content.
- Enforce the group call ceiling instead of documenting the fan-out limit as a caveat. Each participant holds one authenticated WebSocket connection and a bounded signaling rate, so set the maximum participants per call to the minimum of (a) the per-worker authenticated WebSocket connection budget, (b) the signaling rate budget divided by the per-participant signaling cost, and (c) the media server's E2EE room limit; start from a default of 8 participants and justify any change with measurements. Cap concurrent group calls per worker as well.
- Enforce the ceiling server-side at ring time and again at join time, with admission control and a clear non-enumerating "call is full" error, never only in the UI. Track per-worker call occupancy in-process for now and document that the ceiling is a per-worker guarantee until shared fan-out and shared occupancy state exist.
- Rate limit signaling per participant per call. ICE candidate floods and renegotiation loops are the cheapest way to exhaust the socket budget, and they are exactly what the ceiling protects.

*Operations, reliability, and controls*

- Issue short-lived per-session TURN credentials (time-limited HMAC credentials, never static secrets), and choose deliberately between a hosted relay and a self-run one, since relay egress is the main cost driver.
- Model call state explicitly (`RINGING → ACCEPTED → ENDED`, with `DECLINED`, `MISSED`, `BUSY`, and `FAILED` branches) plus per-participant join/leave records. Persist a call record (conversation, initiator, participants, started_at, ended_at, end reason) so missed calls can notify; persist no media.
- Treat browser realities as first-class states and publish a tested support matrix for the encoded-transform API: microphone and camera permission denial, device selection and switching, mute and camera-off indicators, autoplay restrictions, and iOS Safari behavior all get explicit UI states. Where group E2EE is unavailable, that client cannot join group calls — no silent downgrade to server-readable media — while 1:1 calls still work peer-to-peer. Ringing always requires an explicit accept; nothing auto-answers.
- Reuse the settings model for "who can call me", and treat blocks as absolute: a blocked user can neither place nor receive a call.
- Monitor join latency, ICE failure rate, relay usage share, packet loss, dropped-call rate, and E2EE-unavailable events; degrade to audio-only when bandwidth is poor instead of failing hard.
- Bound cost and abuse: per-account fair-use limits, rate limits on call initiation, and alerting on relay egress. Participant counts are governed by the ceiling above rather than by ad-hoc limits.
- Handle device keys honestly: keys live per device, so multi-device call participation is out of scope for this phase and one active device per account is the documented behavior. The device-key model built here is the prerequisite for message encryption later, not a substitute for it.
- Do not encode a server-side recording path at all. Client-side recording, if it ever ships, requires explicit multi-party consent, a visible indicator, and the Phase 8 storage lifecycle.

**Exit criteria:** two users complete a 1:1 audio call and a 1:1 video call across different networks, including a relay-only path where direct connectivity fails, with media proven to be peer-encrypted; a group call runs within the configured ceiling and a test capturing relayed traffic plus the media server's own logs, storage, and process state finds no key material and no plaintext media; the fingerprint flow detects a key substituted during signaling in a test; a participant who leaves cannot decrypt frames sent after the rekey, proven by test; the participant ceiling and concurrent-call cap are enforced server-side, including a rejected over-ceiling join that returns a non-enumerating error; blocked users cannot call or be called; a declined or unanswered call notifies the recipient without exposing content; no call media is stored and no server-side recording path exists; the browser support matrix and the metadata caveats are documented in user-facing copy; tests cover authorization, state transitions, expired TURN credentials, and permission-denied flows.

### Phase 10: News aggregation and discussions

**Deliverables:** attributed news ingestion, article metadata, a derived news index, and finite discussions attached to articles.

**Work:**

- Ingest from a small, explicit source list; store source attribution, canonical URL, and retrieval time for every article.
- Keep the original article and the user discussion as separate records. A discussion never rewrites, replaces, or impersonates the source, and the distinction is visible in the UI.
- Reuse the Phase 5 window contract for news: bounded batches, explicit continuation, no autoplay.
- Serve cached reads when ingestion or the index is unavailable instead of an error page.
- Defer crawl scale, article full-text search, and news recommendations.

**Exit criteria:** users can browse a bounded news window, open discussions on an article, and always see the source and attribution; ingestion failure degrades to cached reads; discussion content is never presented as the publisher's content.

### Phase 11: Commercial foundation

**Deliverables:** Commercial Profile, Business identity with ownership and staff, locations, capability-based verification, and commercial reputation/risk scaffolding.

**Work:**

- Add the Commercial Profile as the user-owned container for commercial activity, and Business as the container that owns offerings and transactions. An account with a Commercial Profile can list a used laptop; a "marketplace listing" without a commercial owner is not a valid state.
- Support the account types from Decision 8 with audited, explicit transitions, multiple owners, and staff roles.
- Model verification as independent signals (`identity`, `ownership`, `phone`, `location`, `payment`, `business_registration`, `transaction_history`). Derive commercial permissions from signals plus risk, never from a single verified flag.
- Keep social reputation and commercial reputation as separate dimensions that do not implicitly determine each other.
- Record risk signals: account age, identity/business verification, transaction history, refunds, chargebacks, complaints, review patterns, duplicate business information, rapid listing creation, suspicious messaging, abnormal transaction behavior, device reputation, message and listing velocity, reports, blocks, and prior sanctions.
- Enforce business scope at the data-access layer and fail closed; audit every privileged commercial change.
- Add graduated limits: new sellers get limited listings, and limits expand with successful transactions, reputation, and account age rather than jumping at verification.

**Exit criteria:** a user can create a Commercial Profile and Business, attach staff, and request verification; no user can read or mutate another business's records by any path, including list, detail, and search-like endpoints; every permission decision traces to a recorded signal; escalation and de-escalation of commercial limits are tested.

### Phase 12: Offerings

**Deliverables:** one `Offering` model with `Product`, `Service`, `Event`, and `Place` specializations, availability, and the commercial event vocabulary.

**Work:**

- Model `Offering` as the common commercial object owned by a Business (type, title, description, price, currency, status, availability reference), with per-type specialization for products, services, events, and places.
- Give availability its own record bound to services, places, and events; capacity and scheduling rules stay with the offering type, not the marketplace.
- Enforce ownership, plan and risk limits, price validation (non-negative, currency-consistent), and deterministic duplicate-listing checks at both the database and use-case layers.
- Emit `business.*`, `offering.*`, `product.*`, `service.*`, `event.*`, `place.*`, and `availability.*` events to the audit log and outbox within the committing transaction.
- Keep unpublished, delisted, and suspended offerings out of every read path, including public pages and the future index.

**Exit criteria:** a business can manage offerings of each type; invalid states (unowned offering, negative price, illegal status transition, unpublished offering visible to non-owners) are impossible to store; every state change is audited and replayable through the outbox.

### Phase 13: Public index and marketplace discovery

**Deliverables:** a derived public index, categories, search, discovery surfaces for products/services/events/places/businesses/nearby, and trust signals in listing views.

**Work:**

- Build the index as a derived read model fed from the outbox. It is rebuildable, eventually consistent, and never authoritative; a full rebuild from authoritative rows must be a tested operation.
- Re-verify price, availability, and seller eligibility against the source offering before checkout. Index reads must never be the basis for charging money.
- Provide discovery that is explicit and user-controlled: source/type/category filters, bounded result sets with the Phase 5 window contract, and no engagement-optimized ranking. If ranking exists, it is deterministic, documented, and inspectable.
- Surface trust signals (verification badges, completed transaction counts, dispute history summary) next to listings.
- Apply seller and listing limits, category restrictions, and blocklists from Phase 11 policy at index write and read time.
- Serve cached index reads when the index is unavailable rather than failing the page.

**Exit criteria:** a signed-in user can find an offering with filters and understand why it is shown; unpublished or suspended offerings disappear from the index within a bounded, tested delay (including after a rebuild); no index read can be used as an authoritative price or availability source; index lag and rebuild time are measured.

### Phase 14: Orders, bookings, tickets, and payments

**Deliverables:** per-offering-type transaction flows, availability-aware bookings, tickets, and provider-backed payments with refunds and disputes.

**Work:**

- Do not collapse every commercial activity into one `Order`. Product → cart → order → payment → delivery → completed; Service and Place → availability → booking → confirmation → completion; Event → ticket/reservation → payment → attendance.
- Model the booking state machine explicitly (`REQUESTED → PENDING → CONFIRMED → IN_PROGRESS → COMPLETED → REVIEWED`, with `CANCELLED`, `DECLINED`, `EXPIRED`, `DISPUTED` branches) and make illegal transitions impossible at the database and use-case layers.
- Integrate one licensed provider in one currency (PayMongo, PHP). The platform stores provider references and transaction metadata and never card data.
- Process provider webhooks idempotently with signature verification, and reconcile provider state before displaying a paid or settled status. A replayed webhook must not double-credit.
- Bound holds and expirations: cart TTLs, reservation expiry, and ticket release are explicit and tested.
- Allow reviews only from participants of a completed transaction, one review per transaction, editable within a bounded window. This is the main defense against fake reviews.
- Route refunds, cancellations, disputes, and chargebacks through provider truth, with a manual operator path before any automation.

**Exit criteria:** a test-mode purchase and a test-mode booking each complete end to end with idempotent retries; unauthorized users cannot read or mutate another business's or buyer's transaction; only transacting participants can review; refunds and disputes have an attributable audit trail; no card data is stored or logged.

### Phase 15: Commercial trust, safety, and risk expansion

**Deliverables:** risk engines, fraud and abuse detection, commercial moderation operations, and appeals.

**Work:**

- Implement `BusinessRiskEngine` and `UserRiskEngine` over the Phase 11 signal sets. Output is a recommendation with an explanation; the policy layer decides.
- Keep sanctions graduated and reversible where appropriate: restrict, delist, suspend, with reason codes, evidence, and an appeal path. No single signal triggers an irreversible action.
- Add marketplace-specific anti-abuse: price anomaly detection, duplicate listing detection, fake-review detection, listing and messaging velocity limits, suspicious-messaging detection, category restrictions, seller limits, and blocklists.
- Provide operator queues for fraud and disputes, with the same audit standards as community moderation.
- Defer ML models, coordinated-abuse graph analysis, and any automated sanction.

**Exit criteria:** risk recommendations are reviewable, explainable, and reversible; restricted sellers can appeal and the reversal is recorded; abuse reports and sanctions are attributable and covered by tests; no automated sanction occurs without a recorded policy decision.

### Phase 16: Xyberos foundation

**Deliverables:** intent engine, memory, knowledge, capability registry, context trust classification, tool firewall, sandbox, and the confirmation system.

**Work:**

- Register capabilities under the existing authorization layer (`commercial.create_profile`, `commercial.create_product`, `commercial.create_service`, `commercial.create_event`, `commercial.create_place`, `commercial.publish`, `commercial.unpublish`, `commercial.update_availability`, `commercial.create_booking`, `commercial.cancel_booking`, `commercial.create_order`, `commercial.cancel_order`, `commercial.view_transactions`, `commercial.create_review`). Every action goes capability → policy → domain service.
- Set confirmation tiers: listing creation may be automatic; publishing, price changes, booking cancellation, order creation, and accepting bookings are configurable; payments require mandatory confirmation.
- Classify context as `TRUSTED`, `UNTRUSTED`, `MIXED`, or `SENSITIVE`. Marketplace listings, community posts, and news content are untrusted: text inside them is data, never an instruction.
- Add the sandbox and tool firewall. Xyberos has no direct database access and gains no privilege from being an AI agent.
- Scope memory and knowledge per user, opt-in, and subject to the same export and deletion lifecycle as other account data.

**Exit criteria:** Xyberos cannot perform a mutating action without passing capability authorization, policy, and any required confirmation; adversarial listing or comment content cannot escalate capability or exfiltrate another user's data (tested adversarially); AI-initiated state changes are audited identically to human ones.

### Phase 17: Xyberos applications

**Deliverables:** user-facing AI features across chat, social, communities, news, business, marketplace, bookings, and orders.

**Work:**

- Compile natural-language constraints into a `ContentWindow` ("only programming posts from the last three days, maximum 15") and let the deterministic engine execute them. AI never fetches content outside the user's authorized scope.
- Provide digests and summaries over bounded windows, translation, and semantic search over already-authorized content only.
- Keep moderation assistance advisory: Xyberos recommends, a human or an explicit policy decides.
- Define failure modes: cost-bounded execution, refusal to exceed policy, and safe degradation to the deterministic path when the model is unavailable.

**Exit criteria:** every AI surface is opt-in and output-bounded; AI cannot extend a window, autoplay, or continue without user action; summaries never reveal content the requesting user cannot read; disabling AI leaves all core workflows intact.

### Mapping from plan1.md phases

| plan1.md phase | Merged phase |
| --- | --- |
| 0 Platform + security foundation | Phases 0-1 (implemented), extended by the bridge work |
| 1 Platform runtime | Phase 1 (implemented); object storage arrives with Phase 8, Redis and workers still wait for measured need |
| 2 Identity, sessions + recovery | Phase 1 (implemented); 2FA and device registry deferred |
| 3 Messaging | Phase 4 (implemented); file/media attachments → Phase 8, end-to-end-encrypted audio/video calls → Phase 9; presence, offline queues, multi-device sync, and message E2EE remain deferred |
| 4 Social | Phase 2 (implemented, minus friend requests and mutes) |
| 5 Communities | Phase 3 (implemented, minus reactions and voting) |
| 6 Public index + finite feed | Phase 5 (finite windows implemented); public index moved to Phase 13 |
| 7 News | Phase 10 |
| 8 Commercial foundation | Phase 11 |
| 9 Marketplace | Phases 12-13 |
| 10 Bookings + orders | Phase 14 |
| 11 Trust & safety expansion | Phase 15 |
| 12 Xyberos foundation | Phase 16 |
| 13 Xyberos applications | Phase 17 |

## 5. Data and API Guidance

- Keep domain modules inside one deployable backend initially; separate API transport, application use cases, domain rules, and persistence access without creating a layer for its own sake. The current split is described in [README.md](README.md#backend-layout).
- Treat PostgreSQL as authoritative. Add Redis or a worker only when a measured latency, fan-out, or scheduling need appears.
- Use foreign keys, uniqueness constraints, check constraints, and migrations to make invalid states difficult to store.
- Index the actual MVP access paths, especially membership/relationship lookups and ordered post retrieval; verify choices with query plans and representative data.
- Use a transactional outbox only when reliable asynchronous side effects are introduced. Do not publish an event before its database change is committed.
- Keep API versioning and pagination contracts consistent. Return authorization-safe errors and avoid leaking whether a private resource exists.
- Store media outside the relational database (Decision 15, Phase 8); validate, scan, quota, and delete uploads through that pipeline before any download path exists.
- **Call data and keys:** persist call records, participants, and timestamps only. Media keys are generated on devices and must never be stored, logged, derivable, or transportable server-side; call media never touches application storage or the database; TURN credentials are short-lived secrets that are never logged. Assume call metadata (participants, timing, connection metadata) stays server-visible, and document that next to the encryption claim rather than hiding it.
- **Authoritative vs derived:** businesses, offerings, availability, and transactions live in PostgreSQL. The public index, unread counts, and completion state are derived and must be rebuildable from authoritative rows plus the audit log.
- **Money-adjacent data:** order totals, paid state, refunds, and disputes derive from provider events, not client input. Store provider ids and idempotency keys; never store card data, and never log payment payloads.
- **Tenant scoping:** every commercial query carries a business scope predicate applied in the data-access layer. Crossing scopes fails closed and returns a non-revealing error.
- **Transaction state machines:** model each flow's legal transitions explicitly and reject illegal ones in the database and the use case. Availability holds and expirations are explicit records with TTLs, not implicit behavior.
- **Idempotency:** every payment webhook, booking, and order mutation is replay-safe and keyed.
- **Untrusted content:** treat listing descriptions, posts, comments, and news bodies as untrusted data everywhere they are rendered or fed to AI, and keep that boundary explicit in the code.

## 6. Quality Gates

Every phase should meet these gates before it is considered complete:

- **Behavior:** acceptance tests cover successful flows, boundary conditions, and failure paths.
- **Authorization:** tests cover owner, member, moderator, blocked user, and anonymous access where relevant.
- **Data integrity:** migrations apply cleanly to an empty database and from the previous release schema.
- **Security:** secrets are not committed; authentication and mutation endpoints have abuse controls; sensitive values are not logged.
- **Usability:** loading, empty, error, and completion states are present; core workflows work by keyboard and on narrow screens.
- **Operations:** health checks, structured logs, backup/restore expectations, and an owner for production alerts are documented.

Additional gates for the media and calling phases (8-9):

- **Upload safety:** no stored object is downloadable before scan promotion; declared type, detected type, and size are all verified server-side; SVG and HTML are rejected; image metadata is stripped.
- **Media authorization:** tests prove object storage is not publicly readable, keys are unguessable and non-enumerable, and every download, derivative, and export path re-checks conversation or community membership and blocks.
- **Quota and abuse:** per-file, per-message, and per-account limits are enforced server-side and tested; upload and call-initiation endpoints are rate limited.
- **Call confidentiality:** only conversation participants can ring or join; blocks are absolute; media keys exist only on participant devices, proven by a test that inspects relayed traffic, logs, stored rows, and the media server process for key material or plaintext; the fingerprint flow detects substituted keys; rekey excludes departed participants; no server-side recording path exists; TURN credentials expire; permission-denied and ICE-failure paths have explicit, tested UI states.
- **Ceiling enforcement:** the group participant ceiling and the concurrent-call cap are enforced server-side at ring and join time, tested with an over-ceiling join, and documented as per-worker guarantees wherever shared fan-out does not yet exist.
- **Lifecycle:** content deletion, moderation removal, and account deletion all revoke access to the object and its derivatives, with the lag measured.

Additional gates for the commercial phases (10-17):

- **Commercial authorization:** tests prove one business cannot read, mutate, or infer another business's offerings, availability, orders, or customers through any read path, including the public index.
- **Money correctness:** source re-verification before checkout, idempotent provider webhooks, explicit refund and dispute paths, reconciliation on startup after an outage, and no card data at rest or in logs.
- **Auditability:** every privileged commercial, moderation, and AI-initiated change records actor, action, target, before/after state, reason, and request id.
- **Derived-data integrity:** the public index and notification counts can be rebuilt from authoritative rows, with a test that proves it and a measured rebuild time.
- **AI safety:** adversarial content tests (untrusted listing, post, and news text cannot trigger capabilities), confirmation enforced for high-impact actions, and a working deterministic path when AI is unavailable.

## 7. Success Measures

Measure whether the product delivers control and reliable communication, not whether it maximizes time spent.

- **Finite retrieval:** percentage of windows that reach an explicit completion state; rate of unexpected automatic continuation should be zero.
- **User control:** use of source/limit/quiet controls, and user-reported confidence in what the window includes.
- **Core utility:** successful conversations, delivered messages, community joins, posts, replies, and return visits chosen by users.
- **Safety:** report response time, repeat-abuse rate, block effectiveness, and authorization incidents.
- **Reliability:** API error rate, latency for key queries, migration failures, and restore-test results.

Additional measures for the media and calling phases (8-9):

- **Media:** attachments sent per conversation, upload rejection and quarantine rate with reasons, download authorization denials, storage per active user, and deletion lag between content deletion and object removal.
- **Calling:** call setup success rate, relay-only share, median join latency, dropped-call rate, permission-denied rate by browser, fingerprint verification rate, and over-ceiling admission denials, which should stay rare and always be explained to the user who hit them.

Additional measures for the commercial phases:

- **Commercial completion:** offerings published, discovery reads that lead to a viewed offering, transactions and bookings completed, and cancellation/refund reasons.
- **Trust:** share of reviews from verified completed transactions, dispute and chargeback rate, listing removals by reason, and appeal reversal rate.
- **Money handling:** settlement reconciliation mismatches, webhook replay rejections, and time to refund.
- **AI:** capability denials due to policy, confirmation abandonment, and context-trust violations caught by tests.

Do not treat session duration, raw scroll depth, notifications sent, listing impressions, call duration, or AI usage volume as primary success metrics; each pushes the architecture back toward the engagement model the product rejects. Call volume and media volume are cost and reliability inputs, not goals.

## 8. Improvements to the Source Plans

### 8.1 Improvements to plan.md

- **Make chat first-class without merging domains.** Inbox is the signed-in home; conversations remain distinct from community threads. Build persisted messaging before finite updates, while deferring presence, calls, E2EE, and multi-node fan-out.
- **Define "finite" precisely.** A `limit` and `has_more` flag alone do not define a stable window. Specify snapshot/live semantics, cursor behavior, concurrent inserts, permission changes, and when completion is final.
- **Resolve privacy before schema expansion.** Visibility is listed as a requirement but not fully defined for relationships, private communities, blocks, comments, and deleted content. Write policy tables and authorization tests before exposing endpoints.
- **Avoid premature infrastructure.** Redis, Celery/Arq, event buses, search engines, CDN, and microservices add operational cost. Introduce them only for a concrete requirement and retain PostgreSQL as the initial source of truth.
- **Treat security and moderation as release criteria.** Password hashing alone is not a security plan. Add session/recovery policy, authorization testing, abuse controls, reporting, audit, backup, and incident ownership to the build sequence.
- **Be cautious with estimates.** The source plan's week ranges do not state team size, existing code, or deployment assumptions. Estimate after repository and team discovery; track scope and dependencies rather than promising calendar dates now.
- **Separate product measures from growth measures.** Define completion, control, safety, and reliability measures up front; avoid engagement metrics that contradict the product's stated values.
- **Defer AI and end-to-end encryption deliberately.** Both require threat, privacy, and operational design. Keep AI advisory and opt-in; do not imply private messaging is end-to-end encrypted until it is correctly implemented and independently reviewed. Phase 9 applies this rule rather than working around it: call encryption is specified up front with key verification, rekey-on-leave, a published browser support matrix, and explicit metadata caveats, and it makes no claim about message content.

### 8.2 Reconciliation with plan1.md

**Adopted as-is.** The commercial domain model (Commercial Profile → Business → Offering → Product/Service/Event/Place → Order/Booking/Ticket → Transaction → Review → Reputation); marketplace as a derived, non-authoritative discovery layer with re-verification at checkout; capability-based verification instead of a `VERIFIED` flag; separate social and commercial reputation; verified transaction reviews as an anti-fake-review control; the commercial event vocabulary and append-only audit log; per-offering-type transaction flows and the booking state machine; capability/policy mediation for AI with confirmation tiers; untrusted-context classification for marketplace and community content.

**Adopted with a change.**

- *"Integrate commercial into the core rather than as a later add-on"* is satisfied structurally, not sequentially: the Commercial Profile and Business abstractions, the derived-index pattern, capability authorization, auditing, and tenant scoping are all extensions of primitives the shipped social core already uses (owner-scoped resources, visibility rules, moderation events, finite windows). The commercial phases therefore do not require reworking identity, chat, communities, or the window engine. Commercial work is deferred in *time* because the social core is already built and validated; it is not deferred in *architecture*.
- *Estimate-free sequencing* is kept from Section 8.1; plan1.md's phase list implies a much larger team than the current repository demonstrates.
- *Payments* is the one item pulled earlier than "later phase": the provider choice (PayMongo, PHP) is a real external dependency with contract lead time, so it is locked as an ADR now even though integration happens in Phase 14.
- *"Verification becomes capability-based"* is implemented as recorded signals that gate specific permissions, not as a checklist whose display implies full trust.
- *plan1.md's media architecture* (object bytes in object storage, metadata rows in PostgreSQL, upload validation, scanning, quotas, and deletion) is adopted as the structure for Phase 8, but CDN delivery, image derivatives, video transcoding, voice/video messages, and resumable uploads wait for measured need.
- *plan1.md's calling material* is adopted as intent, not as design: calls stay bound to conversations, but the topology is decided in Phase 9, group media goes through an E2EE-capable media server rather than a peer mesh, the participant ceiling is derived from the WebSocket signaling budget (Decision 17), and recording is out of scope. plan1.md listed end-to-end encryption as a later milestone for messaging; this plan keeps that for messages and moves encryption into the call design itself, where the 1:1 case gets it without a media server.

**Rejected or deferred.**

- *`packages/` and `services/` deployables, `infrastructure/kubernetes`, and the `apps/` client split* are replaced by module boundaries inside one deployable backend. Revisit only when a measured need appears.
- *Redis for cache, presence, and rate limits; CDN; background workers* are deferred per Section 1. The two exceptions are now accepted dependencies rather than deferred ones: S3-compatible object storage (Decision 15, Phase 8) and a TURN/media-server path (Decision 16, Phase 9). CDN and derivative optimization still wait for measured need, and shared WebSocket fan-out becomes a prerequisite at the second application worker.
- *Realtime-heavy messaging features* are no longer uniformly deferred: attachments ship in Phase 8 and end-to-end-encrypted calling in Phase 9. Offline message queues, multi-device synchronization, presence and typing indicators, disappearing messages, and *message* end-to-end encryption all remain deferred; the device-key model introduced for call encryption is the prerequisite for message encryption, not a substitute for it.
- *Encryption claims are scoped to what is implemented.* Phase 9 calls are end-to-end encrypted by design (Decision 16): 1:1 media is DTLS-SRTP between peers with TURN relaying ciphertext, and group media is SFrame-encrypted client-side so the media server forwards frames it cannot decrypt. Two caveats ship with the claim: key exchange rides an untrusted signaling channel and is only trustworthy when participants verify the fingerprint, and call metadata stays server-visible. Phase 8 attachments are deliberately *not* end-to-end encrypted, because server-side scanning is the point; they are encrypted at rest and access-controlled. Message end-to-end encryption remains a separate milestone with its own threat model and review.
- *Automated fraud sanctioning, ML moderation, Sybil/coordinated-abuse graph analysis, and recommendation ranking* are deferred to Phase 15 or later, and remain advisory when introduced.
- *The `protocol/`, `crypto/`, and `p2p/` layers from earlier drafts of plan1.md* are already removed by plan1.md itself; this plan keeps the conventional server-authoritative model with PostgreSQL plus an outbox.

## 9. Immediate Next Actions

1. Start Docker Desktop and verify the PostgreSQL/Mailpit Compose setup plus the migration locally; CI already runs the PostgreSQL migration.
2. Select hosting and environment/secrets management before beta deployment.
3. Agree on privacy/visibility rules and write the authorization ADR before community endpoints.
4. Validate Phase 4's single-process WebSocket behavior on the intended PostgreSQL deployment and choose shared fan-out infrastructure before scaling workers.
5. Proceed to Phase 7 beta readiness: rehearse restore/recovery, add operational monitoring, complete accessibility checks, and prepare an invited pilot.
6. Land the bridge work while the social core is still the only consumer: generalize the audit log, add request ids and idempotency keys, reserve the transactional outbox, and enforce tenant scoping in the data-access layer.
7. Write the commercial, media, and calling ADRs (Decisions 8-16) and confirm the payment provider contract, prohibited categories, and receipt/refund responsibilities before any offering or transaction schema is created.
8. Choose the object storage provider and malware-scanning approach for Phase 8, then an E2EE-capable media server (one that forwards client-encrypted frames) and a hosted or self-run TURN relay for Phase 9. Confirm the encoded-transform browser support matrix, fix the group participant ceiling from the measured per-worker WebSocket budget and signaling rate, and agree on the media retention and quota policy. Phases 8-9 are blocked on those decisions and on the call-encryption ADR, not on code.
