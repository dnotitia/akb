Not ready to ship. Two blockers remain.

1. **Blocker — write-only callers receive read-protected state.** The REST route and MCP handler establish only writer authority, then return the service result unchanged ([tables.py:267](/Users/kwoo2/Desktop/storage/akb/backend/app/api/routes/tables.py:267), [server.py:998](/Users/kwoo2/Desktop/storage/akb/backend/mcp_server/server.py:998)). The no-op exposes stored collection/URI/schema and mismatch information ([table_service.py:620](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:620)).

   This belongs at the REST/MCP authorization boundary. Compute a fail-closed `can_read_existing` from both:

   - credential scope: PAT `read`, and OAuth `akb:vault:read` when applicable;
   - `check_vault_access(..., required_role="reader")`.

   Then pass an explicit projection capability to the service or redact through one shared serializer. Do not pass raw scopes into the service. A caller lacking read authority should receive only:

   ```json
   {
     "kind": "table",
     "name": "issues",
     "created": false,
     "outcome": "already_exists"
   }
   ```

   Omit URI, vault, collection, columns, keys, indexes, `matches_request`, and `mismatches`; the latter two are themselves schema oracles. Shipping the current disclosure is a security blocker.

2. **Blocker — `matches_request` is materially incorrect.** Stored unique keys and indexes are parsed for output, but requested values are neither accepted nor compared ([table_service.py:586](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:586), [table_service.py:607](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:607)). Consequently, completely different requested constraints/indexes can return `matches_request=true`. Malformed `unique_keys`/`indexes` also bypass their validators on the no-op path; only columns are validated ([table_service.py:695](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:695)).

   Extract one shared create-spec canonicalizer used by both branches: normalize columns, validate references, resolve inline and explicit keys/indexes, reject duplicate metadata names, then compare all five promised fields. Also canonicalize equivalent omissions such as `required` absent versus `false` if “canonical equality” is intended. Add explicit divergent/invalid key and index tests.

3. **The advisory lock is correct under `READ COMMITTED`, with one hardening gap.** Placement before `find_by_name` is sufficient at the repository’s current PostgreSQL default, and the strict path must participate: otherwise a flag request can still race a concurrent strict creator ([table_service.py:674](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:674)).

   `table_migration_service` does not actually use a domain-separated key: both use `f"{vault_id}:{value}"` ([table_migration_service.py:240](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_migration_service.py:240)). A migration key equal to a table name therefore aliases exactly. No advisory deadlock is evident today—each path acquires one such lock, and migration calls `alter_table`, which acquires no second advisory lock—but the alias causes unnecessary blocking. Prefix the domains, e.g. `table-create:` and `table-migration:`. Ordinary 64-bit hash collisions likewise cause false contention, not incorrect ownership classification.

   The pool does not enforce transaction isolation ([postgres.py:49](/Users/kwoo2/Desktop/storage/akb/backend/app/db/postgres.py:49)). Under a deployment-level `REPEATABLE READ` default, the vault lookup establishes a snapshot before the wait, so the loser may not see the winner afterward. Make this transaction explicitly `isolation="read_committed"`.

4. **Cross-vault table ownership is closed in the normal catalog model.** There is exactly one `created=false` return site, reached only after `find_by_name` matches `vt.vault_id = $1 AND vt.name = $2` ([table_registry_repo.py:27](/Users/kwoo2/Desktop/storage/akb/backend/app/repositories/table_registry_repo.py:27)).

   Every other route is closed:

   - Same logical concurrent creator: serialized, then exact-row lookup; valid no-op.
   - Foreign physical table already present: `to_regclass` raises 409.
   - Foreign physical table created after preflight: scoped `DuplicateTableError` raises 409.
   - Registry/PG uniqueness failure: raises 409; never rereads or returns false.
   - Advisory-key collision: only delays the exact lookup.
   - There is no broad `ConflictError` suppression.

   Thus the stated foreign-table invariant is airtight absent catalog corruption. One defense-in-depth caveat: `vault_tables.collection_id` is not constrained to the same vault, and the join checks only collection ID ([init.sql:258](/Users/kwoo2/Desktop/storage/akb/backend/app/db/init.sql:258), [table_registry_repo.py:38](/Users/kwoo2/Desktop/storage/akb/backend/app/repositories/table_registry_repo.py:38)). Ordinary application writes preserve it, but a corrupted/admin-written row could expose another vault’s collection path—not another vault’s table.

5. **The early return is transactionally correct.** Returning inside `async with conn.transaction()` commits the read-only transaction during context exit, then releases the transaction-scoped advisory lock. No collection, DDL, event, or post-commit indexing executes. A validation exception rolls back and releases it instead. The lock is held only through lookup, normalization, and envelope construction on this path.

Remaining gaps:

- The “live PostgreSQL” tests exercise raw lock primitives, not concurrent calls through `table_service.create_table` ([test_table_if_not_exists_e2e.py:64](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_if_not_exists_e2e.py:64)). Add a real service-level concurrency test asserting one `created=true`, one `false`, one registry/physical table, and one event—including differing schemas and collections.
- Add write-only PAT/OAuth and managed-write-grant tests on both surfaces.
- Give create its own OpenAPI response union/full-versus-minimal contract; the current shared envelope merely makes all new fields optional.
- MCP’s `bool(args.get(...))` turns the string `"false"` into true ([server.py:1013](/Users/kwoo2/Desktop/storage/akb/backend/mcp_server/server.py:1013)); validate an actual boolean.
- Reconcile the design document’s still-open status with the implementation decision.

Focused staged tests: 44 passed, 1 skipped; cached diff hygiene passed.