# Local Compose deployment

The repository root `docker-compose.yaml` is the only base Compose entry point.
Run `docker compose up -d` from the repository root. On a fresh installation,
Compose creates its named volumes; subsequent starts reuse them.
Follow the root README to copy the YAML configuration, generate persistent
local-session keys, and provision the administrator.

Like the Kubernetes backend Pod, Compose runs API and worker separately,
sharing `/etc/akb` and the Git volume. Update/build both together. Both have
45 seconds to stop; worker health checks its event-loop heartbeat. Configuration
is read at startup: restart both services after editing the mounted YAML.
The worker uses `tokenizer_processes` from app.yaml, while the API uses one
query tokenizer. An unexpectedly exited worker restarts automatically via
`unless-stopped`; an explicit operator stop remains respected. A health check
reports a stalled process but does not itself restart an unhealthy container.

The example configuration connects the bundled MinIO service internally and
uses `localhost:9000` for browser-signed URLs. For remote access, change
`public_base_url` and `s3_public_url` to the actual browser-reachable origins.
Replace the example credentials outside local development. Set both S3 URLs
blank to disable object storage, or supply your external S3 settings.

## Existing installations

Do not overwrite your existing `config/` or regenerate installation keys.
Merge the example changes into your configuration. Existing root Compose
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
