> Historical review of an intermediate implementation. Findings below were resolved by the [final review](2026-07-29-codex-round13-final.md); they are not a current vulnerability report. Source links point to the repository snapshot used for this documentation cleanup, not the intermediate reviewed diff. Historical line labels are retained as quotations; URL line anchors are omitted.

Not ready to merge.

1. **Blocker — `matches_request` has false mismatches.** Canonicalization preserves no-op fields: omitting `required` and specifying `required: false` produce unequal columns despite identical schemas. I reproduced this directly. The same applies to `unique/index: false` and some explicit nulls. Fix canonicalization and add regression coverage. [table_service.py](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py)

2. **Blocker — the load-bearing live test is not CI-gated.** The unit job excludes `_e2e`, while the live-Postgres job does not list this file. Moreover, its service-level test skips on the job’s unbootstrapped database. Add it to the live job and ensure schema setup cannot green-skip. [backend-pytest.yml](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/.github/workflows/backend-pytest.yml) [test_table_if_not_exists_e2e.py](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/tests/test_table_if_not_exists_e2e.py)

3. **Blocker — remaining factual wording:**

   - “three further windows” names only the two post-check windows; the unit test still says “four different race outcomes.” [table_service.py](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py) [test_table_if_not_exists_unit.py](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/tests/test_table_if_not_exists_unit.py)
   - Missing reference targets are HTTP 400, not 422 as the comments/design claim. [table_service.py](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py)
   - Several docstrings promise stored schema unconditionally despite the minimal-envelope branch. [tables.py](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/api/routes/tables.py) [table_service.py](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py)
   - The compatibility rationale incorrectly says the relevant request models use `extra="allow"`; `CreateTableRequest` inherits `NFCModel`, and request parsing does not establish response compatibility. [README.md](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md)

No new follow-up-only item; the items above block merge.