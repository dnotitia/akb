> Historical review of an intermediate implementation. Findings below were resolved by the [final review](2026-07-29-codex-round13-final.md); they are not a current vulnerability report. Source links point to the repository snapshot used for this documentation cleanup, not the intermediate reviewed diff. Historical line labels are retained as quotations; URL line anchors are omitted.

Not ready to merge.

1. **Blocker — real JSONB divergence is still hidden.** Python equality treats `False == 0`. Thus JSONB defaults `false` and `0` survive `_comparable()` but compare equal at [table_service.py:715](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py), despite generating distinct DDL. The same defect affects JSONB check values and nested JSON. Use type-sensitive recursive comparison and add default/check regressions.

2. **Blocker — nullable no-op fields still cause false mismatches.** Explicit `check: null`, `enum: null`, `references: null`, or `on_delete: null` is DDL-equivalent to omission, but only `default` is stripped at [table_service.py:634](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py). These accepted REST inputs currently report `columns` divergence.

The new rules for `required`/`unique`/`index` and `default` themselves are correct. No other blocker found.