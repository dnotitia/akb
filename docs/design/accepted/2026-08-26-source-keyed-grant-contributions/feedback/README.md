# Feedback

Four corrections shaped this record. Two were self-corrections made before
acceptance, one was published after it, and the fourth came from the first
consumer running against a real deployment.

**The cost argument did not decide what it claimed to.** The proposal argued
for a materialized effective row from the eleven modules that read
`vault_access` in SQL. That defeats only the form of (B) where every reader
derives. It does not touch the stronger form — keep the name and the columns and
make `vault_access` a view — under which all eleven sites and `role_sync` work
with their text unchanged; nothing declares a foreign key against
`vault_access.id`, and the reads select only `vault_id`, `user_id` and `role`.
What the same measurement does separate is the **writes**, which the proposal
never counted: seven statements in two modules, all of which a view would turn
into rewrites or `INSTEAD OF` machinery. That is a real difference and a much
smaller one than the proposal implied. The decision rests on reversibility
instead: the contribution table is identical under both, so (A) can become the
view later while the reverse is a migration plus a backfill.

**§4.3 read literally described the administrator's revoke too.** Found while
writing the gates. Removing only the `direct` basis would let an administrator
revoke somebody who also holds a rule-driven basis, get a success, and leave the
access in place. The two are different operations; §8 records the resolution.

**A claim about the absence of a periodic reconciler was wrong.** The scope
round said `start_workers` has no periodic ticker, and used it as a ground for
leaving expiry out. `start_workers` also starts `role_sync_reconcile_loop`,
hourly by default and enabled in every deployment; the claim came from reading
part of `start_workers` and stopping. The conclusion survives on a weaker
ground — something notices within an hour, which is not what "expired" means for
a security control — and the correction strengthens the case for a materialized
row, because the layer below it is watched continuously rather than only on
request.

**The floor policy covered revocation and said nothing about downgrade.** Found
by the first rule-driven consumer, against a live deployment, after acceptance.
An administrator lowering somebody who also holds a stronger rule basis gets a
200 and no change, and the consumer reported that as in sync — a green tick
telling an administrator their decision took effect. The semantics are right and
the reporting was wrong, but the gap was in this record too: "an explicit human
revoke wins" was written as though it covered every explicit human decision, and
a downgrade is not a revoke. §8 now says so, along with why a ceiling is the
wrong repair and what is actually missing. It was invisible to every comparison
against the effective role, which is why no test found it and a person did.
