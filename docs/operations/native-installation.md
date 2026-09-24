# New PostgreSQL Native installations and safe configuration upgrades

Settings and the recommended Compose/Kubernetes new-install paths now default
to PostgreSQL Native. Use the prepared surfaces below for a **never-used AKB
database**. The reusable Kubernetes base, Helm, all-in-one, standalone SSO and
the Git-oriented CI runtime explicitly retain Bare Git. Merely changing
`document_revision_backend` does not convert an existing database. Existing
installations that need Native must follow the
[explicit cutover procedure](native-revision-existing-database-cutover.md).

## Prepare and preserve installation identity

Choose a prebuilt backend image pinned by registry digest, or build from source
and resolve its local image ID as shown in the root quickstart. Record which
kind of identity you used; a local image ID is not a registry digest. The following offline command
reads an application template and its companion secret configuration, creates a
new database UUID, and writes a **new** application configuration:

```sh
python -m app.cli prepare-native-config \
  --source config/app.yaml.example \
  --secret config/native/secret.yaml \
  --output config/native/app.yaml \
  --tenant-id my-installation \
  --namespace akb \
  --image-digest sha256:REPLACE_WITH_INITIAL_IMAGE_DIGEST
```

Create the output directory and provision its `secret.yaml` first. The command
runs without an existing application configuration, database connection, or
network access. It does not copy or print secret values. It rejects revision
controls in `secret.yaml`, because that file otherwise overrides `app.yaml`.
It also rejects templates already carrying an installation identity. Supply a
fresh template for a new installation, never a copy of another tenant's config.

On an identical retry, the output and UUID are reused unchanged. A different
request fails without overwriting the file. Keep this configuration and its
initial template/request as installation assets. Do not regenerate them at
startup. Later non-identity configuration edits are explicit operator changes;
this preparer is not a general configuration updater.

The `document_revision_runtime_image_digest` field is the **initializing image
receipt**. Keep it unchanged when upgrading the workload image. Record the new
workload image digest separately. Changing tenant, namespace, database UUID, or
initializing digest breaks bootstrap replay intentionally; it is not a recovery
mechanism. Preserve the local-session keys and other existing secrets as well.

Use the same backend image to run the command if Python is only available in
containers. Bind the input/output directory, and use absolute paths inside that
mount. The backend image is pip-installed: use `python -m app.cli`, not `uv`.

## Compose

Requires Docker Compose 2.24.4 or newer for `!reset`/`!override`. Prepare a new
persistent config directory containing the generated `app.yaml`, operator-owned
`secret.yaml`, and persistent `local-session/` keys. Generate keys using
`python -m app.cli generate-local-session-keyset --output-dir ...` as described
in the root README. Configure the bundled PostgreSQL/MinIO names and credentials
in that configuration before preparing it. Do not reuse an existing Compose
project's database volumes for this new-install flow.

```sh
export AKB_NATIVE_IMAGE='registry.example.com/akb-backend@sha256:REPLACE_WITH_IMAGE_DIGEST'
export AKB_NATIVE_CONFIG_DIR='/absolute/path/to/config/native'
# PostgreSQL is built from deploy/postgres/Dockerfile (pgvector plus the
# vchord_bm25 BM25 index), so it has to exist before an `up --no-build`.
docker compose -p my-native-install build postgres
docker compose -p my-native-install \
  -f docker-compose.yaml -f docker-compose.native.yaml \
  up -d --no-build postgres minio minio-bootstrap native-bootstrap backend worker
```

The explicit service list starts the backend stack. For the complete local
stack, first run `docker compose -p my-native-install build frontend`, then
omit the service list in the command above. The root quickstart shows the
complete source-build flow, File bucket initialization and recovery admin. Bootstrap waits for PostgreSQL,
then the API waits for successful bootstrap, and the worker waits for API
readiness. All three application processes use the same pinned image and
read-only configuration. Their Git paths are empty, read-only tmpfs mounts;
they do not require shared Git storage. Object storage is still required for
binary Files when enabled.

If bootstrap fails, inspect its exit status and logs. Fix the supplied
configuration or dependency failure and rerun the same command with the same
installation identity. A matching pre-schema claim resumes after interruption;
a previously used database without that claim is rejected even if its tenant
rows were deleted. Do not erase authority records or add an automatic fallback.
After successful initialization, bootstrap can be replayed with the same
persisted configuration, including after real documents have been written.

On upgrades, update `AKB_NATIVE_IMAGE`, keep the initializing receipt in
`app.yaml`, and rebuild what the new checkout builds before any
`up --no-build`: `docker compose -p my-native-install build postgres`, plus
`frontend` for the complete stack. `--no-build` cannot create those images, and
without them Compose stops the running containers before it fails with
`No such image`. A rebuilt PostgreSQL image recreates the PostgreSQL container
once on the same data volume. Then recreate API/worker together using the same
Compose project and files. A bootstrap replay remains safe. Confirm both processes are healthy and
read back existing documents after replacement. Preserve database/S3/config/key
volumes; never use `down -v` as a repair step.

## Kubernetes

`deploy/k8s/native/` is the recommended **new-install** overlay. Generate its ignored
`app.yaml` with the command above, using the Kubernetes base application's
settings as the template and the actual companion `secret.yaml`. The base
application YAML is the `data.app.yaml` value in `deploy/k8s/backend.yaml`.
Provision `Secret/akb-secret` and persistent local-session keys using the base
Kubernetes README. The namespace in the configuration must match the intended
installation; adjust both the overlay and config together before first start.

Add an `images` entry to your operator-owned copy of this overlay (or use
`kustomize edit set image`) to replace **every** `akb-backend` image, including
the init container, with the pinned backend image, and PostgreSQL's with the
image you built from `deploy/postgres/Dockerfile`:

```yaml
images:
  - name: akb-backend
    newName: registry.example.com/akb-backend
    digest: sha256:REPLACE_WITH_IMAGE_DIGEST
  # PostgreSQL built from deploy/postgres/Dockerfile: pgvector plus the
  # vchord_bm25 BM25 index, which gives the new database the default `vchord`
  # sparse shape. Without this entry the base's stock pgvector image gives it
  # `posting`.
  - name: pgvector/pgvector
    newName: registry.example.com/akb-postgres
    digest: sha256:REPLACE_WITH_POSTGRES_IMAGE_DIGEST
```

Render before applying, using the same explicit Kubernetes context for all
operations. Sibling base resources require the same load restriction setting
as the standalone SSO overlay:

```sh
kubectl kustomize --load-restrictor=LoadRestrictionsNone deploy/k8s/native > native-rendered.yaml
kubectl --context YOUR_NEW_INSTALL_CONTEXT apply -f native-rendered.yaml
```

The bootstrap init container must succeed before either the API or worker
starts. PostgreSQL may not be ready on its first attempt; Kubernetes retries the
init container. Both application containers use the same config and an empty,
read-only Git mount. The overlay omits the unused Git PVC from its render; it
must not be used to repurpose an existing installation with legacy history.
The ordinary base still supplies PostgreSQL persistence and the remaining
application resources. Pin/build the frontend image separately if deploying it.

A Pod replacement replays bootstrap using the retained initialization identity.
A wrong identity or pre-existing Legacy schema keeps the init container failed
and the application unavailable, rather than silently selecting another
backend. Restore the correct installation assets; never clear the authority
marker. For image upgrades, change the image pin, not the initial receipt.

## Preserve existing configuration before changing defaults

Run this offline command against the **active** app/secret pair and review the
new output before installing it:

```sh
python -m app.cli preserve-revision-config \
  --source /path/to/current/app.yaml \
  --secret /path/to/current/secret.yaml \
  --output /path/to/reviewed/app.yaml
```

An omitted backend becomes explicit `bare_git`, matching the behavior before
this default change. Run this step before starting the new runtime; the new
Settings default cannot determine whether an omitted selector belonged to an
old installation.
Explicit selectors (including accepted historical aliases), Native identity,
and other app values remain unchanged. Revision controls in secrets are
rejected; move those non-secret controls into the app configuration explicitly
before retrying. No database is touched or converted, and input files are not
overwritten. YAML comments/formatting may change, so compare parsed values.
This command preserves configuration; it does not certify the database state.

The runtime selects `./config` before `/etc/akb` when both contain `app.yaml`,
then lets the selected directory's `secret.yaml` override app values. Revision
selection is not an environment-variable switch. Verify the mounted active
pair rather than editing an inactive example file.

Managed deployments must persist identity and bootstrap state in their owning
controller. A generated standalone file does not override a controller that
reconciles its own ConfigMap. The new Settings default does not automatically migrate managed or legacy
installations. Managed provisioning belongs to the owning controller.

## Release and upgrade order

This is a **breaking configuration-default change** for configurations that
omit `document_revision_backend`. It belongs in a release that explicitly
announces this compatibility boundary, not an unannounced patch rollout.
The source change alone does not publish a release or deploy an environment.

1. Before replacing an existing runtime, back up the active app/secret pair,
   database (including roles/grants and authority), local-session keys, object
   storage and retained Git data. Record the running image and actual volume names.
2. Run `preserve-revision-config` from a version containing that command (or
   run the new image offline with only the configuration mounted). Review its
   output and install it as the active `app.yaml` while retaining the old copy.
   Keep explicit Native identities, aliases and initial image receipts intact.
3. Replace API and worker with the same candidate image. For existing Bare Git,
   keep the base Compose files and Git volume; do not add the Native overlay.
   For existing Native, replay bootstrap only with the persisted identity and
   receipt. Verify readiness, existing document/history reads and a new write.
4. If validation fails, stop the replacement processes and restore the known
   configuration/image under the release's schema compatibility policy. Restore
   a backup only through the documented restore procedure; never clear Native
   authority, regenerate an identity or force Git fallback to make startup pass.

Copying `app.yaml.example` directly is no longer a runnable installation: its
Native identity fields are deliberately empty. Without preparation, Settings
validation fails before startup. A prepared identity pointed at an old schema
is independently rejected by bootstrap. These are protective failures, not a
request to initialize or convert the old database.

Legacy history, diff and activity compatibility remain available after an
explicit cutover. This default change does not migrate historical Git metadata
or authorize deleting the retained Git archive. Native template/external-Git
capabilities remain unsupported. Helm/all-in-one/SSO are explicit legacy paths,
not evidence that every installer now provisions Native.
