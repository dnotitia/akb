# PM Decision — 2026-08-03

## Question put to the PM

The `akb_todo` ghost references and the account-deletion bug share one root
cause (PR #43's unfinished cleanup), so the scope decision had to come first:
B, C, or C′. C and C′ collapse both items into a single PR.

## Decision

**C′ — archive, then remove the whole stack.** One PR covering: dump the rows,
drop the table via migration, delete `todo_service.py`, remove the four query
sites, strip the three ghost-advertisement surfaces, and add the closure
guards.

**Plus: re-measure the deployment read-only before starting.** The PM asked for
the earlier figures to be re-taken rather than trusted.

## Re-measurement result (2026-08-03, `SELECT` only, context verified)

Identical to the earlier measurement in every field: same row count, same
newest-row date, same set of blocked accounts with the same per-account row
counts, and `assignee_id` / `created_by` still `NOT NULL` on the live schema.
Six further days of zero growth reinforce the "no writer exists" argument the
decision rests on.

Deployment-specific figures (counts, dates, affected accounts, vault
breakdown) are recorded in the internal `gnu-weekly` vault. They are
deliberately kept out of this repository, which is public.

## Deviations from the plan as approved

1. **Four query sites, not two.** The report named
   `access_service.py:1604` and `:1684-1685`. The guard test also caught
   `document_service.py` — a `COUNT(*) FROM todos` in the create_vault
   rollback's foreign-row safety check and a `DELETE FROM todos` in its purge.
   Both removed.

2. **`init.sql` had to change too.** Not in the original scope. `init_db()`
   runs `init.sql` before migrations on every boot, so leaving
   `CREATE TABLE IF NOT EXISTS todos` would have recreated an empty table
   after 050 was already recorded as applied. A test pins this.

3. **`NO_OP` error constant removed.** `todo_service` was its last user; the
   repo's own `test_catalogue_has_no_orphan_constants` then failed. Removing
   it is what that test asks for. Nothing outside the backend references it.

4. **Guards extended to the shipped plugin skills.** Originally scoped to
   `instructions.py` + `help.py`. The skills are distributed through
   `marketplace.json`, so a ghost tool name there escapes the repo entirely.

5. **Archive taken twice, by design.** An out-of-band `pg_dump` before the
   change, and `todos_archive` created by the migration itself. The in-migration
   archive is the durable one: it applies to every environment and cannot be
   forgotten. The out-of-band dump is held outside this repository.

## Still open for the PM

- **`todos_archive` retention.** Kept indefinitely with no reader. Drop it
  after a window, or leave removal to the operator?
- **Rollout.** Migration 050 runs at backend startup. It takes an
  `ACCESS EXCLUSIVE` lock on a table nothing reads, under the standard 5s
  `lock_timeout` + retry, so no special ordering is required — but the deploy
  is what actually unblocks the affected accounts.
