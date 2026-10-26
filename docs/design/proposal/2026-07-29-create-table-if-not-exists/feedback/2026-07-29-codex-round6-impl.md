> Historical review of an intermediate implementation. Findings below were resolved by the [final review](2026-07-29-codex-round13-final.md); they are not a current vulnerability report. Source links point to the repository snapshot used for this documentation cleanup, not the intermediate reviewed diff. Historical line labels are retained as quotations; URL line anchors are omitted.

Not ready to merge.

1. Test isolation still leaks:

   - `settings.db_*` is mutated before the outer `try`; `close_pool()` can fail or be cancelled before restoration is armed. Move the `try` above the first mutation. [test_table_if_not_exists_e2e.py:211](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/tests/test_table_if_not_exists_e2e.py)
   - `settings.git_storage_path` is mutated at module import and never restored. [test_table_read_authority_unit.py:30](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/tests/test_table_read_authority_unit.py)
   - A row-cleanup failure still bypasses `setup.close()`, although `close_pool()` now runs. [test_table_if_not_exists_e2e.py:286](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/tests/test_table_if_not_exists_e2e.py)

2. The design record is not yet faithful:

   - Frontmatter still lists only rounds 1–4; round 5 is missing. [README.md:7](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md)
   - “Four no-ops” is inaccurate: scenarios 2–4 are no-ops; scenario 5 is a conflict. [README.md:374](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md)

3. The help topic itself is accurate, including disclosure semantics. Two adjacent comments remain inaccurate:

   - The SDK comment says `outcome` accompanies both create outcomes, but successful creation has no `outcome`. [openapi-codegen.ts:383](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/packages/akb-client/src/core/openapi-codegen.ts)
   - The MCP tool description’s “only” envelope omits `kind`. [tools.py:640](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/mcp_server/tools.py)

No runtime correctness or security blocker found beyond these items.