> Historical review of an intermediate implementation. Findings below were resolved by the [final review](2026-07-29-codex-round13-final.md); they are not a current vulnerability report. Source links point to the repository snapshot used for this documentation cleanup, not the intermediate reviewed diff. Historical line labels are retained as quotations; URL line anchors are omitted.

Not ready to merge.

1. Process-global restoration is still incomplete:

   - [`git_storage_path`](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/tests/test_table_read_authority_unit.py) is assigned before the `try`.
   - More importantly, the lazy import constructs the cached [`server.doc_service`](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/mcp_server/server.py), whose `GitService` permanently captures the temporary path. Restoring `settings` does not restore that object.
   - The E2E cleanup can leave `postgres._pool` targeting the test database if `close_pool()` raises or is cancelled, because `_pool = None` occurs only after the awaited close.

2. The race description is still inaccurate. For two calls through `create_table`, registry insertion is not a separate third post-check window: once one transaction successfully creates the physical table, the other cannot also reach registry insertion. A `pg_type` uniqueness failure may surface during `CREATE TABLE`; describe two later timing windows and their exception surfaces, not a registry-insert race.

3. Remaining factual cleanup in the design:

   - “the strict path is unchanged” at [README.md:428](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md) remains unqualified.
   - Citations `collection_service.py:151`, `table_service.py:652/:684`, and `tables.py:239` are still stale.

The OpenAPI false-branch comment is now accurate.