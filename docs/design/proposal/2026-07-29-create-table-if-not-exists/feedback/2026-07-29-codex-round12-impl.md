Not ready to merge.

1. **Blocker — real JSONB divergence is still hidden.** Python equality treats `False == 0`. Thus JSONB defaults `false` and `0` survive `_comparable()` but compare equal at [table_service.py:715](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:715), despite generating distinct DDL. The same defect affects JSONB check values and nested JSON. Use type-sensitive recursive comparison and add default/check regressions.

2. **Blocker — nullable no-op fields still cause false mismatches.** Explicit `check: null`, `enum: null`, `references: null`, or `on_delete: null` is DDL-equivalent to omission, but only `default` is stripped at [table_service.py:634](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:634). These accepted REST inputs currently report `columns` divergence.

The new rules for `required`/`unique`/`index` and `default` themselves are correct. No other blocker found.