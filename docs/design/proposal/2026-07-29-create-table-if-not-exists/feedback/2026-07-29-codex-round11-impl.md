> Historical review of an intermediate implementation. Findings below were resolved by the [final review](2026-07-29-codex-round13-final.md); they are not a current vulnerability report. Source links point to the repository snapshot used for this documentation cleanup, not the intermediate reviewed diff. Historical line labels are retained as quotations; URL line anchors are omitted.

Not ready to merge.

1. **Blocker:** `_comparable()` wrongly drops `default: False`. A boolean column without a default generates `BOOLEAN`; one with `default: false` generates `BOOLEAN DEFAULT FALSE`, yet they currently compare equal. Scope is correct; the predicate is not. Treat `False` as absent only for `required`/`unique`/`index`, and `None` as absent for `default`. Add that regression. [table_service.py](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/backend/app/services/table_service.py)

2. **CI gating is genuine.** The workflow supplies `AKB_TEST_DSN`, explicitly runs the file, bootstraps a fresh database, and connection/bootstrap failures fail rather than skip. [backend-pytest.yml](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/.github/workflows/backend-pytest.yml)

3. **Minor factual cleanup:** the design still says 24 feature units and 1045 total; the staged parametrization makes that 30 feature cases, and your reported total is 1049. [README.md](https://github.com/dnotitia/akb/blob/99474c10d5f8a6fb6608074cda0dfc94957d5a0a/docs/design/proposal/2026-07-29-create-table-if-not-exists/README.md)