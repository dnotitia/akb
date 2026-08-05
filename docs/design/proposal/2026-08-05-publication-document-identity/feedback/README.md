# Feedback

Reviews conducted on this item, in order:

- **Per-change reviews during implementation.** Each of the two parts went
  through a specification-compliance review and then a code-quality review
  before the next part started. Both produced real corrections.

  The specification review of the schema work verified its claims by mutation
  rather than by reading — replacing the composite foreign key with a simple
  one, weakening the cascade, removing each guard in turn — and confirmed that
  each mutation failed the test that was supposed to catch it. Two mutations
  survived, which is how the item learned that its per-category backfill
  counters were untested and that one of its two vault-name checks was pinned
  only jointly with the other.

  The quality review of the same work found that a fix applied to one of two
  sibling catalog guards had not been applied to the other, and reproduced all
  three of the resulting misbehaviours — including a case where the migration
  dropped an index on an unrelated table, logged a warning asserting it was
  something else, and reported success.

- **Adversarial external review** on each part. On the schema work it found an
  unguarded index in `init.sql` that would abort initialisation on any database
  predating the migration — and because that file runs in full on every boot,
  before migrations, the migration that would have added the column never runs.
  An unrecoverable startup failure, introduced by this item and caught before
  it left the branch. On the read-path work it found the unpinned body read
  described in the rounds notes.

- **Quality pass over the finished branch**, along four independent angles
  (reuse, simplification, efficiency, depth). The depth angle produced the
  forward-path correction. The efficiency angle measured rather than reasoned:
  it confirmed the fallback query still uses the new partial index under a
  forced generic plan, which is what matters given every statement is prepared,
  and it found one redundant lookup this item had introduced.

- **Code review over the finished branch**, along five angles (guideline
  compliance, defect scan, historical context, prior review feedback, comment
  fidelity). One finding: this item's own design record was missing, which is
  what this folder now answers.

  The historical angle is worth recording. It established that the migration
  ledger post-dates the migration whose guard this item changes, so that change
  is inert on any previously-booted database and matters only on a fresh one —
  where the previous guard would have destroyed the column this item adds, one
  migration before it was re-created. It also traced the body-read fix to its
  original and confirmed the reapplication matches rather than diverges.

Verification is recorded in the branch: the cascade and the vault refusal were
each demonstrated through raw SQL, bypassing the application entirely, which is
the only way to show a database-enforced guarantee is actually enforced by the
database. The initialisation hazard was reproduced by hand — failure first,
then recovery, then the full boot path — rather than accepted on the strength
of a passing test.

Detailed review transcripts are held in the team's internal notes rather than
in this repository.
