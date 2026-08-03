# Codex Adversarial Review — 2026-08-03

Run against the branch with `codex exec -s read-only`, asked to attack the
migration's correctness, the mixed-version rollout, the `create_vault`
rollback edit, guard-test false negatives, and the `NO_OP` removal.

Verdict: no critical findings; two high-severity migration defects, one
medium rollback defect, and substantive guard-test gaps. All accepted findings
were verified against the code before being acted on.

## Accepted and fixed

**H1 — a pre-existing `todos_archive` was trusted, then the source dropped.**
The original code skipped the archive step if `todos_archive` already existed
and dropped `todos` anyway, justifying it as "a partial earlier run". That
justification was wrong: the CTAS and the DROP commit in one transaction, so
this migration can never leave the archive behind with `todos` still present.
Both present therefore means the archive came from somewhere else — a manual
snapshot, a restored dump, a hand-run of an earlier revision — and may not
contain the live rows. Now **fails closed** with an explanatory error.

**H2 — CTAS does not lock out concurrent writers.** `CREATE TABLE … AS SELECT`
takes only `ACCESS SHARE` on the source, which does not block DML. A row
committed between the snapshot and the `DROP` would be destroyed without ever
reaching the archive. The claim that "no writer remains" was too strong:
`akb_sql` permits INSERT/UPDATE/DELETE (`table_service.py`) and an *unscoped
admin* skips the `SET LOCAL ROLE` narrowing entirely, running as the
connection default role (`user_sql_executor.py`); direct operator SQL is
another path. Now takes `LOCK TABLE public.todos IN ACCESS EXCLUSIVE MODE`
inside the transaction, with the existence checks moved inside it too (they
were a TOCTOU window). All references schema-qualified.

**M4 — guard-test false negatives.** Fixed:
- `\bakb_[a-z_]+` matched the `akb_search` *prefix* of a ghost named
  `akb_search2` and passed it as known. Now `\bakb_[a-z_][a-z0-9_]*\b`.
- The root-table parser keyed on the literal header `| Tools |` and silently
  matched nothing if it were reworded — the test would have passed while
  checking nothing. Both table tests now assert rows were found.
- Tool tokens written as `` `put` `` or `put()`, and topics written
  `**topic**`, were skipped by the identifier filters. Both now normalise.
- The plugin scan read only `.md`/`.json`; frontmatter is YAML and manifests
  can be TOML. Extended to `.yaml`/`.yml`/`.toml`.
- The SQL-removal regex missed `public.todos`, `"todos"`, `TRUNCATE todos`,
  and `TABLE ONLY todos`. Extended. Multi-line SQL remains out of reach for a
  line-based scan; that limitation is now stated in the module docstring.

**M4 (cont.) — an uncovered live ghost outside the guarded surfaces.**
`agents/runtime.py` told every model in its default system prompt that it has
tools "for … creating todos", and its usage example was
`agent.run("Summarise my open todos")`. Both fixed. Worth noting the guard
could not have caught the system prompt: it is ordinary prose ("todos"), not a
tool identifier, and lives outside `mcp_server/` and `plugins/`. Capability
claims in prose remain unguarded — see open questions.

**L5 — `CASCADE` was unnecessary and its comment was factually wrong.**
PostgreSQL drops a table's own indexes and constraints without `CASCADE`;
`CASCADE` exists to remove *external* dependents. Now a plain
`DROP TABLE public.todos`, so an unexpected site-local dependent (a view, say)
fails loudly instead of being silently deleted. Pinned by a test.

**L6 — stale prose.** `access_service.py`, `access.py`, and
`document_service.py` docstrings still listed `todos` among the cascade
targets. `access.py` and `access_service.py` also still listed `sessions`,
dropped back in migration 031 — fixed in passing.

**Test gaps → `tests/test_migration_050_drop_todos_unit.py`.** A live-PG test
now pins the whole state matrix (neither table / source only / archive only /
both present), the archive's nullability and FK-freedom, the fail-closed path,
the RESTRICT-dependent path, and — the one no other test covered — the
account-deletion regression itself, asserted positively both before and after
the migration. Registered in the `pgvector e2e (live DB)` CI job so it does
not skip in both jobs.

## Confirmed unfounded

Codex independently reached the same conclusion as our own check on the
rollout question, and cleared three more:

- **Mixed-version rollout needs no fence.** The backend is `replicas: 1` with
  `strategy: Recreate` (`deploy/k8s/backend.yaml`), chosen because the
  `vaultdata` PVC is ReadWriteOnce; the internal overlay does not override
  either. The old pod is fully gone before the new one applies 050, so there
  is no window. (An external deployment using RollingUpdate or >1 replica
  *would* need a two-phase rollout — noted as an open question.)
- **The ledger cannot record a rolled-back migration.** `module.migrate()`
  must return before the ledger INSERT. The reverse — migration committed,
  process dies before the INSERT — is possible and safe: the next boot finds
  `todos` absent, no-ops, and records the ledger.
- **The `create_vault` rollback edit is correct.** `$1`/`$2`/`$3` still bind
  as before, `foreign_keys` matches the remaining aliases exactly, and the
  `any(...)` guard is intact.
- **Removing `NO_OP` breaks no client contract.** The TypeScript SDK and the
  OpenAPI contract treat error codes as opaque strings; nothing in the
  frontend or SDK branches on `no_op`.

## Accepted as documentation, not code

**M3 — an old-image rollback can permanently resurrect `todos`.** Real: deploy
new (050 drops, ledger records) → roll back to an image whose `init.sql` still
creates the table → redeploy; 050 is skipped and the empty table stays. A
once-only migration ledger cannot enforce a postcondition against an older
init file. Building expand/contract across two releases is disproportionate
here because the resurrected table is inert — no code reads or writes it, so
the account-deletion bug does not return. Now stated plainly as a
non-reversibility caveat in the migration docstring and the CHANGELOG.

## Open questions

- Do any deployments outside this repo override `Recreate` / `replicas: 1`?
  Those need the two-phase rollout (remove all query sites, drain, then drop).
- Is database rollback across a destructive migration officially supported?
  This PR assumes not, and says so.
- Should `todos_archive` anonymise user UUIDs when an account is deleted? The
  pre-change *intent* was to null those references; a frozen snapshot does
  not. Related to the retention question already open.
- Nothing guards capability claims written as prose rather than tool
  identifiers (the `agents/runtime.py` system prompt was exactly that). A
  guard for that is a different, harder problem than name closure.
