> Historical review of an intermediate implementation. Findings below were resolved by the [final review](2026-07-29-codex-round13-final.md); they are not a current vulnerability report. Source links point to the repository snapshot used for this documentation cleanup, not the intermediate reviewed diff. Historical line labels are retained as quotations; URL line anchors are omitted.

Not ready to merge. One merge blocker remains: contradictory comments/design wording.

- [table_service.py:759](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py), [table_service.py:928](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py), and [test_table_if_not_exists_e2e.py:198](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/tests/test_table_if_not_exists_e2e.py) still describe registry insertion as a same-path race window.
- [README.md:119](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md) still says “four race outcomes”; the table defines three total.
- [README.md:431](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md) retains the unqualified “strict path is unchanged.”
- [postgres.py:76](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/db/postgres.py) says the clear occurs “in a `finally`”; it actually occurs before the await.

The revised race table and E2E module header are correct. Remaining citations appear accurate.

I agree the cached `doc_service` limitation is pre-existing test debt and a follow-up, not a merge blocker. No runtime blocker found.