---
status: proposal
stage: design
created: 2026-08-05
updated: 2026-08-05
---

# Publications carry a document identity again

## What this is

Migration 022 replaced `publications.document_id` and `publications.file_id`
with a single canonical `resource_uri`, and in doing so dropped the two
`ON DELETE CASCADE` foreign keys those columns carried. Since `documents` is
keyed `UNIQUE(vault_id, path)`, a path can be freed and reoccupied — so a
publication addressed by a path names a location, not a document.

The item that preceded this one made document-publication cleanup reachable
from every delete path and bound resolution to the publication's own vault.
Every defence it added is Python-level. Its Direction section named restoring
an identity anchor as a separate piece of work with its own rollout; this is
that work.

This item restores the identity column and moves the paths that decide *which
document a publication means* onto it. Files are deliberately out of scope —
see Boundaries.

## What changes

1. **A document identity column with a composite foreign key.** Migration 053
   adds `publications.document_id`, a `UNIQUE (id, vault_id)` constraint on
   `documents` to serve as the reference target, and

   ```sql
   FOREIGN KEY (document_id, vault_id) REFERENCES documents(id, vault_id)
     ON DELETE CASCADE
   ```

   The composite form is the point. `FOREIGN KEY (document_id) REFERENCES
   documents(id)` would guarantee only that the document exists — a publication
   in one vault pointing at a document in another satisfies it. The composite
   form makes vault agreement a structural property rather than one more rule
   the code has to remember, which is the reason for the whole item.

2. **The publisher keeps the identity it resolved.** Publishing resolves a
   caller-supplied reference to a document and then inserts a row. Those are
   two steps, and the identity has to survive between them: the id the resolve
   step produces is now carried through, re-verified under the row lock the
   insert already took, and stored.

3. **Resolution reads the identity.** Both directions moved — document → slug
   (the public-slug lookups) and slug → document (public resolution and the
   oEmbed title lookup). The second is the one that decides which document a
   link stands for, so it is the one the identity column exists to serve.

4. **The body is read at the document's own commit.** Selecting a row and
   reading its bytes are two steps as well, and the same reasoning applies —
   an identity established by the first is not worth much if the second does
   not use it. The read is pinned to `documents.current_commit`, the same fix
   already applied to the authenticated read path.

5. **`resource_uri` stays, demoted.** `table_query` publications have no
   resource, so the column has to exist, and the API response already exposes
   it. For document publications it is now derived; `document_id` holds the
   binding.

## Backfill

Existing rows have only their URI to go on. Binding them to "whatever document
that path points at now" would let the new foreign key permanently certify a
wrong binding, and that is not reversible. The migration binds only what is
unambiguous and leaves everything else NULL:

- the URI must parse and name this publication's own vault;
- exactly one document must occupy that path in that vault;
- the document must not post-date the publication.

Every refusal is counted and logged by category. No row is deleted — a
publication row is the only record of its slug, creator, restrictions and view
count.

The conditions are filters, not proof. They remove some wrong answers and
cannot remove all of them, so a bound row means self-consistent, not verified.
Establishing that a given deployment's pre-existing rows are correct is not
something a migration can do; it is a separate step, and it is not described
here.

## Consequences

**`document_id` cannot be `NOT NULL` yet.** "Every document publication points
at a document" is enforced in code, not in the schema, for as long as one
backfilled NULL remains. Three code sites and two tests exist only for that
population and become unreachable when it empties. Nothing currently detects
that day — a follow-up should count the population and install the CHECK when
it reaches zero, so the cleanup has one trigger rather than several independent
judgment calls.

**The migration cannot vouch for what it binds.** It is written to be safe on a
database it knows nothing about, and safe means it declines to guess rather
than that its answers are confirmed. Confirming them is an operational step
that belongs to whoever runs it.

**Two guards from the previous item narrowed.** The orphan-write guard and the
explicit cleanup in the delete chokepoint now protect only unbound rows; the
foreign key covers the rest. Their tests had to clear `document_id` to
reproduce the state at all, which is the honest reproduction rather than a
weakened assertion.

## Boundaries

**Files are unchanged.** `vault_files` ids are not reusable the way paths are,
so an orphaned file publication fails closed rather than resolving onto a
different resource — the defect shape does not carry over. The cost of the
asymmetry sits on the cleanup side instead, where a canonical URI is
reconstructed from three mutable inputs and matched as text; the open backlog
items about file publication URIs and cleanup normalisation are instances of
that. A `file_id` column would collapse them and would need no judgment in its
backfill, which makes it the cheaper half of the same idea — but it is a second
schema change and belongs in its own item.

**`resource_uri` is not removed** and the move-time URI rewrite stays. Removing
either is a contract change.

## Rollout

A schema change with a backfill, so it has ordering constraints and a
verification step that a code change would not. Migration ordering, lock
behaviour and the out-of-band index build are recorded in the migration module
itself; the sequencing and verification a given deployment needs are an
operational matter and are not described here.

Detailed reasoning, review transcripts and the operational record are held in
the team's internal notes rather than in this repository.
