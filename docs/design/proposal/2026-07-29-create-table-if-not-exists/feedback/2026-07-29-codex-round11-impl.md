Not ready to merge.

1. **Blocker:** `_comparable()` wrongly drops `default: False`. A boolean column without a default generates `BOOLEAN`; one with `default: false` generates `BOOLEAN DEFAULT FALSE`, yet they currently compare equal. Scope is correct; the predicate is not. Treat `False` as absent only for `required`/`unique`/`index`, and `None` as absent for `default`. Add that regression. [table_service.py](/Users/kwoo2/Desktop/storage/akb/backend/app/services/table_service.py:629)

2. **CI gating is genuine.** The workflow supplies `AKB_TEST_DSN`, explicitly runs the file, bootstraps a fresh database, and connection/bootstrap failures fail rather than skip. [backend-pytest.yml](/Users/kwoo2/Desktop/storage/akb/.github/workflows/backend-pytest.yml:87)

3. **Minor factual cleanup:** the design still says 24 feature units and 1045 total; the staged parametrization makes that 30 feature cases, and your reported total is 1049. [README.md](/Users/kwoo2/Desktop/storage/akb/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md:342)