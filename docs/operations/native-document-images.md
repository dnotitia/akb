# Native document image references

Native document publication atomically commits its Head, attachment claims,
current image references and bounded revision manifests. This applies to
creation, replacement, exact edits, grep replacement, moves and deletion.
File-surface revisions and the Bare Git write path are unchanged.

New image references must name confirmed attachments in the same Vault.
An unavailable URL already present in the previous committed body remains
editable; imports may explicitly preserve unavailable references. Neither
case grants access to missing or other-Vault objects.

Image uploads remain separate immutable objects. Object-store I/O never runs
inside the document transaction. The two approved Native body placements are
local PostgreSQL payloads: the locked parent's verified body determines its
previous image set. A failed publication rolls back claims and references
with the Head; a replay returns the original receipt without reapplying them.

Single-document deletion retains the actual last revision's images for the
configured retention period. Collection deletion preserves its existing
policy: it removes current references without extending that period.

## Repair existing references

An upgrade prevents new inconsistencies but does not repair old ones. Use
the existing command with the application's configuration and database
credentials, first in an isolated test environment:

```sh
python -m app.cli native-asset-refs-backfill --vault example --dry-run
python -m app.cli native-asset-refs-backfill --vault example
```

Omit `--vault` to inspect Vaults with Native documents, including those whose
referenced attachments have all disappeared. `--limit N` requires a positive
integer and bounds the document selection and repair work; a truncated run
deliberately exits nonzero. The command locks
each resource and reads its current verified Head, so a concurrent move,
update or delete cannot make it attach a stale path's images to another
document. Empty bodies and deleted documents have obsolete current references
removed. Repair does not create a new revision or restart the history clock.

The JSON report distinguishes `stale_refs`, `refs_removed`, `refs_written`,
`unresolved` and `failed`. Dry runs never claim attachments or change references;
their write/removal counts remain zero. A complete successful run exits zero
only when no failed or unresolved items remain. Missing bytes cannot be
recreated: investigate unresolved items and restore from a trusted backup or
correct the document deliberately. This command repairs current references,
not every historical manifest.

## Verification

`backend/tests/concurrency/test_native_document_assets.py` uses committed
PostgreSQL transactions and deterministic barriers for delayed saves,
deletion, rollback, GC ordering, retention and replay. It runs in the live-PG
CI job with `REQUIRE_REAL_PG=1`; parser-only tests are not an atomicity proof.

```sh
cd backend
REQUIRE_REAL_PG=1 AKB_TEST_DSN=postgresql://localhost/akb_test \
  python -m pytest tests/concurrency/test_native_document_assets.py -v
```

Use only a disposable test PostgreSQL server: the fixture creates and drops
its own databases. HTTP acceptance additionally needs a freshly bootstrapped
Native installation and an S3-compatible store, covering upload, competing
saves, move, historical image reads and deletion.
