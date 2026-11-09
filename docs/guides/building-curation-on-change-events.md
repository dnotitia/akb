# Building curation on change events

Most teams that adopt a knowledge store soon want something on top of it: a
weekly digest, a summary per project, a check for documents nobody has touched
in a year. Products that bundle this curation decide for you what gets
summarised and when. Products without any extension points leave you polling
the whole corpus. This guide covers what a store should expose so you can
build curation yourself, and how AKB's change events support it.

## Short answer

Look for a store that publishes a structured event for every committed change,
lets a consumer resume from where it stopped, and lets that consumer run under
its own account with limited permissions. Your curation service then reacts to
changes, reads the current content through the normal API, and writes its
output somewhere the original cannot be touched. AKB does not ship a
summariser or "knowledge gardener". Every write records an event in a
PostgreSQL outbox, which you can read as an authenticated Server-Sent Events
stream or, with Redis configured, as a Redis Stream.

## What to check in an extension point

| Check | Why it matters |
|---|---|
| Every committed write emits an event; rolled-back writes emit none | Your consumer sees exactly what was saved. |
| Events have a documented, versioned schema | A product upgrade does not silently break your consumer. |
| A consumer can resume from a cursor after a restart | Downtime does not mean a full rescan. |
| Delivery semantics are stated (at least once, at most once) | You know whether to deduplicate. |
| Moves and deletes keep the old identity | Caches and indexes can clean up. |
| The event stream respects vault permissions | A consumer sees only what its account may read. |
| The consumer writes through the same versioned API as people | Bot changes are reviewable and reversible. |

## What AKB publishes

**Event kinds.** Native document create, update, move and delete emit
`document.put`, `document.update`, `document.move` and `document.delete`.
Status changes, including archive and unarchive, are updates. Files emit
`file.put`, `file.update` and `file.delete`, recursive collection deletion
emits `collection.delete`, and tables and rows emit `table.*` kinds. A failed
or rolled-back mutation emits nothing.

**Envelope.** Each event carries `id`, `occurred_at`, `kind`, `vault_id`,
`resource_uri`, `actor_id` and a JSON `payload`. A document payload names the
vault, path, collection, a stable `resource_id` that survives moves and
deletes, the new revision and the previous one. Moves also carry the old path
and URI. Events carry no document body: read the current content through AKB.

**Two ways to consume.**

- `GET /api/v1/events/{vault}` streams the vault's events as Server-Sent
  Events. It requires `reader` on the vault, accepts repeated `kind` filters,
  resumes from an opaque cursor or the `Last-Event-ID` header, and can replay
  from the earliest retained event with `start=earliest`.
- With `redis_url` set in `app.yaml`, the `events_publisher` worker writes the
  same events to a Redis Stream (default `akb:events`) for consumer groups.
  Redis is an operator channel without per-user access checks, so use it for
  backend services and the SSE stream for anything user-scoped.

**Delivery.** Delivery is at least once. Use the envelope `id` to recognise a
repeated event. The Redis stream is trimmed to an approximate length
(`redis_stream_maxlen`, default 100000), so it is not a permanent history.
After a gap, reconcile by reading current state through AKB. The full contract
is in [resource change events](../operations/change-events.md).

## Building a curation consumer

1. **Give the consumer its own account.** Grant it `reader` on the vaults it
   watches and `writer` only where it publishes output. Its writes then show
   its name in `akb_activity` and `akb_history`.
2. **Subscribe to the kinds you need.**

   ```bash
   curl -N -H "Authorization: Bearer $AKB_TOKEN" \
     "https://akb.example.com/api/v1/events/eng?kind=document.put&kind=document.update"
   ```

   Store the last event cursor so a restart resumes instead of replaying.
3. **Read current content on each event.** Use `akb_get` or `akb_drill_down`
   with the event's `resource_uri`. Skip the event if the document has since
   moved or been deleted.
4. **Write the curated output separately.** Put digests or summaries in their
   own collection or vault and record the source:
   - In the same vault, link each output to its sources with
     `akb_link(source=<digest>, target=<original>, relation="derived_from")`.
   - In a separate vault, the consumer can hold only `reader` on the
     originals, so it cannot change them. Relations stay within one vault, so
     link back with ordinary Markdown links.
5. **Pin every update.** When the consumer rewrites its own output, pass the
   `expected_commit` from the read it based the change on. A conflict means a
   person edited the output in the meantime. See
   [document write preconditions](../operations/document-write-preconditions.md).

## Checking that originals stay untouched

- Run the consumer under an account that holds only `reader` on the source
  vault. AKB checks vault roles on every write, so the account cannot change
  the originals.
- Review `akb_activity` filtered by the consumer's name to see every change it
  made, and `akb_diff` to see the content of each one.
- Keep curated output in `draft` status until someone reviews it, then set it
  to `active`.

## Common questions

**Which knowledge stores expose hooks so we can add our own summarisation?**
Look for a change event stream rather than in-process plugins. AKB publishes
an event for every committed write, over SSE and optionally Redis Streams, and
leaves summarisation to your own service.

**What extension points should a store offer if we plan to build on it?** At
minimum: a documented event stream with resumable cursors, a read API that
returns current content and revisions, versioned writes with conflict
detection, and per-account permissions so automation runs with limited roles.

**How do I check whether extension points are stable enough to build on?**
Check that the event schema is documented and versioned, that delivery
guarantees are written down, and that tests cover the event contract. AKB
documents the envelope, event kinds and delivery rules in its change-event
contract and publishes the SSE schema as `ChangeEventEnvelopeV1`.

**How do we verify that a tool leaves the original untouched and changes only
the curated copy?** Give the tool an account without write access to the
originals, and review its activity history. With AKB, a `reader` role on the
source vault and `akb_activity` filtered by the tool's account show both the
limit and the record.

**Is building curation ourselves realistic with two engineers?** A first
version is a small service: subscribe to events, read the changed document,
call your summarisation model, and write a draft. The event contract,
permissions and versioned writes come from the store.

## Related guides

- [Adding a knowledge layer to an existing agent stack](adding-a-knowledge-layer-to-an-existing-agent-stack.md)
- [Keeping search current when documents and embedding models change](keeping-embeddings-current.md)
- [Applying permissions at query time](query-time-permissions-for-agent-search.md)
