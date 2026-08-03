Not ready to merge.

1. Test isolation still has one gap. [`git_storage_path`](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_read_authority_unit.py:31) mutates during collection, but the fixture runs only if a test executes. Full deselection, `--collect-only`, or collection failure leaves it mutated. Move the save/set into the fixture before `yield`, with restoration in `finally`; lazy test imports occur afterward.

2. Factual cleanup remains:

   - The E2E header says “four post-check” windows; there are three. [test](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_if_not_exists_e2e.py:9)
   - The design says the “first three” windows become impossible; it is the last three, while `find_by_name` remains the intended outcome. It also retains imprecise “four/five sites” and contradictory “bit-for-bit unchanged” wording. [design](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:137)
   - Several design line citations are stale, including `tables.py:261` and the `table_service.py` concurrency locations.
   - The OpenAPI comment says all three fields accompany the false branch, but `matches_request` and `mismatches` are absent from the minimal false response. [openapi_contract.py](/Users/kwoo2/Desktop/storage/akb/backend/app/openapi_contract.py:654)