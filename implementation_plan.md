# Implementation Plan: Finite Social Communication Platform

This plan turns the product and architecture direction in [plan.md](plan.md) into an incremental delivery sequence. It prioritizes proving the defining behavior, user-controlled finite content, before investing in a broad social feature set.

## 1. Delivery Principles

- Ship a usable web experience before adding mobile or desktop clients.
- Start with a modular monolith and PostgreSQL as the source of truth.
- Enforce privacy and authorization in backend use cases, not only in the UI or feed code.
- Make every feed-like result bounded, explain its scope and count, and require an explicit user action to continue.
- Defer Redis, background workers, event buses, search services, and AI until a measured product need requires them.
- Treat security, moderation, accessibility, and operational visibility as part of the feature work, not a final polish phase.

## 2. Recommended MVP

The first release should feel like a communication product first: users land in their inbox, with community discussions and finite social updates as separate destinations. Chat messages remain chronological conversation history; finite-window rules apply to social updates, not ordinary message history.

### Include

- Account registration, login, logout, password recovery, and profile basics.
- A chat-first signed-in shell with persisted one-to-one and small-group text conversations, chronological messages, and read state.
- A small relationship model: follow and block. Defer friend requests until a specific use case requires mutual relationships.
- Communities with create, join, leave, visibility, and basic owner/moderator roles.
- Text posts and threaded comments; reactions can follow after the core workflow.
- A finite updates view scoped to followed accounts and joined communities.
- User-selected time range and item limit, a clear completion state, and explicit “load another batch” behavior.
- Report content, moderator removal/locking, and an auditable moderation log.
- Essential notification records for replies and moderation outcomes; defer realtime delivery.
- Account export and deletion behavior defined before beta, even if export initially supports a limited format.

### Defer

- Presence, typing indicators, calls, end-to-end encryption, media/file uploads, video, polls, channels, and disappearing messages.
- Multi-node WebSocket fan-out; introduce Redis only when deployment scale requires it.
- AI/Xyberos, semantic search, recommendation systems, and personalized ranking.
- Redis, distributed event infrastructure, microservices, and multiple client applications.

This MVP is intentionally narrower than the feature inventory in [plan.md](plan.md). It keeps the central product promise testable and avoids making realtime messaging, media safety, or AI infrastructure prerequisites for learning from users.

## 3. Decisions to Lock Before Implementation

Record these as short architecture decision records (ADRs), with a decision, alternatives, and consequences:

1. **Runtime and deployment:** decided for the MVP: Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Jinja2, and HTMX. Alpine.js is reserved for interactions that need client-side state. Hosting remains a beta-phase decision.
2. **Identity:** decided for Phase 1: normalized unique email and username, Argon2id password hashes, email verification disabled for the controlled MVP, 14-day opaque server-managed sessions stored by digest, session-bound CSRF protection, and one-time email reset tokens. Browser cookies are HttpOnly/SameSite=Lax and Secure in production; 2FA is deferred. Enable verification before public registration.
3. **Visibility and authorization:** public communities and posts are discoverable; private communities and their content are hidden from non-members. Only members can post/comment. Blocks prevent follows and hide each other's content. Owners control roles; moderators manage ordinary members and content.
4. **Chat boundary:** direct/group messages are chronological conversation history, not community posts or feed items. Only active conversation participants can read/send. A block prevents starting or sending direct messages; existing direct history remains readable. Group messages are shared with all active group participants, even if two participants block each other. Group membership changes are explicit/auditable; departures and owner removals revoke access without deleting history.
5. **Finite-window semantics:** define whether a window is a snapshot or a live query, its maximum size, stable ordering, cursor format, and what “complete” means when items are deleted or permissions change.
6. **Deletion and retention:** choose soft-delete versus hard-delete behavior, retention periods, backup implications, and which audit records must remain.
7. **Moderation operations:** define report handling, moderator permissions, appeals or reversals, and audit-log access.

Do not lock the system into a vendor-specific object store, search engine, or event bus before the MVP needs it.

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

**Deliverables:** deployable application shell, database migrations, registration/login/logout, profile read/update, and account recovery path.

**Work:**

- Set up formatting, linting, type checks where applicable, unit/integration tests, CI, structured logs, and health checks.
- Add database migration tooling and constraints; avoid relying on startup-time schema creation in deployed environments.
- Implement password hashing, session lifecycle, input validation, rate limits on authentication endpoints, and safe error responses.
- Add authorization tests for user-owned profile changes and account boundaries.

**Exit criteria:** a new user can register, verify or recover an account according to the selected policy, authenticate, update a profile, and revoke their session. CI runs migrations and the test suite against a real test database.

### Phase 2: Relationships and community participation

**Deliverables:** follow/block relationships, community creation and membership, and role-checked community administration.

**Work:**

- Enforce uniqueness and valid relationship transitions at the database and application layers.
- Implement public/private community access and membership changes according to the visibility ADR.
- Centralize authorization checks so list, detail, mutation, and search-like endpoints cannot disagree.
- Add audit records for privileged community role changes.

**Exit criteria:** integration tests prove that blocked users and non-members cannot bypass visibility rules through alternate endpoints; users can follow accounts and join/leave eligible communities.

### Phase 3: Posts, comments, and moderation

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

**Deliverables:** a backend window/query service and a UI that makes scope, count, limit, and completion explicit.

**Status:** Implemented in the current application at `/updates`. Each window fixes its time bounds when started; explicit keyset batches use stable `(created_at, id)` ordering, exclude posts created after the window end, and reapply current visibility, block, and moderation rules on every request. Continuation is never automatic.

**Work:**

- Build retrieval from followed accounts and joined communities, applying authorization, blocks, mutes/filters that are in MVP scope, and deduplication before returning items.
- Use stable ordering and cursor-based pagination rather than offset pagination. Bound the maximum requested page size server-side.
- Decide and implement snapshot or live-window semantics. If snapshot semantics are chosen, ensure later inserts do not silently extend an already completed window.
- Return metadata such as applied scope, generated time, returned count, and continuation state. Do not claim an exact remaining count unless it can be computed correctly under permissions and concurrent changes.
- Make continuation explicit: no automatic page fetch, autoplay, or algorithmic insertion. A completed window remains complete until the user requests another one.
- Add controls for source, time range, and maximum items; show empty, filtered-to-zero, loading, error, and completed states.

**Exit criteria:** API and UI tests prove that every request is bounded, ordering is stable across pages, blocked/private content is excluded, limits cannot be bypassed, and no content loads automatically after completion.

### Phase 6: Notifications, controls, and data lifecycle

**Deliverables:** bounded notification inbox, core privacy/content controls, and account data lifecycle workflows.

**Status:** Implemented in the current application. `/settings` centralizes update-window defaults, notification preferences, and account data actions. Replies and report decisions create preference-aware in-app notifications; the inbox and `/api/v1/notifications` use bounded cursor pagination. Account export is a private JSON download.

**Deletion and retention decision:** Account deletion is immediate and requires the current password plus typing `DELETE`. It revokes sessions and reset tokens, removes relationships/memberships/received notifications, replaces email and username with non-identifying values, clears profile text, and disables sign-in. Authored posts, comments, and conversation history remain attributed to “Deleted account”; submitted report reasons are redacted. Owned communities transfer to a moderator or oldest active member; if no other active member exists, the community and its content/audit history are removed. Database backups remain subject to the operator's backup-retention policy; this application does not rewrite existing backups.

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

## 5. Data and API Guidance

- Keep domain modules inside one deployable backend initially; separate API transport, application use cases, domain rules, and persistence access without creating a layer for its own sake.
- Treat PostgreSQL as authoritative. Add Redis or a worker only when a measured latency, fan-out, or scheduling need appears.
- Use foreign keys, uniqueness constraints, check constraints, and migrations to make invalid states difficult to store.
- Index the actual MVP access paths, especially membership/relationship lookups and ordered post retrieval; verify choices with query plans and representative data.
- Use a transactional outbox only when reliable asynchronous side effects are introduced. Do not publish an event before its database change is committed.
- Keep API versioning and pagination contracts consistent. Return authorization-safe errors and avoid leaking whether a private resource exists.
- Store media outside the relational database if uploads are later added; design upload validation, scanning, quotas, and deletion before exposing uploads.

## 6. Quality Gates

Every phase should meet these gates before it is considered complete:

- **Behavior:** acceptance tests cover successful flows, boundary conditions, and failure paths.
- **Authorization:** tests cover owner, member, moderator, blocked user, and anonymous access where relevant.
- **Data integrity:** migrations apply cleanly to an empty database and from the previous release schema.
- **Security:** secrets are not committed; authentication and mutation endpoints have abuse controls; sensitive values are not logged.
- **Usability:** loading, empty, error, and completion states are present; core workflows work by keyboard and on narrow screens.
- **Operations:** health checks, structured logs, backup/restore expectations, and an owner for production alerts are documented.

## 7. Success Measures

Measure whether the product delivers control and reliable communication, not whether it maximizes time spent.

- **Finite retrieval:** percentage of windows that reach an explicit completion state; rate of unexpected automatic continuation should be zero.
- **User control:** use of source/limit/quiet controls, and user-reported confidence in what the window includes.
- **Core utility:** successful conversations, delivered messages, community joins, posts, replies, and return visits chosen by users.
- **Safety:** report response time, repeat-abuse rate, block effectiveness, and authorization incidents.
- **Reliability:** API error rate, latency for key queries, migration failures, and restore-test results.

Do not treat session duration, raw scroll depth, or notifications sent as primary success metrics.

## 8. Improvements to the Source Plan

- **Make chat first-class without merging domains.** Inbox is the signed-in home; conversations remain distinct from community threads. Build persisted messaging before finite updates, while deferring presence, calls, E2EE, and multi-node fan-out.
- **Define “finite” precisely.** A `limit` and `has_more` flag alone do not define a stable window. Specify snapshot/live semantics, cursor behavior, concurrent inserts, permission changes, and when completion is final.
- **Resolve privacy before schema expansion.** Visibility is listed as a requirement but not fully defined for relationships, private communities, blocks, comments, and deleted content. Write policy tables and authorization tests before exposing endpoints.
- **Avoid premature infrastructure.** Redis, Celery/Arq, event buses, search engines, CDN, and microservices add operational cost. Introduce them only for a concrete requirement and retain PostgreSQL as the initial source of truth.
- **Treat security and moderation as release criteria.** Password hashing alone is not a security plan. Add session/recovery policy, authorization testing, abuse controls, reporting, audit, backup, and incident ownership to the build sequence.
- **Be cautious with estimates.** The source plan's week ranges do not state team size, existing code, or deployment assumptions. Estimate after repository and team discovery; track scope and dependencies rather than promising calendar dates now.
- **Separate product measures from growth measures.** Define completion, control, safety, and reliability measures up front; avoid engagement metrics that contradict the product's stated values.
- **Defer AI and end-to-end encryption deliberately.** Both require threat, privacy, and operational design. Keep AI advisory and opt-in; do not imply private messaging is end-to-end encrypted until it is correctly implemented and independently reviewed.

## 9. Immediate Next Actions

1. Start Docker Desktop and verify the PostgreSQL/Mailpit Compose setup plus the migration locally; CI already runs the PostgreSQL migration.
2. Select hosting and environment/secrets management before beta deployment.
3. Agree on privacy/visibility rules and write the authorization ADR before community endpoints.
4. Validate Phase 4's single-process WebSocket behavior on the intended PostgreSQL deployment and choose shared fan-out infrastructure before scaling workers.
5. Proceed to Phase 7 beta readiness: rehearse restore/recovery, add operational monitoring, complete accessibility checks, and prepare an invited pilot.