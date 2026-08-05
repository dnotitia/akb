Not ready to merge.

1. Test isolation still leaks:

   - `settings.db_*` is mutated before the outer `try`; `close_pool()` can fail or be cancelled before restoration is armed. Move the `try` above the first mutation. [test_table_if_not_exists_e2e.py:211](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_if_not_exists_e2e.py:211)
   - `settings.git_storage_path` is mutated at module import and never restored. [test_table_read_authority_unit.py:30](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_read_authority_unit.py:30)
   - A row-cleanup failure still bypasses `setup.close()`, although `close_pool()` now runs. [test_table_if_not_exists_e2e.py:286](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_if_not_exists_e2e.py:286)

2. The design record is not yet faithful:

   - Frontmatter still lists only rounds 1–4; round 5 is missing. [README.md:7](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:7)
   - “Four no-ops” is inaccurate: scenarios 2–4 are no-ops; scenario 5 is a conflict. [README.md:374](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:374)

3. The help topic itself is accurate, including disclosure semantics. Two adjacent comments remain inaccurate:

   - The SDK comment says `outcome` accompanies both create outcomes, but successful creation has no `outcome`. [openapi-codegen.ts:383](/Users/kwoo2/Desktop/storage/akb/packages/akb-client/src/core/openapi-codegen.ts:383)
   - The MCP tool description’s “only” envelope omits `kind`. [tools.py:640](/Users/kwoo2/Desktop/storage/akb/backend/mcp_server/tools.py:640)

No runtime correctness or security blocker found beyond these items.