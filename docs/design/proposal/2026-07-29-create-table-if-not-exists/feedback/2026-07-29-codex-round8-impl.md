Not ready to merge.

1. Process-global restoration is still incomplete:

   - [`git_storage_path`](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_read_authority_unit.py:43) is assigned before the `try`.
   - More importantly, the lazy import constructs the cached [`server.doc_service`](/Users/kwoo2/Desktop/storage/akb/backend/mcp_server/server.py:168), whose `GitService` permanently captures the temporary path. Restoring `settings` does not restore that object.
   - The E2E cleanup can leave `postgres._pool` targeting the test database if `close_pool()` raises or is cancelled, because `_pool = None` occurs only after the awaited close.

2. The race description is still inaccurate. For two calls through `create_table`, registry insertion is not a separate third post-check window: once one transaction successfully creates the physical table, the other cannot also reach registry insertion. A `pg_type` uniqueness failure may surface during `CREATE TABLE`; describe two later timing windows and their exception surfaces, not a registry-insert race.

3. Remaining factual cleanup in the design:

   - “the strict path is unchanged” at [README.md:428](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:428) remains unqualified.
   - Citations `collection_service.py:151`, `table_service.py:652/:684`, and `tables.py:239` are still stale.

The OpenAPI false-branch comment is now accurate.