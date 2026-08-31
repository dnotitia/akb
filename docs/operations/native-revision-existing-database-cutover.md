# Existing-database revision cutover

This runbook moves one existing standalone AKB database from the default
`bare_git` revision backend to `postgres_native`. It requires planned downtime.
It does not perform an online dual write or a reverse migration.

## Before the window

1. Upgrade the AKB image while it still selects `bare_git`, so migrations and
   the cutover command are installed without changing authority.
2. Take a coherent PostgreSQL and Git-storage recovery point and verify that it
   can be restored into a disposable environment.
3. For each persisted external-Git source, have Git Collector write a **read-only
   v1 adoption manifest while the source is still an AKB `external_git` mirror**.
   AKB accepts only Collector's exact
   `akb-collector.git-adoption-manifest` v1 shape: the fixed purpose,
   `binding.{name,source_scope,target_vault,target_collection}`, and
   `source.{remote_url,branch,snapshot_commit,path_prefix}`. `path_prefix` is
   required: it is `null` for an unfiltered source or the Collector's canonical
   filtered prefix. Each document proves `origin_key`, path, resource URI,
   source version/blob SHA, AKB content SHA-256/current version, and the
   `managed_metadata` fields. The manifest is credential-free and body-free.
   Store it as an operator artifact; do not put a token or document body in it.
   AKB resolves `target_vault` to the exact `--vault-id`, preserves the binding
   fields as Collector proof context, and verifies **every** live active
   external-Git Document—not only documents under `path_prefix`—before it
   accepts the handoff.
4. Record the future Native deployment identity fields in `app.yaml`:
   `document_revision_tenant_id`, `document_revision_namespace`,
   `document_revision_database_id`, and the immutable image digest.

## Downtime sequence

Stop the API and every AKB external-Git/collector worker that can write the
database, Git storage, or File catalogue. For each manifest created above, run
the one-way sidecar retirement from the same image and configuration:

```console
python -m app.cli migrate-revision-backend retire-external-git \
  --vault-id VAULT_UUID \
  --manifest-file /secure/operator/collector-adoption.json \
  --idempotency-key RETIREMENT_UUID \
  --requested-by OPERATOR_ID \
  --confirm-planned-downtime RETIRE-EXTERNAL-GIT:VAULT_UUID
```

This command quarantines the old poller before changing the marker, and its
in-transaction sidecar fence prevents an already-running poller from mutating
Documents after quarantine. The retirement tombstone also blocks ordinary Git
writes until the durable receipt and marker cleanup complete. It retains the
Vault, Documents, collections, Files, Git repository, and ACLs, then removes
only the external-Git sidecar. Its receipt contains only the manifest digest,
count, remote/branch/fixed ref, idempotency key, and audit identity. An exact
replay is safe; changing any of those facts is a conflict. A refusal leaves the
mirror fail-closed for investigation.

After retirement, let Git Collector perform its ordinary authorized full
adoption/sync against the retained manual Vault, refreshing every retained
Document, then stop writers again. Do not reuse a pre-retirement cutover plan:
it keeps the former mirror as an immutable exclusion. Abort that uncommitted
plan and make a fresh plan only after the Collector adoption/sync has
completed. Then run:

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
