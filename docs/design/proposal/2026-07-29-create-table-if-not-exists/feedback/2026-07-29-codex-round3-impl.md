Not ready to merge.

1. **Blocker — REST OAuth still leaks the enriched envelope.** The REST helper checks only `token_scopes`; OAuth credentials have `token_scopes=None`, which passes `token_has_scope`, while `oauth_scopes` is ignored ([tables.py](/Users/kwoo2/Desktop/storage/akb/backend/app/api/routes/tables.py:267)). I directly verified that an OAuth user holding only `akb:vault:write` returns `True` from this gate when the vault ACL passes. MCP correctly checks both scope systems ([server.py](/Users/kwoo2/Desktop/storage/akb/backend/mcp_server/server.py:268)).

   More broadly, REST’s `get_current_user` does not enforce OAuth scopes at all ([deps.py](/Users/kwoo2/Desktop/storage/akb/backend/app/api/deps.py:107)); a read-only or zero-scope OAuth token with writer membership can currently POST mutations. Fix centrally or explicitly gate both write and read OAuth scopes here. This makes (b) a definite blocker.

2. **Blocker — `_canonical_create_spec` is not actually the real-create canonicalizer.** The real branch duplicates its logic instead of calling it ([table_service.py](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:796), [table_service.py](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:841)). More importantly, only the real branch runs `_validate_column_references` ([table_service.py](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:849)). Thus an existing-table request containing a syntactically valid reference to a missing, non-unique, or wrong-typed target returns a no-op/mismatch instead of the error a real create produces. Modern non-FK columns, keys, and indexes compare correctly, but full create parity is false.

3. **Blocker — the checked-in REST client is stale.** Its public `CreateTableRequest` lacks `if_not_exists`, and `AkbTableEnvelope` lacks every new result field ([schema.gen.ts](/Users/kwoo2/Desktop/storage/akb/packages/akb-client/src/core/schema.gen.ts:10), [schema.gen.ts](/Users/kwoo2/Desktop/storage/akb/packages/akb-client/src/core/schema.gen.ts:458)). The OpenAPI fixture is stale too, so its codegen check merely proves stale fixture and stale output agree. Typed SDK users cannot use this feature.

4. **`READ COMMITTED` is correct.** Each statement receives a new snapshot, so `find_by_name` after the advisory-lock wait sees the winner. Nothing else in this transaction requires stronger isolation ([table_service.py](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:746)). Add an assertion: the fake records `tx_kwargs` but never verifies it ([test_table_if_not_exists_unit.py](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_if_not_exists_unit.py:63)).

5. **Merge calls:**

   - (a) blocks: concurrency is the feature’s load-bearing guarantee, but the live test exercises generic lock primitives using the old unprefixed key, never `create_table` ([test_table_if_not_exists_e2e.py](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_if_not_exists_e2e.py:64)).
   - (b) blocks, with the demonstrated REST defect.
   - (c) may follow up; the union improves typing but is not runtime security-critical. Updating the existing SDK types now is still mandatory.
   - (d) should block governance sign-off: it still says unresolved proposal, and its lock pseudocode is obsolete ([README.md](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:1), [README.md](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:110)).

Also fix the MCP description/help: it promises stored schema to every caller despite the minimal branch, and `is True` silently treats malformed `"true"` as false instead of rejecting it ([tools.py](/Users/kwoo2/Desktop/storage/akb/backend/mcp_server/tools.py:631), [help.py](/Users/kwoo2/Desktop/storage/akb/backend/mcp_server/help.py:926)).

Focused units: 54 passed, 1 skipped. The cached diff also contains several unrelated design/production-analysis packages; split those before review.