Not ready to merge.

1. **Blocker — `matches_request` has false mismatches.** Canonicalization preserves no-op fields: omitting `required` and specifying `required: false` produce unequal columns despite identical schemas. I reproduced this directly. The same applies to `unique/index: false` and some explicit nulls. Fix canonicalization and add regression coverage. [table_service.py](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:582)

2. **Blocker — the load-bearing live test is not CI-gated.** The unit job excludes `_e2e`, while the live-Postgres job does not list this file. Moreover, its service-level test skips on the job’s unbootstrapped database. Add it to the live job and ensure schema setup cannot green-skip. [backend-pytest.yml](/Users/kwoo2/Desktop/storage/akb/.github/workflows/backend-pytest.yml:41) [test_table_if_not_exists_e2e.py](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_if_not_exists_e2e.py:233)

3. **Blocker — remaining factual wording:**

   - “three further windows” names only the two post-check windows; the unit test still says “four different race outcomes.” [table_service.py](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:759) [test_table_if_not_exists_unit.py](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_if_not_exists_unit.py:489)
   - Missing reference targets are HTTP 400, not 422 as the comments/design claim. [table_service.py](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:391)
   - Several docstrings promise stored schema unconditionally despite the minimal-envelope branch. [tables.py](/Users/kwoo2/Desktop/storage/akb/backend/app/api/routes/tables.py:113) [table_service.py](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:637)
   - The compatibility rationale incorrectly says the relevant request models use `extra="allow"`; `CreateTableRequest` inherits `NFCModel`, and request parsing does not establish response compatibility. [README.md](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:301)

No new follow-up-only item; the items above block merge.