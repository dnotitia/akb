# Local Compose deployment

New installations follow the [root Native quickstart](../../README.md#quick-start):
prepare persistent configuration/identity once, then layer
`docker-compose.native.yaml` over the reusable `docker-compose.yaml` base.
The Native overlay explicitly bootstraps a never-used database before API and
worker start. It keeps their Git paths read-only and empty. Build `frontend` and
`postgres` before using `up --no-build` for the complete stack, and again after
every update of the checkout: `--no-build` cannot create an image that only a
build produces.

Use the same Compose project name, config directory and file list on every
operation so database and object-store volumes are reused. API and worker run
separately and share the persisted read-only config. Both have 45 seconds to
stop; worker health checks its event-loop heartbeat. Restart both after config
changes and upgrade both to the same pinned image. The initializing image
receipt inside app.yaml stays unchanged during image replacement.

The worker uses `tokenizer_processes` from app.yaml, while the API uses one
query tokenizer. An unexpectedly exited worker restarts via `unless-stopped`;
a health check alone does not restart an unhealthy container.

The example configuration connects the bundled MinIO service internally. For
remote access, change `public_base_url` to the actual browser-reachable origin.
`s3_public_url` is retained and ignored: bytes reach a client through the API,
never straight from the store, so no URL is signed for a browser to follow.
Replace the example credentials outside local development. Object storage is
off when `s3_endpoint_url` is blank and `s3_auth_mode` is not `default_chain`;
otherwise supply your external S3 settings.

## Existing installations

Before changing the runtime, run
[`preserve-revision-config`](../../docs/operations/native-installation.md#preserve-existing-configuration-before-changing-defaults)
on the active app/secret pair and install the reviewed output. Omitted old
selectors become explicit Bare Git; explicit Native identity stays intact.
Do not overwrite existing config with the new example or regenerate keys.
Existing Bare Git installations continue using the base Compose without the
Native overlay and retain their Git volume.

PostgreSQL is built from `deploy/postgres`: the same pinned pgvector image plus
the BM25 index extension. `docker compose up -d` builds it. With `--no-build`,
run `docker compose build postgres` first. The first start recreates the
PostgreSQL container once on the same data volume, and a database keeps the
sparse shape it serves. Existing root Compose
volume names (`postgres_data`, `vault_data`, `minio_data`) are unchanged.

The historical `deploy/docker-compose.yaml` used `pgdata` and `vaultdata`,
database service `db`, and environment variables no longer read by AKB.
That obsolete entry point has been removed. Its deletion does not delete Docker
volumes, but the root entry point uses different default volume names. Do not
start an existing installation against new empty volumes unintentionally.

Before switching, back up the database and Git data, stop the old application
processes, and identify the actual volume names with `docker volume ls` and
`docker volume inspect`. Prepare the YAML files and persistent local-session
keys using the existing database credential. Preserve existing keys if present.
Translate the old environment settings into the current YAML setting names.

For this one-time transition, create an operator-owned override outside the
repository, replacing both placeholders with the verified existing volume names:

```yaml
volumes:
  postgres_data:
    external: true
    name: REPLACE_WITH_EXISTING_DATABASE_VOLUME
  vault_data:
    external: true
    name: REPLACE_WITH_EXISTING_GIT_VOLUME
```

From the repository root, use
`docker compose -f docker-compose.yaml -f /absolute/path/legacy-volumes.yaml up -d`.
Continue supplying that override on subsequent Compose operations. The explicit
external mappings are only for existing installations, not fresh installs.
Verify the expected vaults and documents before retiring the old containers;
never run both application stacks against the same data concurrently.
No data is migrated or deleted automatically. Do not use `down -v`.

The Keycloak and Qdrant overlays remain optional. Keycloak still requires the
SSO settings and persistent session encryption key documented in its overlay;
merely starting a Keycloak container does not enable SSO.

## Native bootstrap and recovery

Fresh Native installations use `docker-compose.native.yaml` with a pinned
backend image, persistent generated identity, and a bootstrap dependency before
API/worker startup. See [Native installation and configuration upgrades](../../docs/operations/native-installation.md).
Existing databases still require the explicit cutover procedure; this overlay
does not migrate them.
