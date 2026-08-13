# Standalone SSO Kubernetes bundle

This overlay adds a Keycloak 26.7 broker and a dedicated Keycloak PostgreSQL
database to the standalone AKB stack. It owns exactly one AKB realm and is not
the deployment shape for a managed tenant that reuses a platform-owned
Keycloak.

The bundle implements the product-administrator bootstrap and recovery slice:

1. Keycloak creates one temporary master-realm service account on its first
   start.
2. The AKB bootstrap init container creates and reads back the `akb` realm,
   separate `akb-web`, `akb-admin`, and `akb-sso-manager` clients, the
   RSA-3072 active signing key, the native product administrator, and the exact
   AKB administrator projection.
3. It proves the permanent management credential, deletes the temporary
   bootstrap client, verifies that credential is rejected, and records a
   non-secret retirement receipt in the AKB database.
4. A subsequent init-container run is read-only and succeeds without either
   original one-time value only when its current Keycloak and AKB identities
   exactly match that durable receipt.

Ordinary-user browser SSO uses server-side token custody: the browser receives
an opaque HttpOnly handle and readable double-submit CSRF value, while AKB
encrypts the Keycloak refresh/ID token set and never persists an access token.
It becomes ready only after the browser-session encryption key is supplied and
an upstream provider is enabled. `/admin` uses the dedicated confidential
client and requires a fresh native Keycloak password ceremony; a brokered
upstream identity is not accepted as the recovery administrator.

## Required operator inputs

Patch these public values in an operator overlay before applying:

- `akb.example.com` in the AKB Ingress and `akb-app-config`
- `auth.akb.example.com` in the Keycloak Ingress, `KC_HOSTNAME`, and
  `akb-app-config`
- `product-admin-username` and `product-admin-email` in
  `akb-sso-bootstrap-config`
- the immutable `akb-backend` and `akb-frontend` image references
- TLS issuer/secret names and storage classes as needed

The following Secrets are deliberately absent from Kustomize output. Create
them with a secret manager, Sealed Secrets, or `kubectl create secret`; never
commit their values.

| Secret | Required keys | Lifecycle |
|---|---|---|
| `akb-postgres-credentials` | `POSTGRES_DB=akb`, `POSTGRES_USER=akbuser`, `POSTGRES_PASSWORD` | durable |
| `akb-keycloak-db-credentials` | `POSTGRES_DB=keycloak`, `POSTGRES_USER=keycloak`, `POSTGRES_PASSWORD` | durable |
| `akb-secret-config` | `secret.yaml` | durable |
| `akb-keycloak-bootstrap` | `client-secret` | one-time |
| `akb-product-admin-bootstrap` | `password` | one-time |

The mounted `secret.yaml` must include the AKB database password, an
independent browser-session encryption key, and three independently generated
confidential-client secrets:

```yaml
db_password: <same value as akb-postgres-credentials>
system_hmac_secret: <independent random value>
sso_browser_session_encryption_key: <independent 32-byte base64url value>
keycloak_client_secret: <akb-web secret>
keycloak_admin_client_secret: <akb-admin secret>
keycloak_management_client_secret: <akb-sso-manager secret>
embed_api_key: <provider key, if required>
```

Generate each credential independently. The product-admin password is
temporary, must be at least 12 characters, must differ from its username and
email, and Keycloak forces `UPDATE_PASSWORD` on first login. The realm enforces
the same lean policy for the replacement password. The bootstrap client secret
is not the product-admin password.

Generate the browser-session key as an unpadded 32-byte base64url value:

```bash
python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
```

Keep it stable across backend restarts. Replacing this single key intentionally
invalidates every outstanding ordinary-user SSO browser session and requires a
fresh login; it does not rotate a Keycloak realm signing key.

Render and validate the public overlay before applying:

```bash
kubectl kustomize --load-restrictor=LoadRestrictionsNone \
  deploy/k8s/standalone-sso > rendered-standalone-sso.yaml
kubectl apply --dry-run=client --validate=false \
  -f rendered-standalone-sso.yaml
```

Apply only after all durable and one-time Secrets exist. Wait for the backend
pod's `bootstrap-standalone-sso` init container and main container to complete,
then inspect the init log. Its JSON report contains only IDs, key metadata, and
the exact role names; it never contains credential values.

## Retire one-time material

The init container deletes the temporary Keycloak client only after all
durable recovery paths and the AKB administrator projection have been read
back. Its successful report also means the exact retirement receipt was
written and read back from AKB PostgreSQL. After that report succeeds:

1. Preserve the product-admin password through an approved installer handoff;
   the administrator needs it for the forced first-login password change.
2. Replace both Secret values with fresh, unrelated retirement sentinels. Keep
   the Secret objects because the default first-boot manifest deliberately
   requires them; this prevents an empty Keycloak database from initializing
   without any recovery credential.
3. Restart the Keycloak StatefulSet so the original bootstrap value is no
   longer in its process environment.
4. Restart the backend Deployment. Its init container must complete in
   `readback` mode using only `akb-sso-manager`.
5. Confirm `/admin` can complete native login and that the ordinary local
   login/register endpoints remain unavailable.

Do not remove or replace either one-time value before the first successful
report. The Secret references are required on first boot because Keycloak's
startup bootstrap settings are ignored after the master realm exists. An
absent value without a matching retirement receipt fails closed; it is never
treated as proof that the client was deleted. A custom steady-state overlay may
remove the two mounts and the Keycloak bootstrap environment entries, and then
delete the Secret objects, but only after the receipt-backed report succeeds.
A partial run before deletion deliberately leaves the temporary service account
available so the same inputs can repair the installation. A crash after
Keycloak accepted the delete but before PostgreSQL committed the receipt also
fails closed. For that rare recovery case, or if both temporary and permanent
credentials are lost, stop every Keycloak node and follow Keycloak's official
`bootstrap-admin service` recovery procedure before rerunning convergence; do
not add a permanent master administrator to this bundle.

## Signing-key rotation and rollback

The bootstrap makes an RSA-3072/RS256 provider active and requires at least one
other enabled RS256 signing key to remain published. Inspect the realm's
`/admin/realms/akb/keys` read-back before and after every rotation; the active
`kid` must resolve to a 3072-bit signing key and the previous `kid` must remain
in JWKS for at least the maximum issued-token lifetime plus verifier cache
overlap.

For a planned rotation, add a newly generated RSA-3072/RS256 provider at a
higher priority, verify token issuance and AKB validation against its new
`kid`, and only then demote the prior provider to passive. Roll back by making
the still-retained prior RSA-3072 provider active again. Do not use the bundled
Keycloak default RSA-2048 key as the active rollback target, delete a passive
private key while its tokens may still exist, or rotate Keycloak and AKB
verifier configuration in one unverified step.

Keycloak and both database images are digest-pinned. Candidate validation must
also replace both AKB application tags with immutable digests; `:latest` is
only the generic base's operator placeholder.
