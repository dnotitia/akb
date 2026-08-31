# Existing-database revision cutover

This runbook moves one existing standalone AKB database from the default
`bare_git` revision backend to `postgres_native`. It requires planned downtime.
It does not perform an online dual write or a reverse migration.

## Before the window

1. Upgrade the AKB image while it still selects `bare_git`, so migrations and
   the cutover command are installed without changing authority.
2. Take a coherent PostgreSQL and Git-storage recovery point and verify that it
   can be restored into a disposable environment.
3. Move persisted external-Git sources to Git Collector. The cutover refuses to
   commit while an `external_git` sidecar remains.
4. Record the future Native deployment identity fields in `app.yaml`:
   `document_revision_tenant_id`, `document_revision_namespace`,
   `document_revision_database_id`, and the immutable image digest.

## Downtime sequence

Stop the API and every AKB/collector worker that can write the database, Git
storage, or File catalogue. Then run, from the same image and configuration:

```console
python -m app.cli migrate-revision-backend plan \
  --coverage-version operator-YYYYMMDD
python -m app.cli migrate-revision-backend apply --cutover-id CUTOVER_UUID
python -m app.cli migrate-revision-backend verify --cutover-id CUTOVER_UUID
python -m app.cli migrate-revision-backend commit --cutover-id CUTOVER_UUID
```

Each command prints a JSON receipt. Preserve it with the recovery point. `plan`
captures every retained (non-deleted) Vault and its current Git ref; omitted
Vaults, changed refs, File drift, or an external-Git sidecar make later
authority commit fail closed. Archived Vaults are included because they can be
reactivated later. `commit` revalidates the complete retained inventory while
holding Git write locks and closes the database-enforced Legacy revision-write
fence atomically with Native authority. The committed fixed ref also remains
the frozen source for pre-cutover vault activity, including deleted and
delete/recreate lifecycles.

Before `commit`, an operator may permanently close the attempt without deleting
its evidence or additive Native rows:

```console
python -m app.cli migrate-revision-backend abort --cutover-id CUTOVER_UUID
```

An aborted cutover cannot be resumed. Create a new plan with a new coverage
version after correcting the cause. Abort is unavailable after authority
commit.

## Start Native

Set `document_revision_backend: postgres_native` with the exact identity used by
`commit`, then start the API and workers. Verify original current and historical
Document reads, ACLs, File metadata/downloads, and worker drain before reopening
writes.

Authority commit is the forward-only boundary. Operational rollback after that
point is restoration of the coherent pre-cutover PostgreSQL and Git recovery
point while the service remains stopped. This release does not delete or edit
the immutable authority record in place and does not reverse post-cutover
writes.
