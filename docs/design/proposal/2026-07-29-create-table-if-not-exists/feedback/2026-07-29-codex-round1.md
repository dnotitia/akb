> Historical review of an intermediate implementation. Findings below were resolved by the [final review](2026-07-29-codex-round13-final.md); they are not a current vulnerability report. Source links point to the repository snapshot used for this documentation cleanup, not the intermediate reviewed diff. Historical line labels are retained as quotations; URL line anchors are omitted.

The proposal needs revision before acceptance. The transaction and race handling are blockers.

1. The `UniqueViolationError` reread is not implementable inside the current transaction. The exception is caught while the outer transaction is still active, leaving PostgreSQL in an aborted state; any subsequent query fails until rollback. See [table_service.py:586](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py) and [table_service.py:729](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py).

   Correct structures are either:

   - Catch outside `async with conn.transaction()`, let the whole losing attempt roll back, then start a fresh transaction and reread.
   - Put all potentially failing DDL/registry work in a nested `conn.transaction()` savepoint, catch only after that savepoint rolls back, then reread under `READ COMMITTED`.

   In either case, return no-op only if the exact `(vault_id, name)` row exists. If it does not, preserve the conflict: the uniqueness failure may represent a cross-vault physical-name collision, not a concurrent logical create. `grant_table_in_conn` and `emit_event` must remain winner-only inside the successful transaction. The no-op must also return before post-commit metadata indexing at [table_service.py:750](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py).

   Better still: take a transaction-scoped advisory lock keyed by `(vault_id, name)` before the existence check, following the existing precedent in [table_migration_service.py:240](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_migration_service.py). That removes the same-name race rather than recovering from it.

2. There are more than two collision sites. A winner can commit:

   - Between `find_by_name` and `to_regclass`, causing the physical-name preflight to conflict at [table_service.py:648](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py).
   - After that preflight but before the loser’s `CREATE TABLE`, potentially producing `DuplicateTableError` at [table_service.py:674](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py).
   - During catalog or registry insertion, producing the cited uniqueness error.

   All three race outcomes need exact-row classification, or the advisory lock must make the first two impossible for the same logical table. Physical fusion involving a different logical table must remain a conflict.

3. The existence-disclosure conclusion is correct, but the stated reason is not. The current strict create already reveals existence through success versus 409, so the flag adds no new existence bit to anyone authorized to call create.

   However, not every authorized writer can browse. Read and write token scopes are independent: create is write-scoped, browse is read-scoped, and `write` does not imply `read` ([server.py:205](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/mcp_server/server.py), [server.py:232](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/mcp_server/server.py), [auth_service.py:213](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/auth_service.py)). Managed-vault wildcard grants can also authorize a write before reader membership is checked ([access_service.py:254](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/access_service.py)).

   Consequently, returning the stored schema, collection, and URI is a new disclosure for write-only callers. Either require read authority for the enriched no-op response, define write as implying read, or return a minimal no-op envelope to callers lacking read authority.

4. Returning stored state is necessary but insufficient. A successful-looking `created=false` response still invites callers to assume the requested schema was ensured. Make mismatch machine-explicit, for example:

   ```json
   {
     "created": false,
     "outcome": "already_exists",
     "matches_request": false,
     "mismatches": ["collection", "columns"],
     "table": { "...": "stored canonical resource" }
   }
   ```

   Use an exact canonical comparison, not “compatible,” which would reopen schema-diff policy. Define whether comparison covers collection and description as well as columns, keys, and indexes. Also define validation order: currently an existing table bypasses column/key/index validation. Test concurrent requests with different schemas and different collection paths.

5. Several compatibility and implementation claims need correction:

   - Adding `created=true` to default successful creates means behavior is additive, not “bit for bit” unchanged.
   - The actual REST route is `POST /api/v1/tables/{vault}`, not `/vaults/{vault}/tables` ([tables.py:261](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/api/routes/tables.py)).
   - The MCP schema/help and static OpenAPI `AkbTableEnvelope` must change ([tools.py:606](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/mcp_server/tools.py), [openapi_contract.py:645](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/openapi_contract.py)).
   - A no-op must not auto-create the requested collection, overwrite/index metadata from the losing request, or emit more than one `table.create`.
   - Stored JSONB should be normalized through the repository parsing helpers, including legacy string rows ([table_registry_repo.py:222](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/repositories/table_registry_repo.py)).

6. Agree: no `table.create` event on a no-op. Domain events represent committed changes, while tool usage already records the attempted command. But the collection precedent is described incorrectly: `create_collection` currently emits `collection.create` unconditionally, including `created=false`, at [collection_service.py:151](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/collection_service.py). Do not cite it as event precedent.

7. “Low” understates the evidence. The data proves callers recovered from these conflicts, not that no user was blocked or that tasks completed successfully. Repeated conflicts introduce systematic latency/cost and obscure an operational signal (deployment counts omitted in this public copy). I would classify it medium priority—still below the live search-latency and failing-terminal-step defects, but not backlog-level low.