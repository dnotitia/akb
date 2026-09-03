# Existing Kubernetes installation: local to SSO cutover

This runbook plans a mode-exclusive `auth_mode: local` to `auth_mode: sso`
transition for an existing standalone Kubernetes installation. It complements
the resource definitions in
[`deploy/k8s/standalone-sso`](../../deploy/k8s/standalone-sso/README.md); it is
not permission to apply that reference overlay unchanged to a customized live
namespace.

SSO is a human-login boundary. AKB continues to own users, administrator
status, Vault grants, PATs, service identities, and application credentials.
Switching the mode removes local login, registration, password recovery, and
local product-admin login from the browser. It does not convert existing local
accounts into upstream identities and it does not revoke PATs automatically.

## 1. Freeze the target and choose a broker topology

Record the exact current and candidate image digests, database migration
ledger, PostgreSQL backup, storage snapshots, public AKB origin, TLS names,
Ingress ownership, and every component that authenticates to AKB. Do not use a
mutable image tag as either the candidate or rollback artifact.

Choose one topology before rendering resources:

- **Installation-owned Keycloak (`direct`)**: deploy the standalone bundle's
  dedicated Keycloak database, realm, `akb-web`, `akb-admin`, and
  `akb-sso-manager` clients. AKB `/admin` can manage its own upstream provider
  instances. This is the reference standalone shape.
- **Shared or platform-owned Keycloak (`delegated`)**: the realm owner creates
  and proves the required client profiles and configures upstream providers
  out of band. AKB must not receive broader realm authority merely to make the
  admin form writable.

Changing the public Keycloak issuer or its local subject values changes the
exact identity AKB sees. Treat that as an account migration, not a DNS change.

## 2. Audit account continuity before disabling local login

Inventory, without exporting credentials:

- total human users and administrators;
- users already bound to the candidate broker issuer and subject;
- local-only users that still need browser access;
- active PATs, service identities, and machine consumers;
- Vault grants owned by accounts that would otherwise be replaced;
- companion products that still depend on a legacy token-exchange contract.

Email and username are not identity keys. A first Entra login must not adopt
an existing AKB account merely because profile fields match. Pre-bind an exact
verified identity to the intended existing account, use an approved explicit
identity-migration operation, or accept a newly created account and re-grant
its authorization deliberately. `keycloak_enrollment_mode: invite_only`
fails closed until exact bindings exist; `open` permits new accounts but does
not preserve old account ownership.

At least one recovery administrator must be usable after the cutover. For a
shared broker, pre-provision the exact administrator issuer and subject with
`python -m app.cli provision-recovery-admin sso`. For the installation-owned
bundle, prove the native Keycloak product-administrator bootstrap and AKB
projection before retiring one-time material. Do not assume that an existing
local `is_admin` row can sign in after local mode is disabled.

## 3. Build a target-specific overlay

Start from the standalone SSO bundle, but preserve the live installation's
existing PostgreSQL storage, Vault Git storage, object storage, derived
indexes, Services, Ingress routes, NetworkPolicies, collectors, and companion
applications. A reference manifest may use the same resource names with
different selectors, StatefulSet definitions, or PVC contracts; an
unreviewed `kubectl apply` can replace those resources.

The rendered `app.yaml` must include a coherent SSO profile:

```yaml
auth_mode: sso
auth_runtime_generation: <positive monotonic integer>
keycloak_enabled: true
keycloak_server_url: https://<public-broker-host>
keycloak_internal_url: http://<in-cluster-keycloak-service>:8080
keycloak_backchannel_logout_uri: http://<backend-service>:8000/api/v1/auth/keycloak/backchannel-logout
keycloak_realm: akb
keycloak_client_id: akb-web
keycloak_public_client: false
keycloak_admin_client_id: akb-admin
keycloak_management_client_id: akb-sso-manager
keycloak_enrollment_mode: invite_only
keycloak_require_verified_email: true
keycloak_verify_ssl: true
public_base_url: https://<public-akb-host>
```

Use `open` only after account-admission behavior has been approved. The Secret
must provide an installation-owned UUID `sso_session_epoch`, an independent
32-byte unpadded base64url `sso_browser_session_encryption_key`, and the three
independent confidential-client secrets described by the standalone bundle.
Keep the generation/mode/epoch tuple stable for ordinary restarts; advance the
generation for every later mode or epoch change.

Render, schema-check, and diff the target-specific overlay against the live
objects before the maintenance window. A client-side dry run does not prove
selector, PVC, Ingress, or database compatibility; review those explicitly.

## 4. Rehearse the database transition

Restore a current production backup into an isolated candidate environment
and run the exact candidate image. Verify the migration ledger and execute the
session-epoch preflight there before attempting the live database.

An installation first upgrading from a pre-epoch build requires the explicit
stop-the-world bridge:

1. Stop every backend replica and every other direct client of the AKB
   PostgreSQL database.
2. Configure the desired positive generation and SSO epoch, plus the temporary
   `sso_session_epoch_upgrade: stop-the-world-v1` acknowledgement.
3. From the candidate image run
   `python scripts/sso_session_epoch_preflight.py status`; a legacy database
   reports `migration_pending`.
4. Run `python scripts/sso_session_epoch_preflight.py prepare-upgrade`. It
   refuses concurrent database clients, applies the compatible migration set,
   purges pre-epoch browser authority, and enables the legacy-write guard.
5. Require `state=enforced` and `legacy_rows_present=false`, then remove the
   temporary acknowledgement before starting the application.

The ordinary backend startup is not a substitute for `prepare-upgrade`.

## 5. Cut over in a maintenance window

1. Quiesce writes and capture both a logical PostgreSQL backup and the
   installation's storage snapshots. Record the rollback image digests and
   configuration revision.
2. Scale every backend/API/worker process that connects directly to the AKB
   database to zero. Confirm `pg_stat_activity` has no other client backend.
3. Deploy or verify the selected Keycloak topology, its database, TLS,
   hostname, clients, signing profile, and back-channel route.
4. Run the rehearsed `prepare-upgrade` job with the candidate image and exact
   mounted configuration. Remove the temporary acknowledgement after success.
5. Start one backend replica. Require its bootstrap/read-back gate, `/livez`,
   `/readyz`, migration ledger, and authentication runtime boundary to be
   healthy before starting more replicas or dependent components.
6. Verify `/admin` through the recovery administrator. Save the generic OIDC
   upstream disabled, register AKB's displayed broker endpoint as an upstream
   Web redirect URI, then enable it.
7. Start ordinary login only from AKB `/auth`. Complete one canary identity
   before restoring full traffic.

## 6. Acceptance gates

The cutover is incomplete until all of these pass against the immutable
candidate:

- `/api/v1/auth/config` advertises only enabled SSO providers and no local
  login, registration, password-reset, or hidden fallback route is usable;
- product-admin login succeeds through `akb-admin` and a brokered ordinary
  user is rejected from that admin ceremony;
- Entra login reaches the exact Keycloak broker callback, AKB callback, and
  `/api/v1/auth/me` without exposing provider tokens to the browser;
- the canary resolves to the intended existing account or to an explicitly
  approved new account, with expected Vault roles;
- logout, refresh, idle/absolute expiry, back-channel logout, wrong
  issuer/audience, copied callback, and stale state behave fail-closed;
- representative existing PAT and service consumers still work, while local
  human credentials no longer do;
- collectors and companion products complete their own read/write smoke tests.

## 7. Rollback

To run a pre-epoch rollback image, stop every current backend and database
client and use the current image/config to run:

```text
python scripts/sso_session_epoch_preflight.py prepare-rollback
```

Only after that succeeds may the operator restore the old configuration
without `auth_runtime_generation`, `sso_session_epoch_upgrade`, and
`sso_session_epoch`, then start the frozen rollback image. No current browser
session survives. Retain the generation floor: a later re-upgrade must use a
greater generation.

Prefer this prepared application rollback over restoring a database snapshot
after post-cutover writes. Scale an installation-owned Keycloak down only
after AKB has returned to the old authority. Newly created SSO accounts are
not converted into local-password accounts by rollback; record and reconcile
them explicitly.
