# Vault isolation via PostgreSQL ACL — Design

**Status**: draft, awaiting review
**Branch**: `feat/pg-native-rbac`
**Started**: 2026-05-21

## Statement

AKB's vault boundary is enforced by **PostgreSQL ACL**, not by
application-side identifier filters. A user issuing arbitrary SQL via
`akb_sql` runs the query under a per-user PG role whose membership
in vault group roles determines what they can touch. Cross-vault
reads/writes return `42501` directly from PG.

The application coordinates lifecycle (role creation, grant/revoke)
but does not gate execution. The grammar of "who can do what" lives in
PostgreSQL catalogs.

## Why PostgreSQL ACL

`akb_sql` is the only surface in AKB that accepts arbitrary user
SQL. Every other tool (`akb_search`, `akb_put`, `akb_browse`, …) sends
application-authored parameterized queries — there is no user-supplied
SQL to filter. So the security question reduces to: **how do we
enforce vault isolation on arbitrary SQL?**

Two answers exist:

1. Application-side identifier blocklist that inspects every SQL
   string before execution. This is a regex / parser / allow-list.
2. PostgreSQL roles + table-level GRANT. The application never
   inspects SQL for security; PG returns permission-denied for any
   reference to a table the caller's role lacks privilege on.

Answer 2 is correct on first principles:

- The validity question is "does this role have access to this table"
  — exactly the question PG was built to answer.
- The blocklist approach has unbounded bypass surface (new system
  catalogs across PG versions, dollar-quoted strings, `SET ROLE`,
  `COPY ... TO PROGRAM`, comment injection, …) and any single miss
  exfiltrates cross-tenant data.
- The PG approach has a bounded surface (PG's ACL implementation
  itself), which is part of the platform's trusted base.

## Model

### Roles

```
akbuser                        application connection role; superuser
                               for DDL operations during normal lifecycle.

akb_user_<uid>                 one per AKB user, NOLOGIN-friendly but
                               LOGIN-capable for completeness. Created
                               on signup, dropped on user delete.
                               No direct table privileges of its own;
                               inherits everything from group memberships.

akb_vault_<vid>_reader         per vault, per scope. Created when
akb_vault_<vid>_writer         the vault is created. Holds the actual
akb_vault_<vid>_admin          table-level GRANTs.

                               reader  → SELECT
                               writer  → SELECT, INSERT, UPDATE, DELETE
                               admin   → all of the above + TRUNCATE
```

### Membership

| AKB grant | PG GRANT |
|---|---|
| `vault_access(user, vault, scope='reader')` | `GRANT akb_vault_<vid>_reader TO akb_user_<uid>` |
| `vault_access(user, vault, scope='writer')` | `GRANT akb_vault_<vid>_writer TO akb_user_<uid>` |
| `vault_access(user, vault, scope='admin')` | `GRANT akb_vault_<vid>_admin TO akb_user_<uid>` |
| vault owner | `GRANT akb_vault_<vid>_admin TO akb_user_<owner_uid>` |

`vault_access` rows in the AKB system DB are the **catalog**; PG role
memberships are derived state. The two are kept in sync by lifecycle
hooks at write time and by a reconciler at startup.

### Tables

User-created tables stay at their current physical location:
`public.vt_<vault_name>__<table_name>`. **No data migration.**

Each table is owned by `akbuser` (today's default) and has GRANT
entries for the three group roles of its vault:

```sql
GRANT SELECT                          ON vt_<v>__<t> TO akb_vault_<vid>_reader;
GRANT SELECT, INSERT, UPDATE, DELETE  ON vt_<v>__<t> TO akb_vault_<vid>_writer;
GRANT ALL                             ON vt_<v>__<t> TO akb_vault_<vid>_admin;
```

System tables (`users`, `vaults`, `vault_access`, `tokens`,
`documents`, `chunks`, `bm25_*`, `edges`, `file_objects`, …) have
**no GRANT to any akb_user_* or akb_vault_*** role. They are accessible
only to `akbuser` (the application connection role). Non-superuser
`akb_user_*` roles get permission-denied if they attempt to read
these tables.

## Code shape

Two new components, plus thin wiring in existing services.

### `RoleSync` — lifecycle hook + reconciler

`backend/app/services/role_sync.py`. Single class with the methods:

```python
class RoleSync:
    def __init__(self, pool): ...

    # Lifecycle hooks — called by services after their system DB write.
    # Best-effort: failures are logged and audited; reconciler covers drift.
    async def on_user_create(self, user_id) -> None
    async def on_user_delete(self, user_id) -> None
    async def on_vault_create(self, vault_id, owner_user_id) -> None
    async def on_vault_delete(self, vault_id) -> None
    async def on_grant(self, vault_id, user_id, scope) -> None
    async def on_revoke(self, vault_id, user_id, scope) -> None
    async def on_ownership_transfer(self, vault_id, old_uid, new_uid) -> None
    async def on_table_create(self, vault_id, table_pg_name) -> None
    async def on_table_drop(self, vault_id, table_pg_name) -> None

    # Reconciler — runs at backend startup and on /admin/reconcile.
    # Reads users + vaults + vault_access + vault_tables from system DB,
    # emits CREATE/GRANT statements idempotently.
    async def reconcile_from_catalog(self) -> ReconcileReport
```

All methods are idempotent (`CREATE ROLE IF NOT EXISTS`,
`GRANT ... IF NOT GRANTED`, etc., via `DO $$ BEGIN ... EXCEPTION WHEN
duplicate_object THEN NULL; END $$` blocks where IF NOT EXISTS isn't
available).

### `UserSqlExecutor` — sole entry point for `akb_sql`

`backend/app/services/user_sql_executor.py`. One method:

```python
class UserSqlExecutor:
    def __init__(self, pool): ...

    async def execute(self, *, user_id, vault_id, sql) -> list[dict]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"SET LOCAL ROLE akb_user_{user_id}; "
                    f"SET LOCAL search_path = public"
                )
                return await conn.fetch(sql)
```

`SET LOCAL` is transaction-scoped — automatically reset on
commit/rollback. Safe under any PgBouncer mode.

`search_path` stays on `public` because tables are physically in
`public` (overlay design). The vault boundary is enforced by the
table-level GRANT, not by schema namespacing.

### Wiring in existing services

| File | Change |
|---|---|
| `app/services/auth_service.py` `register()` | After `INSERT users`, call `role_sync.on_user_create(user_id)` |
| `app/services/auth_service.py` user delete | After cascade, call `role_sync.on_user_delete(user_id)` |
| `app/services/document_service.py` `create_vault()` | After `INSERT vaults`, call `role_sync.on_vault_create(vault_id, owner_id)` |
| `app/services/access_service.py` `grant_access()` | After `INSERT vault_access`, call `role_sync.on_grant(vault_id, user_id, scope)` |
| `app/services/access_service.py` `revoke_access()` | After `DELETE vault_access`, call `role_sync.on_revoke(vault_id, user_id, scope)` |
| `app/services/access_service.py` `transfer_ownership()` | After mutation, call `role_sync.on_ownership_transfer(...)` |
| `app/services/access_service.py` `delete_vault()` | After cascade, call `role_sync.on_vault_delete(vault_id)` |
| `app/services/table_service.py` `create_table()` | After `CREATE TABLE`, call `role_sync.on_table_create(vault_id, pg_name)` |
| `app/services/table_service.py` `drop_table()` | After `DROP TABLE`, call `role_sync.on_table_drop(vault_id, pg_name)` |
| `app/services/table_service.py` `execute_sql()` | Delegate to `UserSqlExecutor.execute(user_id=..., vault_id=..., sql=...)`. Remove `allowed_pg_tables` param, `_validate_sql_surface` call, read-only-tx forcing. Keep `rewrite_table_names` for UX. |
| `app/main.py` startup (lifespan) | After pool init, call `role_sync.reconcile_from_catalog()` |

### Code removed

Bookkeeping for the application-side sandbox is no longer the
authoritative gate; it disappears:

| File | Removed |
|---|---|
| `app/services/table_service.py` | `_validate_sql_surface()` entirely; `_FORBIDDEN_TOKEN_RE` constant; `allowed_pg_tables` parameter on `execute_sql()`, `_enrich_undefined_error()`, and callers |
| `app/services/table_service.py` | Read-only-tx forcing (`SET TRANSACTION READ ONLY`) — reader role has no INSERT/UPDATE/DELETE grant, so PG enforces |
| `app/services/table_service.py` | First-keyword DML check — keep a minimal "must be DML" pre-flight for friendly errors; this is UX, not security |

Estimated net diff: **+420 / -120 = +300 LOC**.

## Lifecycle semantics and failure handling

Lifecycle hooks are best-effort: a hook failure is logged + audited
but does **not** roll back the system DB write. The reconciler
catches up at next startup or on-demand via `/admin/reconcile`.

This is the right trade-off because:

- The catalog (`users`, `vaults`, `vault_access`, `vault_tables`) is
  the **authoritative source**. PG role state is derived; it can
  always be reconstructed.
- Cross-DB-style 2PC is unnecessary because both writes happen on
  the same PG instance, same connection pool — but they are separate
  `transaction()` blocks (the system DB write is in its own tx
  with row locks; the `GRANT/REVOKE` runs separately).
- A drift between catalog and PG roles is correctness-equivalent to
  "the grant hasn't taken effect yet." The reconciler converges
  state in milliseconds.

Drift scenarios and their resolution:

| Drift | Result | Recovery |
|---|---|---|
| `vault_access` row exists, PG GRANT missing | User's `akb_sql` returns 42501 even though catalog says they have access | Reconciler emits the missing GRANT on next run |
| PG GRANT exists, `vault_access` row missing | User can run `akb_sql` even though catalog says no | Reconciler drops the orphan GRANT |
| `akb_user_<uid>` exists, no `users` row | Orphan role | Reconciler drops |
| `users` row exists, no `akb_user_<uid>` | User's `akb_sql` fails with "role does not exist" | Reconciler creates |

The reconciler runs:
- At backend startup (lifespan).
- On `POST /admin/reconcile-roles` (admin-only).
- Optionally on a slow timer (e.g. hourly) if operations want it.

## `akb_sql` after the change

```python
async def execute_sql(self, *, user_id, vault_id, sql):
    await check_vault_access(user_id, vault_id)   # friendly 403 still
    sql = rewrite_table_names(sql, vault_id)      # UX: "<vault>:<t>" → "vt_v__t"
    sql = check_first_keyword_is_dml(sql)         # UX: friendlier than PG's syntax error
    return await self.user_sql_executor.execute(
        user_id=user_id, vault_id=vault_id, sql=sql,
    )
```

That's the whole gate. The PG-side enforcement happens inside
`UserSqlExecutor.execute`. The application no longer enumerates
allowed table names or scans for forbidden identifiers.

If the user attempts `SELECT * FROM vt_<other>__<t>`:
- PG resolves the identifier in `public`.
- ACL check: `akb_user_<uid>` is not a member of
  `akb_vault_<other>_reader` (or writer/admin).
- PG returns `42501 permission denied for table vt_<other>__<t>`.
- `UserSqlExecutor` propagates the error to the MCP caller as a
  structured `permission_denied` response, with the original PG
  error text included.

## Test plan

New file: `backend/tests/test_pg_rbac_e2e.sh`. Categories:

### Positive (3)
- Owner of vault A can CREATE TABLE, INSERT, SELECT, DELETE via `akb_sql`.
- Reader of vault B (granted by owner) can SELECT but not INSERT.
- Admin of vault C can CREATE INDEX (via dedicated tools — `akb_sql` itself is DML only).

### Negative — must be PG-side 42501, not app-side 403 (9)
For each: assert the error originates from PG (response contains
`permission_denied` / PG error code `42501` / SQLSTATE), not from
application validation.

1. Reader of A → `SELECT * FROM vt_<B>__<t>` directly.
2. Reader of A → `SELECT * FROM vt_<B>__<t> WHERE id = 1` (with predicate).
3. Reader of A → `WITH cte AS (SELECT * FROM vt_<B>__<t>) SELECT * FROM cte`.
4. Reader of A → `SET ROLE akb_vault_<B>_reader`.
5. Reader of A → `SELECT * FROM users` (system table).
6. Reader of A → `SELECT * FROM pg_authid` (PG superuser-only).
7. Reader of A → `COPY vt_<A>__<t> TO PROGRAM 'whoami'` (superuser-only).
8. Writer of A → `DROP TABLE vt_<A>__<t>` (admin-only).
9. Writer of A → `INSERT INTO vt_<A>__<t> ...; SELECT * FROM vt_<B>__<t>;` (multi-statement leak attempt).

### Lifecycle (4)
1. Concurrent `grant` and `revoke` on the same (user, vault): PG role
   state converges to whichever wins the row lock in the catalog.
2. User delete with active grants: `REVOKE` chain runs, then `DROP
   ROLE`. No PG error. Reconciler finds no orphans.
3. Vault delete with active members: similar.
4. **Drift recovery**: manually `DROP ROLE akb_user_X`, then run
   `/admin/reconcile-roles`. Role is recreated with all grants from
   `vault_access`. User's `akb_sql` works again.

## Acceptance criteria

- [ ] All 8 existing e2e suites pass unchanged
  (`test_mcp_e2e.sh`, `test_edit_e2e.sh`, `test_stdio_files_e2e.sh`,
  `test_put_file_param_e2e.sh`, `test_security_edge_e2e.sh`,
  `test_graph_replace_e2e.sh`, `test_defensive_e2e.sh`,
  `test_probes_e2e.sh`).
- [ ] `test_pg_rbac_e2e.sh` passes — 3 positive + 9 negative + 4
  lifecycle = 16 cases.
- [ ] `_validate_sql_surface`, `allowed_pg_tables` parameter,
  `_FORBIDDEN_TOKEN_RE`, and read-only-tx forcing are removed.
- [ ] Reconciler runs at startup and produces a clean report on a
  fresh database (everything created from catalog) and on a populated
  database (no spurious creates/drops).
- [ ] README + CLAUDE.md reflect PG-native RBAC as the security
  model. The earlier "akb_sql sandbox" reference is replaced.

## Out of scope

- `external_pg` mode (user tables on a separate PG instance). Future
  hardening when operational need arises; this design's lifecycle
  hooks + RoleSync are forward-compatible.
- Per-user PG roles for non-`akb_sql` paths (search, browse, doc
  CRUD). Those continue to use application-level ACL because they
  don't accept arbitrary user SQL. Threat model is "developer omits
  vault filter," caught by code review and e2e.
- The pgvector bootstrap gap (discovered 2026-05-21) and the
  concurrency audit findings (2026-05-20). Each is its own work
  stream.

## Sequencing

One branch, one merge. Internal commits ordered:

1. Design doc.
2. `RoleSync` skeleton + reconciler + unit tests.
3. Lifecycle wiring in `auth_service`, `access_service`,
   `document_service.create_vault`, `table_service` (create/drop).
4. `UserSqlExecutor` + `table_service.execute_sql` delegation.
5. Remove obsolete sandbox code.
6. `test_pg_rbac_e2e.sh`.
7. Local docker-compose verification of all e2e suites.
8. README + CLAUDE.md + deploy doc updates.
