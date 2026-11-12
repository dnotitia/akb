# Resource change events

AKB records committed resource changes in the PostgreSQL `events` outbox.
With `redis_url` configured, the existing publisher delivers them to
`redis_event_stream` (default `akb:events`). No additional daemon or Redis
materialized graph is required. Events invalidate a consumer's cached reads;
they are not document bodies, row deltas, exact counts, or execution lineage.
Read current content, tables, collection membership and counts through AKB.

## Supported production write boundaries

| Resource / action | Public kind | Publication boundary |
| --- | --- | --- |
| Native document create, including import and Vault skill seed | `document.put` | Native authority transaction |
| Native body edit or metadata/status update, including archive/unarchive | `document.update` | Native authority transaction |
| Native document move / rename | `document.move` | Native authority transaction, both paths retained |
| Native document delete | `document.delete` | Native authority transaction, deleted identity retained |
| Native authority restore (internal) | `document.restore` | Native authority transaction |
| Ordinary File upload confirmation | `file.put` | File catalog transaction after byte verification |
| Ordinary File replacement confirmation | `file.update` | File catalog transaction after replacement verification |
| Ordinary File delete | `file.delete` | File catalog deletion transaction |
| Collection recursive deletion | `collection.delete` | Aggregate event; invalidate the collection / Vault |
| Tables and rows | Existing `table.*` kinds | Registry transaction / statement-level row trigger |

A failed mutation or rolled-back transaction emits no committed change event.
Native mutation-key replay returns the committed revision without adding an
outbox row. This is distinct from Redis delivery, which can retry the same
outbox event. Ordinary File upload reservations and object-store byte PUTs are
not confirmed resource changes. File upload reconfirmation does not have the
Native mutation-key contract; do not interpret event cardinality as a count.

Ordinary Files have no public move or restore API. Native searchable File
projection create/replace/move/delete/restore is derived work: it must not emit
another public File lifecycle event. In particular, replacing text with binary
can remove its searchable projection while the original File still exists.
The Native authority restore hook currently has no public document restore
endpoint. Experimental M1 File measurement drivers are outside this production event
contract. Arbitrary external filesystem edits are not detected by this contract.

## Native document payload

The existing envelope contains `id`, `occurred_at`, `kind`, `vault_id`,
`resource_uri`, `actor_id` and JSON `payload`. The native document payload is:

```json
{
  "vault": "example",
  "path": "notes/final.md",
  "collection": "notes",
  "resource_id": "11111111-1111-4111-8111-111111111111",
  "revision_id": "opaque-native-revision",
  "commit_hash": "opaque-native-revision",
  "previous_commit": "previous-opaque-native-revision"
}
```

`resource_uri` is built with the canonical URI helper, for example
`akb://example/coll/notes/doc/final.md`. The stable `resource_id` survives move,
delete and restore. `commit_hash` is the existing compatibility name for the
same opaque revision; it is not a Git hash. `previous_commit` is null on create.
Root documents use `collection: ""`. No body, frontmatter, custom mutation
message, or storage locator is copied into the native event.

Moves additionally include `old_path`, `old_uri`, and `old_collection`; invalidate
both locations. Updates include `previous_status` and `status` when the document
facade supplies them. A consumer needing exact status/content must read AKB.
Delete retains the last path and URI even though the current document cannot
be read. A restore invalidates the collection and document again. An update
may alter visibility without changing an archive-inclusive count.

## Consumption and delivery

Use the Redis stream-entry ID to resume transport and the envelope `id` to
recognize a repeated domain event. Delivery is at least once: XADD may succeed
before the publisher records its acknowledgement. The approximate configured
MAXLEN bounds retained entries; it is not a time retention guarantee. After a
trim gap, stream reset or database restore, reconcile current state through AKB.
Do not increment counts from event numbers or assume the stream is a permanent
history. Capture a starting cursor before initial reads and replay changes that
arrive during those reads.

For collection events `resource_uri` may be null. Use the Vault identity and
payload path, or conservatively invalidate the Vault. Relation declarations and
actual policy execution causation are separate from resource mutation events.
Redis is an operator channel, not a per-user ACL boundary: browser consumers
need a backend that applies Vault access and projects safe fields. The existing
authenticated `GET /api/v1/events/{vault}` SSE tail reads the same PG outbox with
reader access checks; its opaque cursor is different from a Redis cursor.

Each server process wakes its open SSE tails from one dedicated PostgreSQL
`LISTEN` connection outside the request pool. The `events` insert trigger's
notification names the event's Vault (`<vault id>:<event id>`, migration 131),
and only that Vault's tails wake; an event without a Vault wakes none. A
database that has not yet applied migration 131 sends the event id alone, and
then every tail wakes. A notification only wakes a tail, which then reads the
outbox by cursor. If the listener connection drops, open tails keep re-reading
on their 15-second heartbeat until it reconnects, so a missed wake-up delays
delivery rather than dropping it.

Opening a tail first decides admission, which reads nothing. It then checks
the caller's access and reads the Vault's retained range on one connection,
before any event is read. The tail's first frame is a checkpoint at its
starting position, sent on that access check, so a client that brought no
cursor holds one from the start; store it like any other cursor.

A waiting tail holds no pooled connection. To read, it borrows one for the
batch and, when the batch has rows, checks the reader's access on that same
connection before sending any of them. A pass that finds nothing checks
nothing, except the pass after each heartbeat. Before each frame, a check that
started more than a second earlier is repeated, and so is a check that itself
took longer than that to finish. A tail whose checks keep taking that long,
the first check of a batch included, ends after three in a row with no further
frame, and its client resumes from the last cursor it received. Heartbeats
keep their 15-second interval while a batch is read or access is checked. A
revoked reader stops receiving events within about a second and gets no
further frame, and an idle tail closes within one heartbeat.

A cursor is refused with 410 `event_gap` only when an event it never received
was deleted. A trigger on `events` records, per Vault, the highest event id
deleted, in every statement that deletes, whichever code issues it: this
release's sweeper, an earlier release's during a rolling deployment or after a
rollback, or an operator (migration 133). A cursor below that mark is refused,
and the refusal's `earliest_cursor` resumes at it.
Event ids are shared by every Vault, so a cursor taken before a Vault's first
event, on an empty Vault, is usually far below that event and is still valid.
`start=earliest` replays from the first position after which nothing was
deleted. For a Vault that already had events when migration 133 ran, the mark
starts just below its earliest retained event, where the boundary was before.

All tail database work in a process, opening included, shares at most
`event_tail_db_concurrency` connections. Work beyond that waits for a free
slot without holding a connection, and a slot is free again only once its
connection is back in the pool, even when the stream that used it was
cancelled. A connection that takes more than five seconds to go back, as a
half-open one can, is closed instead, which frees its slot and its place in
the pool. Tail load, a burst of reconnects included, therefore cannot take
the pool that other requests and the readiness probe need. At startup a server
process logs a warning when its pool size (`pg_pool_max_size`, or
`AKB_PG_POOL_MAX_SIZE`) less `event_tail_db_concurrency` and
`write_lane_concurrency` leaves fewer than four connections for everything
else.

Limits per server process; each environment variable overrides its key:

| Limit | Key (environment variable) | Default | Past the limit |
| --- | --- | --- | --- |
| Open tails | `event_tail_max_streams` (`AKB_EVENT_TAIL_MAX_STREAMS`) | 256 | 503 `event_tail_capacity` with `Retry-After`; retry later and resume from the last cursor |
| Open tails of one account | `event_tail_max_streams_per_user` (`AKB_EVENT_TAIL_MAX_STREAMS_PER_USER`) | 48 | 429 `event_tail_user_quota` with `Retry-After`; close tails the account no longer reads |
| Pool connections for tail database work | `event_tail_db_concurrency` (`AKB_EVENT_TAIL_DB_CONCURRENCY`) | 6 | the work waits for a free slot |

A new tail's errors come in this order: 403, or 404 for an unknown Vault; then
429 for an account at its quota and 503 for a full process; then 400 for an
invalid kind or cursor, and 410 for a cursor that missed a deleted event. A
caller without access is told so even when it would also be refused for
capacity, and an account at its quota gets 429 even when the process is also
full: retrying cannot help either, and the SDK's change channel retries a 5xx
but stops on a 4xx. `GET /health` reports the process's `event_tail` counters:
open tails, accounts holding them, the limits above, database slots in use and
waiting, and whether the listener is connected. It names no account or Vault.

## Regression verification

`backend/tests/test_native_document_events_postgres.py` exercises the five
native mutations, old/new move scopes, deletion identity, transactional rollback,
mutation-key retry, obsolete-head rejection and caller-owned transactions.
With `AKB_TEST_REDIS_URL`, it also delivers them through the real publisher to
an isolated Redis stream. `test_notification_native_postgres.py` preserves inbox
behavior without duplicate notifications. `test_native_file_projection_pg.py`
verifies ordinary File events are not duplicated by projection retries or
text/binary transitions.

Use a disposable PostgreSQL instance capable of creating test databases; these
suites create and remove their own databases. `REQUIRE_REAL_PG=1` makes the native
document suite fail if PostgreSQL is unavailable, and `REQUIRE_REAL_REDIS=1`
requires the Redis integration input. Never point these suites at a shared
application database as a general fixture target.

For an explicitly authorized deployed endpoint, use the [live acceptance
driver](../../scripts/e2e/README.md). It checks REST writes and actual Redis
delivery with a disposable Vault.
