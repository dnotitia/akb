# Personal notifications

Personal notifications provide an inbox for access changes and documents you
explicitly watch. They are separate from Activity history and indexing progress.
This guide describes the implemented feature. Deployment is a separate step.

## Read your inbox

Open the bell in the global header. Choose **All**, **Documents**, or
**Access** (invitations and permission changes), then optionally enable **Unread only**. Categories and
read state are independent server-side filters, applied before pagination.
Both the panel and full inbox offer **Load more**. **View all notifications**
opens the full inbox with the selected filters preserved in its URL.
Opening the bell does not mark items read. Open an available item or choose
**Mark read**; **Mark unread** reverses that state.

Rows are grouped into **Today**, **Yesterday**, and **Earlier** in your local time.
Use the header **…** menu for **Notification settings** or
**Mark all notifications read**, which acknowledges the server snapshot across
every category. A later change
remains unread, including a new edit added to a previously read group. If an item
changes while you mark it, the interface refreshes and asks you to try again.

The count normally refreshes every 45 seconds while the page is foregrounded,
and refreshes on focus. Errors use a longer retry interval. An unavailable or
older backend displays an unavailable state, rather than claiming an empty inbox
or a zero unread count.
If an older notification server does not support category filtering, the UI
offers **Show all categories** rather than mislabelling an unfiltered response.

## Watch documents

Choose **Watch** in a document reader to receive its changes; choose **Unwatch**
to stop watching. Account Settings lists your accessible watched documents and
also offers **Unwatch**. Reading, creating, or starring a document does not
subscribe you, and a public vault does not subscribe all readers.

Watches use the document's stable UUID, so renaming or moving the document keeps
the watch. Recipients are captured when a change queues its notification; starting
a watch does not replay earlier queued changes. Your own document changes are
suppressed. REST and MCP document writes use the same server-side producers.

Ordinary updates to the same document are grouped into fixed five-minute
event-time buckets. These are clock buckets, not a rolling five-minute timer:
edits on opposite sides of a bucket boundary can appear separately. Moves,
archive/restore transitions, and deletions remain separate items.

Native documents currently display their **current filename**, including its
extension, in notification and watched-document labels. Their frontmatter title
is not used for these labels. Targets still resolve to the current Native resource
and path. Legacy document labels use the current stored document title.

## Follow watched documents from Home

Home shows **Recent updates** and **Watched documents** in separate sections,
side by side on wide screens and stacked on smaller screens. Recent updates
shows accessible document changes; Watched documents limits its own list to
documents you currently watch. Each document appears once per section, ordered by its latest modification,
including your own edits. A newly watched document can appear immediately with
its actual modification time; this does not replay old notifications.

**Recently viewed** remains your browser-local recently visited list. Watching
is neither a second inbox nor an unread list: opening a Home document preview
does not acknowledge notifications. **Manage watches** opens notification
settings, where you can review and stop subscriptions.

The server applies watch and access filters before pagination, using stable
document IDs so moves and renames retain the watch. Deleted and inaccessible
documents are excluded. Show more loads another page; a failed request preserves
the visible documents for retry. Older servers without watch filtering show an
unavailable notice instead of displaying unfiltered results as Watching.

## Access and privacy

Access notices target the affected account. Repeated or stale grants and changes
that do not alter the effective role do not create a role-change notice. Removing
direct membership is distinguished from losing access: public access or another
grant basis may still allow reading. Ownership transfers notify the previous and
new owner.

Watching never grants permission. Delivery and inbox reads check current vault
access, and inaccessible document items do not contribute to the unread count.
Deleted document notices contain generic wording and no document link. A personal
access-removal notice can remain after revocation, with no private document
metadata or unusable resource link.

Inbox and watch endpoints accept supported human browser sessions: local JWT
sessions and SSO browser sessions. PATs, service credentials, and OAuth API tokens
cannot use them, including a PAT scoped to only one vault. Existing browser
authentication and CSRF handling still apply.

## Server configuration

The top-level settings in `config/app.yaml` are:

```yaml
notifications_enabled: true
notification_retention_days: 90
```

Retention accepts 1–3650 days. The inbox displays the configured duration.
Cleanup removes inbox entries based on their last update time; a grouped update
therefore refreshes that group's retention age. Cleanup runs periodically in the
notification worker, so expiration is not an exact wall-clock deletion deadline.

Migration `099_personal_notifications.py` creates the inbox, subscription,
delivery-deduplication, recipient-version, and work tables. Eligible domain writes
queue bounded notification work in the same PostgreSQL transaction. Work contains
stable IDs and minimal role metadata, not document bodies, credentials, or private
URLs. A dedicated task in the existing worker process delivers the work. This
feature does not require Redis, SMTP, or a separate service.

Disabling notifications stops new notification work and worker startup, and the
APIs report the feature unavailable. Historical Activity events are not replayed
when it is enabled. Existing pending work and stored inbox rows are not erased by
that setting. Email, browser push, webhooks, general preference rules, and vault
subscriptions are not implemented.

## Investigate delivery problems

The detailed `/health` response includes `notifications.enabled`, `pending`,
`failed`, and `oldest_pending` when the check succeeds. Pending means queued work
that has not completed or failed terminally; failed work remains available for
inspection. Check the field over time to distinguish a brief backlog from a
stalled worker. An unavailable health check is not evidence of an empty queue.

The worker claims work with a 60-second lease and locks the row while processing.
It retries errors with backoff and marks work failed when an error reaches the
eighth attempt. Recipient delivery and work completion commit together; restarting
the worker can recover an expired claim without duplicating completed delivery.
Cleanup preserves pending and failed work independently of Activity retention.

Start an investigation with read-only checks: verify the configured feature
state and worker process, inspect `/health`, and correlate the work ID in worker
logs. With an authorized database connection, this query inspects operational
state without displaying recipients or document metadata:

```sql
SELECT id, kind, created_at, attempts, available_at,
       lease_until, processed_at, failed_at
FROM notification_work
WHERE processed_at IS NULL
ORDER BY created_at, id
LIMIT 100;
```

Resolve the underlying worker or database error before planning recovery. There
is no notification retry administration endpoint in this release. Do not clear
delivery keys, mark pending work completed, or delete failed rows as a routine
diagnostic step: those writes can lose notifications or defeat deduplication.
Retain the failed work and use the project's reviewed operational change process
for any repair.

The design and remaining acceptance checks are recorded in the
[implementation design](../design/accepted/2026-09-08-user-notifications/README.md).
