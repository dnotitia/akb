Not ready to merge. No runtime blocker remains, but these items do:

1. Test isolation is still incomplete. Settings are mutated before the `finally`; skips during bootstrap/seed bypass restoration at [test_table_if_not_exists_e2e.py:210](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_if_not_exists_e2e.py:210). Cleanup failures can also bypass `close_pool()`. Put mutation through pool closure under one outer `try/finally`.

2. The MCP help topic remains stale: it omits `if_not_exists` and its response semantics at [help.py:926](/Users/kwoo2/Desktop/storage/akb/backend/mcp_server/help.py:926).

3. The design record is not current. It still says proposal, lists only round 1, says three rounds, 1042 tests, and 23 feature units instead of 24 at [README.md:3](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:3) and [README.md:304](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:304). It should also document the one-pass enum normalization split and SDK response fields.

4. The `normalize_columns` split is correct. Both branches produce identical normalized columns, keys, and indexes; reference validation follows canonicalization in both. I also compared enum plus inline/explicit key/index inputs directly—the canonical tuples were identical.

5. The feature-specific SDK/codegen surface is correct. One minor comment is inaccurate: [openapi-codegen.ts:384](/Users/kwoo2/Desktop/storage/akb/packages/akb-client/src/core/openapi-codegen.ts:384) implies `outcome` is withheld without read access, but the minimal response includes it. Independently, the hardcoded envelope still omits the pre-existing OpenAPI `created_at` field; that is unrelated debt, not a blocker for this feature.