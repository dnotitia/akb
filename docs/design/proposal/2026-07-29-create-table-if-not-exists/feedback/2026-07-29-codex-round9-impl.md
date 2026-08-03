Not ready to merge. One merge blocker remains: contradictory comments/design wording.

- [table_service.py:759](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:759), [table_service.py:928](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:928), and [test_table_if_not_exists_e2e.py:198](/Users/kwoo2/Desktop/storage/akb/backend/tests/test_table_if_not_exists_e2e.py:198) still describe registry insertion as a same-path race window.
- [README.md:119](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:119) still says “four race outcomes”; the table defines three total.
- [README.md:431](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:431) retains the unqualified “strict path is unchanged.”
- [postgres.py:76](/Users/kwoo2/Desktop/storage/akb/backend/app/db/postgres.py:76) says the clear occurs “in a `finally`”; it actually occurs before the await.

The revised race table and E2E module header are correct. Remaining citations appear accurate.

I agree the cached `doc_service` limitation is pre-existing test debt and a follow-up, not a merge blocker. No runtime blocker found.