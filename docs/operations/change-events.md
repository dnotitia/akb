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
