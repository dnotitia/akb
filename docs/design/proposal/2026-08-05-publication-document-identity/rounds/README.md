# Rounds

This item was designed in two parts, each with its own correction rounds, plus
one round that reopened a decision after implementation.

- **Part A — the schema.** Established the composite foreign key over a simple
  one. A first pass assumed that a foreign key on `document_id` alone would
  bring vault agreement with it; it does not, and the correction is why
  `documents` gained `UNIQUE (id, vault_id)` as a reference target.

  Also settled the migration's internal order against the classical
  expand/backfill/constrain shape: the constraint is added **before** the
  backfill, while every value is still NULL, so the database checks each
  binding as it is written rather than certifying a whole batch at the end.
  Constrain-last would have reproduced the failure the item exists to remove.

  `NOT VALID` was evaluated and rejected — it takes the same lock and skips
  only a scan of a table with one row per published link, at the cost of a
  constraint that is enforced but unverified. A plain index build was chosen
  over `CONCURRENTLY` because a cancelled concurrent build leaves an invalid
  index that `IF NOT EXISTS` then steps over silently; the migration adopts a
  valid index built out of band instead, which is the escape hatch if the
  reference table is ever too large to index inline.

- **Part B — the read and write paths.** Scoped to carrying the resolved id
  from resolve to insert, and to moving the reverse lookups onto it.

- **Correction round — the forward path.** A review pointed out that the item
  had moved the *reverse* lookups onto the identity and left the *forward*
  ones on the path, which is backwards with respect to consequence: the reverse
  direction answers a question an author asks about their own document, the
  forward direction decides what a link stands for. It also observed that
  refusing a disagreeing pair at write time establishes agreement at creation
  and nothing afterwards — what keeps them agreeing is a single statement in
  the move path, which is the same "one place must remember" shape the item
  exists to abolish, and a foreign key cannot help while it constrains a column
  no read path reads. The forward paths moved.

- **Correction round — the body read.** Immediately after, an adversarial
  review observed that the change had not gone far enough: choosing a row and
  reading its content are separate steps, and an identity used only for the
  first is not carried by the second. The codebase had already named and fixed
  this class on the authenticated read path; the public one had not been. The
  same pin was applied.

- **Cleanup round.** Four review angles over the finished branch produced a
  sixth unguarded reference in `init.sql` to a column that arrives with a
  migration (found by reading migrations, not by the replay technique
  documented alongside the guards, which is structurally blind to columns older
  than that file's recorded history), and a set of smaller simplifications.
  Several proposed simplifications were declined and the reasons recorded:
  collapsing the publish-time refusals into one message would cost the only
  branch a real user reaches its actionable wording, and removing the
  "changed underfoot" refusal category would make a log conflate two materially
  different stories.
