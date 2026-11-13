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
transaction, so put, update, edit, move and delete first lock their collection
and then their document, the order in which a recursive collection delete
takes them, then the edges a move or delete changes after the commit (an update
or edit rewrites its document's implicit edges at this point instead of after
the commit), then take the Vault's lock, and only then commit to Git; a busy
Vault fails them before Git changes. Every transaction that locks several
collections, a move or a recursive collection delete, locks them in id order.
A transaction that creates a collection also locks the collection's existing
ancestors (`FOR KEY SHARE`), in id order and in one pass with any other
collection it locks, such as a move's source; a recursive collection delete
locks the collection at its prefix and those under it `FOR UPDATE`, so either
the creation waits for the delete or the delete waits for the creation and
then deletes the new collection too. Every insert into `collections` goes
through `CollectionRepository`, which takes these locks; a measurement File
upload removes expired transfer intents in a transaction of its own before it
creates the collection, since a delete removes a collection's intents with it.
After it has read the documents, files and tables under its prefix, the delete
reads the collections under it once more and starts over if one appeared; this
covers a prefix that has no row of its own, where a creation finds no ancestor
to lock. These rules apply to what the delete selected: the documents, files,
tables and collections it read go together, and a collection whose contents it
removed is removed with them. A write that commits after the delete's reads is
not part of it; see the known limits below for the Native authority.
A write that finds its document moved after locking it, or a move that finds
its destination collection gone, rolls back to a savepoint, releasing every
collection and the document, before it tries again. They hold the Vault's lock through
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

A transaction that waits for the Vault's lock keeps its pooled database
connection while it waits. A holder keeps the lock from its event insert to its
commit, and a Bare Git document write through its Git work as well, so when one
Vault received more event-writing transactions than it could commit one at a
time, they took every connection that other Vaults' requests also needed.
Every transaction that may write a Vault's events (document, collection, File,
table, table row and `akb_sql` writes, access and Vault settings changes, app
rollouts, external Git synchronization, and a check that records an admin
override) is therefore admitted for that Vault before it takes a connection.
Each API process admits at most `vault_write_concurrency` (default 4) such
transactions of one Vault at a time; the others wait without a connection. A
transaction that writes several Vaults' events is admitted for all of them at
once or waits for all of them, holding none, so a full Vault does not delay
another Vault's writes; among waiters whose Vaults all have room, the earliest
goes first. One that waits longer than `vault_write_queue_timeout_secs`
(default 10 seconds) fails with a retryable `write_busy`: over REST, HTTP 429
with `Retry-After: 1` and a body whose `code` is `write_busy` and whose
`details` carry `retry_after_secs`; over MCP, the same envelope. A Bare Git
document write enters the Bare Git write lane first and then this admission.
The limit is per process: with several processes a Vault can have that many
admitted transactions in each, and the event lock still orders them, so an
admitted transaction can still wait up to 5 seconds for it. An event written
for a Vault outside its admission is logged once per event kind. The readiness
check still uses the shared pool.

These guarantees cover what the application writes. An unscoped system
administrator's raw SQL runs under the backend's own database role, so it can
also write AKB's system tables, such as `events`, or call
`akb_lock_vault_events`. Such a statement is admitted for the Vaults it
declares and those whose dynamic tables its plan modifies, not for a Vault
whose events or event lock it reaches that way.

A write refused this way has written none of its own events, and no Git
commit, document, Revision, prepared Native payload, collection, table, row or
access change: those paths are admitted before their first database write, and
a Native document write before it stores its payload. The access check that
precedes a write is a separate step: when it records an admin override (an
unscoped system administrator writing a write-managed Vault), it commits that
`vault.write_policy_admin_bypass` event in a transaction of its own, admitted
for the Vault, before the write is admitted, and the event stays when the write
is then refused. What a refused write may have done before its admission is
read, such as resolving a Vault's name to its id, and the following, which
leave the Vault's content unchanged:

- A File upload confirmation is admitted twice: to renew the upload's lease,
  and, after it read the upload from object storage, to publish the File. A
  refusal of the publication leaves the renewed lease committed and the File
  pending; the same confirmation sent again publishes it.
- A File replacement copies the uploaded bytes and schedules the copy's
  removal before its publication is admitted. A refusal removes the copy,
  leaves its scheduled removal, and keeps the upload, so the same confirmation
  can be sent again once the Vault has room.
- An app rollout leases its target before the target is admitted, and waits
  at most `app_rollout_admission_wait_secs` (default 1 second) for the
  target's Vault. A refusal makes the target pending again and defers it,
  with every other pending target of its Vault, for 5 seconds, in one
  statement, and the worker claims again at once rather than sleep its idle
  interval, so it takes other Vaults' targets meanwhile; once the deferral
  ends the targets are claimed again in their usual order.
- External Git synchronization fetches the remote into its mirror before it
  records each changed file, admitted per file.
- A raw SQL write reads its plan on a pool connection before its admission,
  described below.

Two requests are made of several writes, each admitted on its own, so a
refusal part-way leaves their earlier writes committed; their error says so
with `partially_applied` in its details:

- A Native recursive collection delete is admitted for each document's
  transaction and then for the collections'. A refusal part-way, or the
  Vault's event lock timing out part-way, leaves the documents deleted so far
  deleted, each with its event, and fails with a `write_busy` whose hint says
  that the same request sent again continues it.
- A grep replacement (`akb_grep` with `replace`) writes each document as a
  write of its own and stops at the first failure. A refusal, or any other
  failure, after some replacements committed leaves those documents replaced
  and listed in `replacements`. The hint names them with their new revisions
  and asks to apply the replacement only to the documents not replaced yet:
  the same request sent again would replace the text again in a replaced
  document that still matches the pattern.

A write whose transaction finds its Vault by name, such as an access or Vault
settings change or raw SQL, resolves the name to the Vault's id for its
admission before the transaction. When the transaction finds another Vault
under that name, because the Vault was deleted and another created with the
name meanwhile, it releases its connection and admission and starts again for
the Vault it found; a second change fails with a conflict (HTTP 409). Its
statement has not run when that happens.

Creating a Bare Git Vault takes the new Vault's write-lane gate and then its
admission before its first Git commit and before its row is visible, and holds
both through the seed document, so a request that reaches the Vault as soon as
its row commits waits behind the creation. It holds no global write-lane slot
meanwhile: each of its Git operations, and the seed's own write, takes one for
itself, so creations waiting for something else, such as the role authority
lock, leave the slots to other Vaults' writes. The seed's write, and the
compensation that removes the Vault's row and repository when the creation
fails, come after the first Git commit, and wait for their slot as other Vault
lifecycle work does, without the lane's deadline: giving up there would leave
a half-created Vault. A Native Vault's creation is admitted before its
transaction, which commits the Vault with its seed.

The table API's row reads (`GET /api/v1/tables/{vault}/{table}/rows` and
`POST /api/v1/tables/{vault}/{table}/query`), which the server compiles to a
`SELECT`, run in a READ ONLY transaction and are not admitted. Raw SQL
(`akb_sql` and `POST /api/v1/tables/{vault}/sql`) takes a `read_only` option,
false by default. With it, the statement runs in a READ ONLY transaction, which
PostgreSQL enforces, without admission; use it for queries, so a busy Vault
does not hold them back. Without it, any statement, a `SELECT` included, is a
write for admission: it waits for its Vaults' admission and fails with
`write_busy` when they stay busy. Over MCP, an `akb_sql` call whose token or
OAuth scopes grant read but not write always runs read-only; the REST SQL
endpoint is a POST and needs a write-scoped credential. A write runs exactly
once: nothing tries it first to find out whether it writes. It is admitted for
the Vaults the request declares and for every Vault whose dynamic tables its
plan modifies. The plan is read with `EXPLAIN`, which does not run the
statement, on a pool connection taken before the admission: at most
`akb_sql_plan_concurrency` (default 2) plans are read at once per process,
and at most one for each Vault: a plan holds a token for every Vault its
statement declares, taken all at once or not at all, as the admission takes
its slots. The others wait without a connection or a token, and are served in
arrival order among those whose Vaults are all free. The plans of a Vault
whose table is locked therefore wait one behind another, whatever other Vaults
they declare with it, and another Vault's statement takes the free slot. A
wait that ends without a slot, at its deadline or cancelled, leaves the queue
at once. A plan that waits longer than `akb_sql_plan_lock_timeout_ms` (default
500) for a table's lock fails with a retryable `write_busy` rather than hold
its connection.
Before the write commits, its transaction checks the dynamic-table row changes
it queued for `table.rows_changed` events; a change of a Vault it was not
admitted for rolls it back with an `invalid_argument` error that names that
Vault in `undeclared_vaults`. `akb_sql` accepts one SELECT, WITH, INSERT,
UPDATE or DELETE statement, so a caller cannot create a function that writes
out of the plan's sight, run a `DO` block, or have the deferred event trigger
fire early with `SET CONSTRAINTS`; and no function a SQL role may call writes
a table. The check before commit is for functions an administrator installs.

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

Known limits that predate the ordering change:

- A Native document write commits the document first and registers its
  collection row afterwards, on its own and on a best-effort basis. When a
  recursive delete removes the document and its collection in between, the late
  registration creates the collection row again, empty.
- Native document writes do not lock collection rows. A document that a Native
  write commits into an existing sub-collection after a recursive delete read
  the prefix's documents is not deleted, while its collection row is: the
  document stays without a collection row.
- In a database cut over from Bare Git, the `documents` rows stay as immutable
  history, and a trigger refuses to change them, including the `SET NULL` that
  deleting a collection they reference would perform. A recursive collection
  delete therefore leaves those rows and the collection rows they reference in
  place, while it deletes the Native documents, the other collections, files
  and tables. Its response lists the collections left in place as
  `retained_collections`, with a `hint` that says why, and so does the
  `collection.delete` payload; the same delete sent again answers the same
  way, and browsing shows those collections, empty. Removing such a collection
  row needs a schema change.
- `akb_link` and the REST link endpoint resolve a document endpoint without
  the lock a Native delete takes, and an edge's key accepts a deleted
  Resource. A link that resolved a document before that document's delete
  committed therefore inserts its edge after the delete removed the
  document's edges, and a document restored before the derived worker runs
  keeps that edge. A single-document delete behaves the same way.
- A link from or to a document cut over from Bare Git resolves to the
  document's preserved `documents` row and stores no Resource identity.
  Neither a single-document nor a recursive delete removes it while that row
  exists, and the document restored keeps it.
- The derived worker keeps one pending intent per Resource, the one with the
  latest `occurred_at`, which comes from the clock of the process that wrote
  it. When processes' clocks disagree, an earlier Revision's intent can carry
  the later time; the worker then closes the current Head's intent as
  superseded, and the Head's derived state is not updated: a deleted
  document's chunks stay. This holds for every Native write.

On the Native authority a recursive collection delete removes the documents
first, one at a time. Each document's delete and its `document.delete` commit
together in a short transaction that locks only that document's Resource, so the
delete never waits for a second Resource while it holds one. It does not lock
every document under the prefix before it deletes the first, which would hold
all of them for the whole delete; a delete stopped part-way is completed by
running it again instead. A document that
another request deleted, or moved out of the prefix, meanwhile is left as that
request left it and is not counted. The collections, files and tables under the
prefix, with the `collection.delete`, follow in a last transaction, once no live
document is left under the prefix; a document written there meanwhile is
deleted in another round, up to three, before the delete gives up with a
retryable conflict. A failure part-way, including a `write_busy` from the
Vault's event lock, returns the error and leaves the documents deleted so far
deleted, each with its event; running the same delete again continues with the
documents that remain. When nothing is left under the prefix because an earlier
recursive delete of it deleted its documents, the delete succeeds with zero
counts rather than reporting the prefix as not found, and records
`collection.delete`; it tells this from a prefix nothing was deleted under by
the Revisions that delete gave its documents, whose subject is
`[delete-collection] <prefix>`, and which are kept. `deleted_docs` counts the
documents that call deleted.

A caller that may not delete tables (a writer, not an admin or owner) is
refused before any document goes when the prefix holds a table. A table that
another request creates under the prefix after that check is found by the last
transaction, which checks again under its locks, once the documents are
deleted. They stay deleted, and the refusal (HTTP 403, `permission_denied`)
says so: `partially_applied`, `deleted_docs` and the deleted documents'
`deleted_uris` in its details, and a hint. The same request from that caller is
refused before anything else goes; only a caller who may delete the table can
finish the delete.

`collection.delete` is at least once. Every delete that finds the prefix
empty records it, so one prefix can have it more than once: when a delete
stopped after its documents and is run again, or when two deletes of the same
prefix finish together. Consumers treat it as "this prefix is gone", which a
repeat does not change. Within akb-oss only the event tail and the Redis
publisher read it, and they pass every event on; the gardener schedulers
ignore the kind or de-duplicate events without a resource by Vault and kind,
and Reef reads only `table.rows_changed`.

Each document's graph edges, keyed on its Resource, are removed in the
document's own delete transaction, while its Resource is locked and before
its event. A document restored afterwards therefore starts without the links
it had when its delete committed and keeps the ones made after its restore,
apart from the link races and the cut-over documents listed among the known
limits above. Edges are
rows that other writers, such as a move re-linking its document, lock in their
own order, so that transaction can end in a deadlock (`40P01`); the request
then fails, the document stays, and sending the same delete again continues.

A deleted document's chunks are removed by the derived worker, from the
invalidation intent the delete records; the worker also removes the
document's edges once more. The worker locks the Resource and closes the
intent as superseded once the Resource has a later Revision. Its transaction
can also end in a deadlock, or fail otherwise; the worker then retries the
intent with backoff: 1, 5, 15 and 30 minutes, then 1, 2 and 4 hours between
its attempts. A claimed intent that is not finished, as when its process
stopped, is claimed again once its 10-minute lease expires, except after the
final attempt. When the eighth attempt (`MAX_RETRIES` in
`app/services/_backfill.py`) fails too, the intent is abandoned: it is
completed with `delivery_outcome = 'abandoned'`, and the derived state of that
Revision, such as the deleted document's chunks, stays as it was. An intent
whose eighth claim ends without an outcome is not claimed again; the queue
rescuer, which runs every minute, marks it abandoned once that claim's lease
expired. An abandoned intent's `last_error` is the exception class that ended
its last failed attempt, or `claim lease expired after final attempt` when no
attempt recorded one. Abandoned intents are counted as
`native_derived.abandoned` by `GET /health`, and per Vault by
`GET /health/vault/{name}`; the rows are those of `native_invalidation_intents`
with `delivery_outcome = 'abandoned'`. Once the cause is fixed, an operator
re-opens them with `app.services.native_derived_worker.requeue_abandoned(
namespace_id=..., resource_id=...)` (the Resource is optional), which returns
how many it re-opened. It re-opens an intent only while its Revision is still
the Resource's Head, and closes the others as superseded: the work of an
earlier Revision, re-opened, could close the Head's own pending intent (see the
clock limit above). The worker then attempts the re-opened intents again from
the first step.

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
on a busy Vault fails before its Git commit, waits for a concurrent collection
create, collection delete or table drop before it takes the Vault's lock, takes
collections in the same order as a recursive collection delete, and leaves Git
and the database in agreement when a role's `lock_timeout` ends it; and that
only the lock's own deadline is reported as `write_busy`, over REST and MCP,
also when it fails a dynamic-table write at commit.
`test_collection_delete_child_create_postgres.py` commits a sub-collection
created under a recursive delete's prefix between the delete's lock and its
document reads, with and without a row at the prefix, and checks that nothing
remains under the prefix; it also checks that a creation locks its ancestors in
id order, together with a move's source, and that a measurement File upload
into a new sub-collection removes expired intents first, so none of them
deadlocks with the delete.
`test_vault_write_admission_postgres.py` sends 24 collection creates, and in
another case 24 confirmations of one pending File, to one Vault through a pool
of 8 connections while another transaction holds that Vault's event lock, and
checks that a reader of another Vault keeps getting a connection within a
second and that every request succeeds once the lock is free. It also checks
that a refused File replacement is confirmed again without a new upload, and
that a confirmation refused at its publication keeps its lease renewal; that a
new Bare Git Vault is seeded while another request holds its admission slot or
writes a document as soon as its row commits, and that creations waiting for
the role authority lock leave the Git lane to another Vault's write; that a new
Vault's seed, and its compensation when the seed fails, wait for a Git lane
that other Vaults' writes keep busy past its deadline; that a write whose Vault
was replaced under its name is admitted for the Vault it changes, over the
access API and raw SQL; that an app rollout target refused by admission is
released and applied by a later run, and does not hold back another rollout,
also under the worker's own runner, which sleeps after an iteration without
work, and with two busy targets of one Vault under the default deadlines;
that raw SQL runs read-only without admission only when asked to, and
otherwise as a write that waits; that raw SQL writes waiting for a table lock
while they read their plans leave connections for another Vault, and, sixty
of them declaring the locked Vault with sixty different companions, do not
keep another Vault's statement from reading its plan; that a statement declared
for one Vault whose plan writes another Vault's table runs once, admitted for
both, while one that writes an undeclared Vault through an installed function
is rolled back and not run again; that `akb_sql` refuses `CREATE FUNCTION`, `DO`
and `SET CONSTRAINTS` and that no function a SQL role may call writes a table;
that a Native recursive delete refused after its first document says it partly
completed; that a grep replacement refused after its first document, on Native
and Bare Git, names the document it replaced and asks to replace only the
rest; and that an admin override's audit event recorded before a refused write
stays. `test_sql_plan_slots_unit.py` checks the plan-slot limit, one plan
per Vault with every declared Vault's token taken at once, and that waits that
time out or are cancelled leave the queue at once and take no slot;
`test_write_busy_rest_unit.py` checks the REST response to a refused write and
to one refused part-way.
`test_vault_write_admission_paths_postgres.py` fills a Vault's admission and
drives each event-writing path, including raw SQL declared for one Vault whose
plan writes another Vault's table. Each must fail with `write_busy` without
changing anything listed above and without holding a pool connection while it
waits; it also scans the application for every place that writes a Vault's
events and fails when one has no such case.
`test_collection_delete_native_atomic_postgres.py` holds the Vault's event lock
after a Native recursive delete removed its first document and checks that the
delete fails with `write_busy`, that the first document stays deleted with its
event committed in its own transaction, and that running the delete again
completes it; that the delete holds one document at a time; that a document
deleted or moved out by another request while the delete waits is left alone
and not counted; on a database cut over from Bare Git, that the delete
leaves the preserved `documents` rows and the collection they reference, says
so in its response, and answers the same when run again, with browsing showing
that collection; that a document restored while the delete is held right after
that document's commit has lost its earlier link and keeps one made after the
restore; that a delete stopped after its documents completes when run again,
recording `collection.delete` at least once, while a prefix nothing was deleted
under is not found; that a table created under the prefix after a writer's
delete checked for one refuses the rest, listing the documents already
deleted, and that an admin's delete finishes it; that the derived worker
retries a cleanup that ended in a deadlock; that a cleanup failing on every
attempt keeps the document's chunks, is abandoned after `MAX_RETRIES`
attempts, and once re-opened removes the chunks and queues their vector
deletes; that a final claim whose lease expired is abandoned by the queue
rescuer with the `last_error` described above; and that re-opening an
abandoned intent of an earlier Revision, stamped later than the Head's, closes
it as superseded and leaves the Head to be indexed.

Use a disposable PostgreSQL instance capable of creating test databases; these
suites create and remove their own databases. `REQUIRE_REAL_PG=1` makes the native
document suite fail if PostgreSQL is unavailable, and `REQUIRE_REAL_REDIS=1`
requires the Redis integration input. Never point these suites at a shared
application database as a general fixture target.

For an explicitly authorized deployed endpoint, use the [live acceptance
driver](../../scripts/e2e/README.md). It checks REST writes and actual Redis
delivery with a disposable Vault.
