# Personal notifications for AKB

- Status: accepted and implemented in the working branch; not deployed
- Stage: implemented and integration verified
- Date: 2026-09-08
- Baseline inspected: `0655632` (PR #506 head); remote main observed at its merge `3dea8368`

## Implementation status

The working branch implements a PostgreSQL personal inbox, effective access
notices, explicit document watches, transactional delivery work, a worker,
authenticated APIs, and the header/full-inbox/settings UI. Legacy and Native
document mutations enqueue work; Native reads and watches use current Native
resource identity. Implementation was authorized after the initial proposal and
reviewed across the backend and frontend. This decision does not authorize deployment.

See the [user and operator guide](../../../guides/personal-notifications.md) for
the implemented behavior and configuration. The implementation uses migration
099, fixed five-minute event-time buckets for ordinary document updates, and
90-day retention by default. Before/after archive transitions and terminal
events remain separate. Native document labels currently use the current
filename, not the frontmatter title. Vault filtering, inbox dismissal, actor
display, and general notification preferences are not implemented.

Focused producer and real-PostgreSQL checks cover rollback, mutation replay,
Native lifecycle events, current resource resolution, and watch preservation
across move. These checks are not a claim that every acceptance item below has
passed. Real HTTP checks additionally exercise two human logins, the background
worker, grant/update/delete/revoke delivery, subscription opt-out, snapshot reads,
and private-target redaction. Browser fixtures cover desktop/mobile light/dark
layouts, Watch, read acknowledgement, and reader return focus.

The persistent regression suites live in `backend/tests/test_notifications_*`,
`backend/tests/test_notification_*`, and `frontend/e2e/notifications-contract.spec.ts`.
The HTTP suite is opt-in and rejects non-loopback deployment targets.

## Product scope

Add a small, persistent **personal notification inbox**, not a second Activity
feed and not an infrastructure alarm console. Start with effective access changes
and explicit document subscriptions. Retain existing indexing progress indicators.
Defer email, browser push, scheduled reminders, comments/mentions, and generalized
rule builders. Implementation has proceeded in the working branch; deployment
remains outside this work.

## Foundations inspected before implementation

| Surface | Evidence | Implication |
| --- | --- | --- |
| Domain events | `backend/app/repositories/events_repo.py:20` | Transactional insertion API can be reused; inspect each producer's actual transaction. |
| Vault change stream | `backend/app/api/routes/events.py:20` | Reader-authorized, vault-scoped SSE; not a recipient-filtered inbox. |
| Stream authorization | `backend/app/services/event_tail_service.py:320` | Rechecks access; revoked users cannot rely on this stream to receive revocation notices. |
| Optional Redis transport | `backend/app/services/lifecycle.py:410` | Notifications should not require Redis or change its existing delivery cursor. |
| Event cleanup | `backend/app/services/events_publisher.py:267` | Published rows are swept after seven days; cleanup currently depends on Redis publisher running. |
| Access changes | `backend/app/services/access_service.py:571`, `:681`, `:1151` | Grant, revoke and ownership changes provide initial integration points. |
| Publications | `backend/app/services/publication_service.py:483`, `:627` | Create/delete do not currently emit notification-ready domain events. |
| Global header | `frontend/src/components/layout.tsx:221`, `:250` | Search, indexing status and profile provide the shared bell integration point. |
| Indexing state | `frontend/src/hooks/use-accessible-indexing-health.ts` | Reader-scoped snapshots already poll at 15/60 seconds; do not turn every chunk into an inbox item. |
| Authentication | `frontend/src/lib/api.ts:255`, `frontend/src/components/layout.tsx` | Reuse authenticated requests, CSRF handling and identity-change cache invalidation. |
| Document preview | `frontend/src/components/document-preview-dialog.tsx` | Reuse reader presentation, but generalize its Search-specific return/focus contract before inbox use. |

At the inspected baseline, no personal inbox, read-state/preferences schema, or
product email delivery layer existed. The inbox and read state are now implemented;
email and general preferences remain deferred. MCP protocol notifications are not
user-facing inbox notifications. Audit logging is not a reliable inbox source.

## Separate three meanings

1. **Immediate feedback:** saved/failed UI action; inline feedback or short toast.
2. **Personal notification:** something relevant happened while the user was away;
   persisted and individually readable across devices.
3. **Operational alarm:** a service is unhealthy; an operator must respond through
   independent monitoring. An unavailable AKB cannot reliably announce its own outage.

Activity remains the historical record. Indexing badges remain current progress.
The inbox contains selected, actionable changes, not every event or commit.

## First-release scope

| Event | Recipient | Default and behavior |
| --- | --- | --- |
| Access granted/effective role changed | Affected user | On; suppress replays and changes to a grant basis that do not change the effective role. |
| Direct membership removed | Affected user | On; distinguish membership removal from total access loss when public access or another basis survives. |
| Ownership transferred | Previous/new owner | On; stable user IDs, no misleading notification to every reader. |
| Subscribed document updated/moved/archived/restored/deleted | Explicit subscribers | Opt-in; own ordinary edits excluded; preserve subscription across rename/move by resource UUID. |
| Repeated agent edits | Same subscribers | Coalesce ordinary updates for the same document in fixed five-minute event-time buckets; keep terminal/destructive transitions separate. |

Document subscription is independent of permissions. Reading, creating, starring,
or receiving public access must not silently subscribe someone. Start with
document-level Watch/Unwatch; defer Vault/Collection inheritance to avoid an
unbounded subscription matrix. Collection paths and document titles are not IDs.

The implementation groups ordinary updates into fixed five-minute event-time
buckets and retains inbox entries for 90 days by default. Retention is configurable
and the interface displays its current value. Historical source events are not
replayed when enabling the feature.

### Later, only after source events are reliable

- Import/sync terminal failure and recovery: requester and responsible owner,
  once per operation/incident, not once per retry or document.
- Long-running requested job completion: initiator; exclude routine instant saves.
- PAT expiry: token owner; requires deduplicated scheduling and safe metadata.
- Publication expiry: creator/owner, not every page view; requires lifecycle events.
- Email digest/Slack/webhook channels: outbound delivery policy, secrets, retries,
  endpoint validation and opt-out need their own design.
- Mentions/comments and reminders require the corresponding product workflows;
  do not introduce them incidentally just to populate an inbox.

## UI and interaction

- One bell between global Search and profile, visible across authenticated routes.
  Reserve its width regardless of unread count; keep Search/indexing alignment.
- Desktop: approximately 480px header-anchored panel, constrained to viewport
  height, with one list scroll area. Mobile: full-width accessible sheet.
- Header: title and overflow actions, followed by text-only All / Documents /
  Access underline tabs alongside Unread only. The overflow holds Notification
  settings and the explicitly global Mark all notifications read action. Both surfaces
  paginate on the server by category and read state. View all notifications
  preserves those filters in the full-route URL. Vault filtering remains deferred.
- Compact rows grouped by Today / Yesterday / Earlier in local time: event glyph,
  title, short event label and non-repeated Vault context, timestamp, explicit
  unread indicator and row actions. Times and read actions sit side by side;
  omit icon boxes and repeated row dividers. Preserve server order and do not
  invent actor details absent from the response.
  Avoid raw URIs, rainbow cards, permanent warning-red badges or new gradients.
- Opening the panel does not mark everything read. Explicit mark-read/unread and
  opening a specific notification update only that item. Mark all read applies to
  the server-provided snapshot boundary, not notifications arriving afterward.
- Bell count is server-authorized unread count, capped visually at 99+; accessible
  label conveys unread state. Missing/error count is unknown, never fake zero.
- No automatic modal, sound or browser permission prompt. Avoid transient toasts
  for background events while a user is writing. Critical action errors remain
  inline; inbox items remain until retention or user dismissal policy applies.
- Reuse Radix focus management, Escape/backdrop dismissal and focus return. Opening
  a document closes the inbox overlay before the reader opens; do not stack two
  independent focus traps. Preserve full-inbox filters/scroll on return.
- Existing route-backed reader must be generalized before using it for inbox
  navigation. Deleted/inaccessible resources show a safe state without a dead CTA.
- Account Settings owns personal subscription/preferences controls. Vault Settings
  must not let an owner silently change everybody's personal preferences.

The UI/UX skill returned a relevant toast rule (transient non-critical feedback),
not a full notification delivery design. The persistent-inbox distinction is a
project-specific recommendation, informed by the references below. Existing AKB
design tokens, 14px body, neutral hairlines and semantic icon/text remain binding.

## Delivery architecture

```text
Domain change + durable event/notification work (one transaction)
  -> dedicated notification processor (existing worker process)
  -> recipient policy + deduplication + current access check
  -> PostgreSQL personal inbox
  -> authenticated inbox/count APIs
  -> bell, panel, full inbox, existing resource reader
```

Reuse the PostgreSQL event insertion boundary, not the raw Vault SSE as the durable
consumer. Existing BIGSERIAL `id > cursor` reads need caution: transactions can
commit out of ID order. A simple maximum processed ID may skip a late commit.

Use independent durable per-event notification work/ack state, created in the
same transaction as eligible source events. Claim pending work with a lease and
`SKIP LOCKED`; atomically persist recipient items and processing completion.
Retry with bounded backoff and a visible terminal failure state. Unique delivery
keys make replay safe. Do not reuse Redis `published_at` as notification progress.

Source-event deletion must wait for notification processing acknowledgment, or
notification work must carry its own bounded safe payload independent of source
retention. Existing seven-day sweeper must be coordinated explicitly; do not add
a foreign key/cascade that silently deletes unprocessed work or personal history.
Define cleanup for both Redis-enabled and Redis-disabled deployments.

Initial delivery uses a per-user unread-count poll every 45 seconds while
foregrounded and fetches the inbox on open/focus. The open inbox also refreshes
every 45 seconds. Requests back off on errors, pause in hidden tabs, and use
identity-scoped query keys. This avoids one SSE/PG connection per
Vault per browser. Add a user-scoped invalidation stream only if measured latency
requirements justify it; the database inbox remains authoritative either way.

## Implemented storage and API contracts

Migration 099 creates these tables:

- `notification_work`: unique source/delivery key, event type, bounded payload,
  recipient UUID snapshot, claim lease, retry state, processed time. Only eligible
  event types are queued.
- `user_notifications`: recipient user UUID, type, resource UUID/Vault UUID,
  grouping key, first/last time, current version and read version. Display labels
  and permitted targets are resolved at read time.
- `notification_subscriptions`: user UUID + resource UUID + preference; unique pair.

It also creates `notification_inboxes` for serialized recipient versions and
`notification_deliveries` for per-source/per-recipient deduplication. Subscription
presence is the watch preference; there is no general preference schema.

Use distinct source event delivery keys even when several events update one grouped
row. Track a per-recipient inbox version: an update to an already-read group makes
it unread again, and a mark-read request acknowledges only the version observed by
the client. This also prevents Mark all read from swallowing concurrent arrivals.

REST surfaces under `/api/v1`, all bound to the authenticated human session:

- `GET /notifications?state=unread&cursor=...&limit=20`
- `GET /notifications/unread-count`
- `PATCH /notifications/{id}` for read/unread with observed version
- `POST /notifications/mark-read` with a server-issued snapshot boundary
- `GET /notification-subscriptions` to list current accessible watches
- `GET/PUT/DELETE /notification-subscriptions?uri=<document-uri>` to inspect,
  create, or remove a watch; the server resolves the stable resource UUID

Cursor pagination uses a stable tie-breaker. Enforce page size limits, indexes on
recipient and ordering/unread fields, and bounded payloads. Current recipient
identity is never supplied by the browser. Separate preferences can be added
without making a generic rule engine part of the first release.

## Privacy, authorization and compatibility

- Resolve recipients to stable user UUIDs; existing event actors mix usernames
  and UUID strings. Normalize actor/target identity before suppressing self-events.
- Generate and serve content notifications only with authorized access. Recheck
  on inbox list/count and target open; never rely solely on delivery-time access.
- Public readability is not an opt-in subscription and is not grounds for mass
  fan-out. A PAT with narrow Vault scope must not become a way to read the user's
  cross-Vault inbox; explicitly constrain it or restrict inbox to supported human
  session credentials. Preserve existing SSO CSRF and session-isolation rules.
- Revocation is a narrow personal-account notice: minimum safe wording, no document
  title/body/path exposure and no unusable Members link. Do not broadly bypass ACL
  for old content notifications. Deleted targets follow the same safe-redaction rule.
- Do not put source bodies, token values, raw exceptions or private URLs in payloads.
  Use structured target IDs and resolve allowed routes rather than arbitrary URLs.
- Legacy and Native writes, REST/MCP/import paths must converge on one server-side
  integration point; frontend-only event generation would miss agent activity.
- Expose support explicitly. Old servers or disabled deployments omit the feature
  or show unavailable; a 404/503 is not “no notifications.” No mandatory Redis,
  SMTP, new service, or production deployment is required by the first release.

## Implementation sequence and acceptance checks

1. Define eligible event schemas, stable recipient identities, no-op rules and
   transactional notification work. Verify legacy/Native source parity first.
2. Implement inbox, processing/retry/retention and access checks, including removal
   of access and public/multi-basis membership semantics.
3. Add header panel/full inbox and effective access notifications as first slice.
4. Add explicit document subscriptions and aggregation after volume/replay tests.
5. Evaluate job/operational notifications separately against real lifecycle events.

Required tests: domain rollback creates no notification; late commit is not skipped;
crash/replay does not duplicate delivery; cleanup cannot lose pending work; retries
and poison events remain observable; no-op grants produce no misleading alert;
resource move preserves subscription; revoked users see no content metadata/count
leak; scoped PAT/SSO/multi-account isolation; concurrent grouping and read-all;
large inbox pagination; unknown/missing backend capability; mobile, keyboard,
dark mode, error recovery and reader return navigation. Run repository gates for
the changed layers; benchmark fan-out before enabling bulk Vault subscriptions.

## References

- [Confluence notification inbox](https://support.atlassian.com/confluence-cloud/docs/view-your-notifications/): personal relevance through watched content and collaboration events.
- [Confluence watch model](https://support.atlassian.com/confluence-cloud/docs/watch-pages-spaces-and-blogs/): explicit subscriptions.
- [GitLab notifications](https://docs.gitlab.com/user/profile/notifications/): personal settings, subscription scopes, suppression of own activity and rate limits.

Borrow relevance and control, not their entire feature matrix. The original
research preceded the working-branch implementation described above; production
deployment has not been performed as part of this work.
