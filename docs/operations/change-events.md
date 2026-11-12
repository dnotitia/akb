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

Within one Vault, the authenticated SSE tail described below delivers every
committed event in commit order, and its cursor never skips an event. Event ids
follow commit order inside a Vault: an insert trigger (migration 132) locks the
event's Vault, holds the lock until the transaction commits or rolls back, and
only then draws the id. A transaction that commits later therefore always
carries a larger id than every event of that Vault already visible, and reading
`id > cursor` cannot pass over one that commits late. The trigger assigns every
event id; `events.id` has no column default. The migration also sets the id
sequence's cache to 1 if it was larger: with a cache, each session draws from
its own block of ids and could draw a smaller id after another session's larger
one. PostgreSQL gives the sequence new storage when the cache changes, and a
session that cached a block before the migration drops it at its next draw, so
no id cached earlier is used afterwards. A dynamic table's
`table.rows_changed` event is inserted when its transaction commits. The cursor
format, `Last-Event-ID` resume and `410 event_gap` are unchanged, and cursors
issued before the migration stay valid. Ids are not contiguous; a gap does not
mean a missing event. Events without a Vault (account and sign-in events) are
not ordered this way. The Redis stream has no order guarantee: the publisher
claims rows in batches and retries failures, so entries can arrive out of id
order. Recognize a repeat by the envelope `id`.

The `410 event_gap` check compares a cursor with the oldest retained event of
the Vault and treats the Vault's ids as if they were contiguous. Other Vaults'
events and rolled-back inserts take ids in between, so once retention removes
the event a client read last, a resume can be refused with `410` although
nothing was missed; the client restarts from the returned cursor and
reconciles. This predates the ordering change; a per-Vault deletion watermark
is planned to replace the check.

This ordering has a cost. Transactions that write events for the same Vault
run one at a time, each from the moment it takes the Vault's lock to its
commit. AKB inserts a transaction's events after its other locking statements
(its row writes, DDL and role changes), so that span is short. Bare Git
document writes are the exception. A Git commit does not roll back with the
transaction, so put, update, edit, move and delete first lock the rows they
change after the commit (their collection, and the edges of a moved or
deleted document), then take the Vault's lock, and only then commit to Git; a
busy Vault fails them before Git changes. They hold the Vault's lock through
the Git work and their remaining database writes up to their commit, and a Git
command may run for up to 30 seconds (`git_write_timeout_secs`). In a Bare Git
installation the other writes of a Vault therefore also wait for a document
write in progress. The Vault's lock is a row lock in `vault_event_order`, a
table no SQL role is granted, so a statement run through `akb_sql` cannot hold
it; `akb_sql` also refuses PostgreSQL's advisory lock functions. Every Vault
has its row from the start: a trigger on `vaults` inserts it with the Vault and
deletes it with the Vault, and the migration inserts the rows of existing
Vaults. An event of a Vault without a row, such as one deleted meanwhile,
inserts the row.

A transaction waits at most 5 seconds in total for its Vault's lock, however
many others wait with it: it polls the lock against one deadline instead of
queueing for it, and when the row is missing it creates the row within the
same deadline. A lock taken after the deadline counts as not taken. Waiters are
not served in arrival order, and a lock cycle through the Vault's lock (the
holder waits for a row that a waiter holds) ends at the waiter's deadline, not
through PostgreSQL's deadlock detection. Past the deadline the waiting
transaction fails with SQLSTATE `55P03` and the constraint name
`akb_event_lock_timeout`, and rolls back. That error alone is answered with a
retryable `write_busy`: HTTP 429 with `Retry-After: 1`, or `retry_after_secs`
in the MCP envelope. Any other lock wait past its limit, such as one under a
`lock_timeout` set on the database role, and a deadlock (`40P01`) remain
internal errors, because either can end a Bare Git write after its Git commit.

A waiting transaction keeps its pooled database connection while it waits.
Because a holder keeps the Vault's lock from its event insert to its commit,
and a Bare Git document write keeps it through its Git work as well, other
writers of the same Vault can each wait the full 5 seconds with a connection
held, and then fail with `write_busy`. They accumulate when a holder is slow or
when one Vault receives more event-writing transactions than it can commit one
at a time, and they take connections that other Vaults' requests and the
readiness check also need. AKB does not bound the number of waiters: admission
control in front of the connection pool for writers of the same Vault, and a
readiness check that does not depend on the shared pool, are deferred to a
separate change. Until then the 5-second deadline and `write_busy` bound each
wait.

During a rolling deployment the first upgraded process applies migration 132.
Processes still on the previous release then take the Vault lock too, but some
of their writes insert the event before other locking statements, and their
Bare Git document writes reach the lock only after their Git commit. Until they
are replaced, a writer that waits for a row such a write holds fails with
`write_busy` at its deadline, and a previous-release document write that waits
for a busy Vault past the deadline fails after its Git commit, leaving that
commit in Git but not in the database. The migration asks for its table lock
for at most half a second at a time and holds nothing between attempts, so
event inserts keep flowing while a previous-release transaction is still open;
after 40 attempts it stops the server's start with an error. Rolling back to an
earlier image leaves the trigger in place and working, with the earlier
release's statement order and the same exposure for its Bare Git document
writes, so upgrade again rather than run an earlier release for long. Do not
drop the trigger: without it an INSERT that names no id fails, because the
column has no default.

A full `pg_dump` / `pg_restore` creates the triggers after it loads the data,
so restored events keep their ids. A data-only restore into a database that
already has the schema needs three precautions. Stop writes first: stop the API
and worker processes, because with triggers disabled an event written during
the load would get no Vault lock, and with no column default its insert fails.
Run as a superuser with triggers disabled and in one transaction, so that a
failed restore rolls back completely, the trigger switch included:

```bash
pg_restore --data-only --disable-triggers --single-transaction --dbname <database> <dump>
```

With the triggers active, every restored Vault event would receive a new id and
the restore would lock every Vault until it commits. After the restore, whether
it succeeded or failed, and before starting AKB, check the two triggers on
`events`; the query must return both rows with `enabled = true`:

```sql
SELECT tgname, tgenabled = 'O' AS enabled
  FROM pg_trigger
 WHERE tgrelid = 'public.events'::regclass
   AND tgname IN ('akb_events_commit_order_trigger', 'akb_events_notify_trigger');
```

No other trigger may stay disabled either; this query must return no rows:

```sql
SELECT tgrelid::regclass AS table_name, tgname
  FROM pg_trigger
 WHERE tgenabled = 'D';
```

Re-enable a table it lists with `ALTER TABLE <table_name> ENABLE TRIGGER ALL;`.

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
text/binary transitions. `test_events_commit_order_postgres.py` keeps one
event-writing transaction open while a second writer of the same Vault tries
to commit first, through `emit_event` and through the dynamic-table row
trigger, and checks that a reader following the cursor receives both events in
commit order, and that the wait for a busy Vault ends at one deadline, also
when the Vault's lock row has to be created first. The
`test_*_event_lock_postgres.py` suites check that SQL admin overrides, Vault
settings changes, table migrations (enum value renames included) and app
rollouts insert their events last, so a writer of the same Vault does not wait
behind them while they still take other locks; that a Bare Git document write
on a busy Vault fails before its Git commit, and waits for a concurrent
collection create before it takes the Vault's lock; and that only the lock's
own deadline is reported as `write_busy`.

Use a disposable PostgreSQL instance capable of creating test databases; these
suites create and remove their own databases. `REQUIRE_REAL_PG=1` makes the native
document suite fail if PostgreSQL is unavailable, and `REQUIRE_REAL_REDIS=1`
requires the Redis integration input. Never point these suites at a shared
application database as a general fixture target.

For an explicitly authorized deployed endpoint, use the [live acceptance
driver](../../scripts/e2e/README.md). It checks REST writes and actual Redis
delivery with a disposable Vault.
