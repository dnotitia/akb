# Local Compose deployment

The repository root `docker-compose.yaml` is the only service definition.
`deploy/docker-compose.yaml` includes it for compatibility (Compose 2.20+).
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
Its compatibility entry point maps the new logical volume keys to the existing
`<project>_pgdata` and `<project>_vaultdata` names automatically. Keep the same
Compose project name. These are external volumes: existing legacy Compose
labels are accepted, and a missing volume fails instead of silently creating
an empty replacement. Use the root entry point for a fresh installation.
Before upgrading, back up the database and Git data and
prepare the YAML files and persistent local-session keys using the existing
database credential. If your old volume names were customized, preserve those
names with an operator-owned override. Switching to the root entry point changes
the default volume names; continue using the compatibility entry point until
you explicitly map your existing volumes. No data migration is performed.
Existing volumes are never deleted by this change. Do not use `down -v`.

The Keycloak and Qdrant overlays remain optional. Keycloak still requires the
SSO settings and persistent session encryption key documented in its overlay;
merely starting a Keycloak container does not enable SSO.
