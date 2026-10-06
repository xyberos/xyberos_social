# Development Plan: Finite Social Communication Platform

The product can be designed as a **hybrid of Telegram + Reddit + Facebook**, but with a fundamentally different architecture:

> **Communication is user-controlled. Communities are user-controlled. Identity is user-controlled. Content is finite by default.**

The central design principle should be:

**No infinite feed. No algorithmic addiction loop. No uncontrolled content stream.**

Instead, users explicitly decide **who, what, where, and how much** they consume.

---

# 1. Product Architecture

Think of the system as six major domains:

```text
                         ┌─────────────────────┐
                         │      CLIENTS        │
                         │ Web / Mobile /      │
                         │ Desktop             │
                         └──────────┬──────────┘
                                    │
                                    ▼
                    ┌───────────────────────────┐
                    │       API / Gateway       │
                    │ REST / WebSocket / Auth   │
                    └─────────────┬─────────────┘
                                  │
          ┌───────────────────────┼────────────────────────┐
          ▼                       ▼                        ▼
 ┌────────────────┐      ┌────────────────┐      ┌────────────────┐
 │ Communication  │      │ Social Graph   │      │ Communities    │
 │                │      │                │      │                │
 │ DM             │      │ Profiles       │      │ Communities    │
 │ Groups         │      │ Friends        │      │ Posts          │
 │ Channels       │      │ Following      │      │ Comments       │
 │ Calls/events   │      │ Blocking       │      │ Moderation     │
 └───────┬────────┘      └───────┬────────┘      └───────┬────────┘
         │                       │                        │
         └───────────────────────┼────────────────────────┘
                                 ▼
                     ┌──────────────────────┐
                     │   CONTENT CONTROL    │
                     │                      │
                     │ Finite feeds         │
                     │ User filters         │
                     │ Muting               │
                     │ Limits               │
                     │ Expiration           │
                     │ Quiet modes          │
                     └──────────┬───────────┘
                                │
                                ▼
                    ┌────────────────────────┐
                    │       DATA LAYER       │
                    │                        │
                    │ PostgreSQL             │
                    │ Redis                  │
                    │ Object Storage         │
                    │ Search Index            │
                    │ Event Bus              │
                    └────────────────────────┘
```

---

# 2. Core Product Concepts

Instead of copying three platforms literally, define three first-class concepts.

## A. Communication

Telegram-like:

* 1:1 messaging
* group chat
* private groups
* public groups
* channels
* replies
* reactions
* media
* files
* voice messages
* disappearing messages
* message editing
* message deletion
* pinned messages
* mentions
* threads

---

## B. Communities

Reddit-like:

```text
Community
   │
   ├── Posts
   │    ├── Text
   │    ├── Image
   │    ├── Link
   │    ├── Video
   │    └── Poll
   │
   ├── Comments
   │
   ├── Moderators
   │
   ├── Rules
   │
   └── Membership
```

Example:

```text
r/programming
r/gaming
r/philippines
r/startups
r/xyberos
```

But avoid the traditional endless homepage.

---

# 3. Identity / Social Graph

Facebook-like identity should be a separate subsystem.

```text
User
 │
 ├── Profile
 │
 ├── Friends
 │
 ├── Following
 │
 ├── Followers
 │
 ├── Communities
 │
 ├── Groups
 │
 └── Privacy settings
```

Relationships should have explicit types.

```text
FRIEND
FOLLOW
BLOCK
MUTE
RESTRICT
COMMUNITY_MEMBER
MODERATOR
ADMIN
```

This makes the social graph much easier to reason about.

---

# 4. The Most Important Concept: Finite Content

This should be a **core architectural constraint**, not merely a UI feature.

Traditional social media:

```text
Open app
    ↓
Feed
    ↓
Scroll
    ↓
More content
    ↓
Scroll
    ↓
More content
    ↓
Infinite
```

Your architecture:

```text
Open app
    ↓
User chooses context
    ↓
System creates finite content set
    ↓
User consumes it
    ↓
SET COMPLETE
```

For example:

```text
Home
├── Friends
│   └── 18 new posts
│
├── Communities
│   └── 31 new posts
│
├── Messages
│   └── 7 conversations
│
└── Following
    └── 12 updates
```

The user can see:

> **18 posts available**

rather than:

> Here is an algorithmically generated infinite stream.

---

# 5. Finite Feed Engine

This should be its own service/module.

```text
FeedRequest
    ↓
Determine sources
    ↓
Apply user filters
    ↓
Apply privacy rules
    ↓
Apply moderation
    ↓
Apply time window
    ↓
Generate finite result
    ↓
Return N items
```

Example:

```http
GET /feed?limit=20
```

Response:

```json
{
  "items": [...],
  "count": 17,
  "has_more": false,
  "generated_at": "...",
  "scope": "friends"
}
```

The important part:

```json
"has_more": false
```

should be normal.

---

# 6. Content Budgets

Give users explicit consumption controls.

Example:

```text
Daily content budget

Friends       30 posts
Communities   40 posts
Following     20 posts
Recommended   10 posts
```

Or:

```text
Maximum community updates:
25/day
```

The system then stops presenting additional content.

This transforms the feed from an engagement-maximization system into a **personal information queue**.

---

# 7. User-Controlled Feed Modes

Provide explicit modes.

### Inbox

Only direct communication.

```text
DM
Mentions
Replies
Group messages
```

### Friends

Only people the user has accepted as friends.

### Following

Only explicitly followed accounts.

### Communities

Only selected communities.

### Digest

A finite summary.

```text
Today's digest

Friends       12
Community A    8
Community B    6
Following      9

Total: 35
```

### Quiet

```text
No social content.

Messages from priority contacts only.
```

---

# 8. No Forced Recommendation Engine

Recommendations can exist, but they should be opt-in.

Instead of:

```text
Algorithm
   ↓
You may like this
   ↓
More recommendations
   ↓
More recommendations
```

use:

```text
Explore
   ↓
User explicitly requests discovery
   ↓
Finite recommendation set
   ↓
Done
```

For example:

```text
Explore Communities

20 communities found.

[Load another 20]
```

The user explicitly controls continuation.

---

# 9. Architecture: Modular Monolith First

Do **not** start with microservices.

For the first version:

```text
backend/
├── app/
│   ├── main.py
│   │
│   ├── api/
│   │   ├── auth.py
│   │   ├── users.py
│   │   ├── messages.py
│   │   ├── groups.py
│   │   ├── communities.py
│   │   ├── posts.py
│   │   ├── comments.py
│   │   ├── feed.py
│   │   ├── notifications.py
│   │   └── media.py
│   │
│   ├── domain/
│   │   ├── users/
│   │   ├── social/
│   │   ├── messaging/
│   │   ├── communities/
│   │   ├── content/
│   │   ├── feed/
│   │   └── moderation/
│   │
│   ├── services/
│   │   ├── auth_service.py
│   │   ├── messaging_service.py
│   │   ├── social_service.py
│   │   ├── community_service.py
│   │   ├── feed_service.py
│   │   └── notification_service.py
│   │
│   ├── models/
│   ├── schemas/
│   ├── repositories/
│   ├── events/
│   ├── workers/
│   └── core/
│
├── migrations/
└── tests/
```

Recommended initial stack:

```text
Python
FastAPI
SQLAlchemy
PostgreSQL
Redis
WebSocket
Object Storage
Celery / Arq
```

---

# 10. Database Design

PostgreSQL should be the source of truth.

## Users

```text
users
-----
id
username
email
password_hash
display_name
bio
avatar_id
created_at
status
```

## Profiles

```text
profiles
--------
user_id
bio
website
location
privacy_mode
```

## Relationships

```text
relationships
-------------
id
source_user_id
target_user_id
type
created_at
```

Types:

```text
FRIEND
FOLLOW
BLOCK
MUTE
RESTRICT
```

---

# 11. Messaging Tables

```text
conversations
-------------
id
type
created_by
created_at
```

```text
conversation_members
---------------------
conversation_id
user_id
role
joined_at
```

```text
messages
--------
id
conversation_id
sender_id
content
message_type
reply_to_id
created_at
edited_at
deleted_at
```

For scale, messages can eventually be partitioned by:

```text
conversation_id
created_at
```

---

# 12. Communities

```text
communities
-----------
id
name
slug
description
visibility
created_by
created_at
```

```text
community_members
------------------
community_id
user_id
role
joined_at
```

```text
community_rules
---------------
id
community_id
title
description
```

---

# 13. Posts

```text
posts
-----
id
author_id
community_id
title
body
post_type
visibility
created_at
edited_at
deleted_at
```

Comments:

```text
comments
--------
id
post_id
author_id
parent_id
body
created_at
deleted_at
```

This supports Reddit-style threaded discussions.

---

# 14. Reactions

Avoid creating dozens of reaction columns.

Use:

```text
reactions
---------
id
user_id
target_type
target_id
reaction
created_at
```

Example:

```text
LIKE
LOVE
LAUGH
SAD
ANGRY
```

---

# 15. Content Lifecycle

Every piece of content should have a lifecycle.

```text
CREATED
   ↓
VISIBLE
   ↓
EDITED
   ↓
ARCHIVED
   ↓
DELETED
```

Optional:

```text
EXPIRES
```

This enables finite communities and temporary content.

---

# 16. Expiring Content

Users should be able to define:

```text
Delete after:

1 hour
24 hours
7 days
30 days
Never
```

Community administrators could also configure:

```text
Posts older than 90 days → archive
```

This keeps communities manageable over time.

---

# 17. Notification Architecture

Notifications should **not** become another infinite feed.

Use finite notification batches.

```text
Notifications

12 new replies
5 mentions
3 friend requests
2 community updates
```

And provide:

```text
Mark all as read
Clear
Mute category
Digest
```

---

# 18. Messaging Architecture

Use WebSockets for real-time communication.

```text
Client
  │
  │ WebSocket
  ▼
FastAPI
  │
  ├── authenticate
  ├── validate
  ├── persist message
  └── publish event
          │
          ▼
        Redis
          │
          ▼
   Connected clients
```

PostgreSQL remains authoritative.

Redis is for:

* presence
* pub/sub
* temporary state
* rate limiting
* caching

---

# 19. Presence

Telegram-style:

```text
online
last_seen
typing
recording
```

But presence should be configurable:

```text
Everyone
Friends
Nobody
Custom
```

---

# 20. Privacy Model

Privacy should be designed into the data model rather than added later.

Every resource should answer:

```text
Who can see this?
Who can interact with this?
Who can discover this?
Who can message this user?
```

Example:

```text
Post visibility

PUBLIC
FRIENDS
COMMUNITY
FOLLOWERS
PRIVATE
CUSTOM
```

---

# 21. User-Control Center

Create one centralized control system.

```text
Control Center
│
├── Who can contact me
├── Who can follow me
├── Who can see my profile
├── Who can see my posts
├── Community discovery
├── Recommendations
├── Notifications
├── Content limits
├── Quiet hours
├── Muted words
├── Muted communities
├── Blocked users
└── Data retention
```

This becomes one of the defining product features.

---

# 22. Finite Content as a Platform Primitive

I would actually create a dedicated abstraction:

```python
class ContentWindow:
    source: str
    limit: int
    offset: int
    created_after: datetime | None
    created_before: datetime | None
    exhausted: bool
```

Every feed-like operation produces a `ContentWindow`.

Example:

```python
window = ContentWindow(
    source="friends",
    limit=30
)
```

Result:

```json
{
  "items": [...],
  "remaining": 0,
  "exhausted": true
}
```

This makes finite content an architectural property rather than a frontend trick.

---

# 23. Feed Algorithm

Do not optimize for:

```text
time_on_platform
scroll depth
engagement
ad impressions
```

Instead optimize for:

```text
relevance
user-selected sources
freshness
relationship priority
community relevance
information density
finite completion
```

A simple first implementation:

```python
sources = [
    friends,
    followed_users,
    joined_communities
]

items = collect(sources)

items = apply_privacy(items)
items = apply_blocks(items)
items = apply_mutes(items)
items = apply_user_filters(items)
items = deduplicate(items)

items = rank_by(
    relationship_strength,
    freshness,
    explicit_user_interest
)

return items[:user_limit]
```

---

# 24. Avoid Engagement Loops

Don't implement:

```text
autoplay
infinite scroll
automatic next video
continuous recommendation
```

Instead:

```text
20 posts loaded

──────────────

You've reached the end.

[Done]
[Load another 20]
```

This small UX decision has major architectural consequences.

---

# 25. Community Moderation

Each community gets:

```text
Owner
Admin
Moderator
Member
```

Moderation tools:

```text
remove post
remove comment
lock thread
mute user
ban user
approve post
mark spam
community rules
mod log
```

Important:

**Every moderation action should be auditable.**

```text
moderation_events
-----------------
id
moderator_id
community_id
target_type
target_id
action
reason
created_at
```

---

# 26. Anti-Spam System

Before building sophisticated AI moderation, implement deterministic controls:

```text
rate limiting
account age limits
posting cooldown
message limits
link limits
duplicate detection
report thresholds
block lists
```

Then optionally add ML/AI classification.

---

# 27. AI Should Be an Assistant, Not the Feed Controller

AI can provide:

* spam detection
* moderation assistance
* summarization
* translation
* search
* duplicate detection
* content warnings
* community recommendations

But avoid:

```text
AI decides what everyone should see
```

Instead:

```text
User chooses scope
      ↓
System retrieves content
      ↓
AI optionally helps organize it
      ↓
User sees finite result
```

---

# 28. Search

Search should cover:

```text
Users
Posts
Comments
Communities
Messages
Files
```

Start with PostgreSQL full-text search.

Later:

```text
OpenSearch / Elasticsearch
```

For semantic search:

```text
Post
 ↓
Embedding
 ↓
Vector index
```

This could eventually integrate well with your Xyberos architecture.

---

# 29. Recommended Xyberos Integration

Given your existing Xyberos direction, I would **not make Xyberos the core social backend**.

Instead:

```text
Social Platform
       │
       ├── Authentication
       ├── Messaging
       ├── Social graph
       ├── Communities
       ├── Content
       └── Feed
              │
              ▼
          Xyberos
              │
              ├── intent
              ├── memory
              ├── knowledge
              ├── moderation
              ├── search
              └── AI tools
```

Xyberos becomes the **intelligence layer**.

The social platform remains deterministic.

This separation is important.

---

# 30. Xyberos Example

User:

> "Show me what I missed in the programming communities."

Flow:

```text
User
 ↓
Xyberos intent
 ↓
community_digest
 ↓
Feed engine
 ↓
retrieve finite content
 ↓
Xyberos summarizes
 ↓
20-item digest
 ↓
DONE
```

The AI does not decide to keep the user scrolling.

---

# 31. API Structure

Example:

```text
/api/v1/auth
/api/v1/users
/api/v1/profiles
/api/v1/relationships

/api/v1/conversations
/api/v1/messages
/api/v1/groups
/api/v1/channels

/api/v1/communities
/api/v1/communities/{id}/members
/api/v1/communities/{id}/posts

/api/v1/posts
/api/v1/comments
/api/v1/reactions

/api/v1/feed
/api/v1/digest

/api/v1/notifications

/api/v1/search

/api/v1/settings
/api/v1/privacy
/api/v1/controls
```

WebSockets:

```text
/ws/messages
/ws/presence
/ws/notifications
/ws/community
```

---

# 32. Client Architecture

Build three clients eventually:

```text
                   Backend
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
        Web        Mobile       Desktop
```

For the first release, build a responsive server-rendered web app with **FastAPI, Jinja2, and HTMX**. Use Alpine.js only for small interactions that genuinely need client-side state. Keep core authorization and product rules on the backend.

Add PWA installation only after defining which static assets may be cached; authenticated pages and personal content must not enter a shared service-worker cache. Defer the Flutter client until the web MVP validates the workflows and users demonstrate a need for native mobile features. A desktop wrapper can follow if there is a clear use case.

---

# 33. Main UI

Instead of a Facebook-style feed-first interface:

```text
┌─────────────────────────────────────┐
│ Search                              │
├──────────┬──────────────────────────┤
│ Messages │                          │
│ Friends  │      Content Window      │
│ Groups   │                          │
│ Community│      18 items            │
│          │                          │
│          │      item                 │
│          │      item                 │
│          │      item                 │
│          │                          │
│          │ ──────────────────────── │
│          │ End of updates           │
└──────────┴──────────────────────────┘
```

The UI explicitly communicates:

> **You are done.**

---

# 34. Home Screen

A better home screen could be:

```text
Good afternoon

┌──────────────────────────┐
│ Messages                 │
│ 7 conversations          │
└──────────────────────────┘

┌──────────────────────────┐
│ Friends                  │
│ 12 updates               │
└──────────────────────────┘

┌──────────────────────────┐
│ Communities              │
│ 24 updates               │
└──────────────────────────┘

┌──────────────────────────┐
│ Following                │
│ 8 updates                │
└──────────────────────────┘

[Explore]
```

No feed automatically starts consuming attention.

---

# 35. Architecture Layers

I would divide the backend into:

```text
┌────────────────────────────┐
│         API Layer          │
├────────────────────────────┤
│      Application Layer     │
├────────────────────────────┤
│        Domain Layer        │
├────────────────────────────┤
│      Infrastructure       │
├────────────────────────────┤
│       Persistence          │
└────────────────────────────┘
```

Domain modules:

```text
identity
social
messaging
community
content
feed
moderation
notification
search
control
```

---

# 36. Event-Driven Internals

Use domain events.

```text
MessageCreated
PostCreated
CommentCreated
UserFollowed
FriendAccepted
CommunityJoined
PostReported
UserBlocked
```

Example:

```text
PostCreated
    │
    ├── notification
    ├── feed update
    ├── search indexing
    ├── moderation
    └── analytics
```

This keeps components loosely coupled.

---

# 37. Caching

Redis:

```text
user sessions
presence
unread counts
feed windows
rate limits
hot community metadata
```

Don't cache everything.

PostgreSQL remains the source of truth.

---

# 38. Media Architecture

Never store large media directly in PostgreSQL.

```text
Client
 ↓
API
 ↓
Object Storage
 ↓
CDN
```

Examples:

```text
S3
Cloudflare R2
MinIO
```

Database stores:

```text
media_id
object_key
mime_type
size
owner_id
created_at
```

---

# 39. Security

Required from day one:

```text
Argon2id password hashing
JWT/session authentication
refresh token rotation
CSRF protection
rate limiting
input validation
SQL injection protection
XSS protection
content security policy
file validation
virus scanning
audit logs
account recovery
2FA
```

For messaging, consider end-to-end encryption as a later architecture milestone rather than attempting it casually.

---

# 40. Data Ownership

Give users controls over:

```text
Export my data
Download messages
Download posts
Download media
Delete account
Delete content
Clear activity
```

Account deletion should be a real workflow:

```text
request deletion
     ↓
grace period
     ↓
delete personal data
     ↓
anonymize necessary community records
     ↓
remove media
```

---

# 41. Anti-Addiction Architecture

This is where the product can become substantially different.

Avoid making these backend primitives:

```text
infinite_feed()
autoplay_next()
recommend_forever()
engagement_optimizer()
```

Instead create:

```text
create_content_window()
get_updates()
get_digest()
mark_window_complete()
set_content_limit()
set_quiet_mode()
```

The API itself reflects the philosophy.

---

# 42. User-Control Algorithm

The hierarchy should be:

```text
USER CONTROL
     ↓
PRIVACY
     ↓
EXPLICIT RELATIONSHIPS
     ↓
EXPLICIT COMMUNITY MEMBERSHIP
     ↓
USER FILTERS
     ↓
FINITE RETRIEVAL
     ↓
OPTIONAL AI ORGANIZATION
```

Not:

```text
ENGAGEMENT
     ↓
ALGORITHM
     ↓
RECOMMENDATION
     ↓
USER
```

---

# 43. Development Phases

## Phase 0 — Architecture

**1–2 weeks**

Define:

* domain model
* database schema
* authentication
* permissions
* API contracts
* event model
* content-window model
* privacy model

Deliverable:

```text
Architecture specification
Database ERD
API specification
```

---

## Phase 1 — Identity

**2–3 weeks**

Build:

* registration
* login
* sessions
* profiles
* usernames
* avatars
* privacy
* blocking
* following
* friends

---

## Phase 2 — Messaging

**3–5 weeks**

Build:

* DM
* groups
* WebSockets
* message persistence
* read states
* typing indicators
* reactions
* replies
* attachments

---

## Phase 3 — Communities

**3–4 weeks**

Build:

* create community
* join/leave
* posts
* comments
* voting/reactions
* moderation
* rules
* moderators

---

## Phase 4 — Finite Content Engine

**2–4 weeks**

This is the most important custom component.

Build:

```text
ContentWindow
FeedService
DigestService
UserLimits
Filters
Mute system
Quiet mode
Completion state
```

---

## Phase 5 — Notifications

**1–2 weeks**

Build:

* mentions
* replies
* messages
* friend requests
* community activity
* digest notifications

All notifications should be finite and controllable.

---

## Phase 6 — Search

**2–3 weeks**

Build:

```text
users
communities
posts
comments
messages
```

Then add semantic search later.

---

## Phase 7 — AI/Xyberos

**2–4 weeks**

Add:

```text
intent detection
content summarization
community digest
moderation assistant
semantic search
translation
natural-language controls
```

Example:

> "Only show me programming posts from the last three days, maximum 15."

Xyberos converts this into:

```json
{
  "source": "programming",
  "time_range": "3d",
  "limit": 15
}
```

The deterministic feed engine executes it.

---

# 44. Phase 8 — Privacy / Security Hardening

Before public launch:

```text
penetration testing
rate-limit testing
permission testing
privacy testing
file-upload testing
account recovery testing
abuse testing
load testing
backup testing
disaster recovery
```

---

# 45. Phase 9 — Beta

Start with:

```text
100–500 users
```

Measure:

```text
messages/day
communities created
posts/day
moderation events
content-window completion
notification volume
user-defined limits
retention
reports
blocked accounts
```

Do **not** optimize primarily around:

```text
hours spent
scroll depth
sessions per day
```

Those metrics would push the architecture toward the same behavior you're trying to replace.

---

# 46. Scaling Strategy

Start:

```text
1 application
1 PostgreSQL
1 Redis
1 object storage
```

Then:

```text
                Load Balancer
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       API #1       API #2      API #3
          │           │           │
          └───────────┼───────────┘
                      │
                 PostgreSQL
                      │
                    Redis
```

Eventually separate:

```text
Messaging
Feed
Search
Media
Notifications
AI
```

Only when actual scale requires it.

---

# 47. Recommended Repository

A production-oriented monorepo:

```text
social-platform/
│
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── models.py
│   │   ├── database.py
│   │   ├── config.py
│   │   ├── security.py
│   │   ├── templates/
│   │   └── static/
│   ├── alembic/
│   ├── tests/
│   └── requirements.txt
│
├── mobile/                 # Flutter client, deferred until validated
│
├── ai/
│   └── xyberos/
│
├── infrastructure/
│   ├── docker/
│   ├── nginx/
│   ├── postgres/
│   └── redis/
│
├── docs/
│   ├── architecture/
│   ├── api/
│   ├── database/
│   └── security/
│
└── docker-compose.yml
```

---

# 48. MVP Scope

Do **not** attempt all features initially.

The first usable product should contain:

### Identity

* account
* profile
* friends
* following
* block

### Communication

* DM
* groups
* WebSocket
* media

### Communities

* create/join
* posts
* comments
* moderation

### Content control

* finite feeds
* mute
* block
* filters
* content limits
* quiet mode

### Notifications

* messages
* mentions
* replies
* friend requests

That's enough to prove the concept.

---

# 49. What Makes the Architecture Different

The fundamental difference is not:

> "Telegram + Reddit + Facebook."

It is:

```text
                USER
                 │
                 ▼
        ┌─────────────────┐
        │ Explicit Intent │
        └────────┬────────┘
                 ▼
        ┌─────────────────┐
        │ User Controls   │
        │                 │
        │ Who             │
        │ What            │
        │ Where           │
        │ How much        │
        │ How often       │
        └────────┬────────┘
                 ▼
        ┌─────────────────┐
        │ Finite Content  │
        │ Window          │
        └────────┬────────┘
                 ▼
        ┌─────────────────┐
        │ Communication / │
        │ Community       │
        └────────┬────────┘
                 ▼
              DONE
```

That should be reflected **all the way from the database and API to the UI**.

## Final architecture

```text
                    ┌───────────────────┐
                    │      CLIENT       │
                    │ Web / Mobile / PC│
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │    API GATEWAY    │
                    └─────────┬─────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
    ┌───────────┐       ┌───────────┐       ┌───────────┐
    │ Identity  │       │ Messaging │       │Community  │
    └─────┬─────┘       └─────┬─────┘       └─────┬─────┘
          │                   │                   │
          └───────────────────┼───────────────────┘
                              ▼
                    ┌───────────────────┐
                    │  CONTENT ENGINE   │
                    │                   │
                    │ filters           │
                    │ privacy           │
                    │ limits            │
                    │ finite windows    │
                    │ quiet mode        │
                    └─────────┬─────────┘
                              │
                    ┌─────────▼─────────┐
                    │   DATA PLATFORM   │
                    │ PostgreSQL        │
                    │ Redis             │
                    │ Object Storage    │
                    └─────────┬─────────┘
                              │
                       optional AI
                              │
                    ┌─────────▼─────────┐
                    │     XYBEROS       │
                    │                   │
                    │ Intent             │
                    │ Search             │
                    │ Summarization      │
                    │ Moderation         │
                    │ Natural-language   │
                    │ controls           │
                    └───────────────────┘
```

**The key architectural decision:** make **`ContentWindow` / finite retrieval** a first-class backend primitive. That prevents the product from accidentally becoming another infinite-feed platform even as features and recommendation systems are added later.
