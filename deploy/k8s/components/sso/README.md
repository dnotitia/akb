# SSO Kubernetes component

This reusable Kustomize component adds a Keycloak 26.7 broker, a dedicated
Keycloak PostgreSQL database, SSO ingress, and AKB auth patches. Public
profiles compose it over the canonical base; AKB and its PostgreSQL manifest
are not copied. It owns exactly one AKB realm and is not
the deployment shape for a managed tenant that reuses a platform-owned
Keycloak.

For a customized existing namespace, first follow the
[local-to-SSO cutover runbook](../../../../docs/sso/kubernetes-cutover.md). This
directory is a reference resource set, not an in-place migration patch: review
resource names, selectors, StatefulSets, PVCs, Ingress, and retained companion
workloads before applying a target-specific overlay.

The bundle implements the product-administrator bootstrap and recovery slice:

1. Keycloak creates one temporary master-realm service account on its first
   start.
2. The AKB bootstrap init container creates and reads back the `akb` realm,
   separate `akb-web`, `akb-admin`, and `akb-sso-manager` clients, the
   RSA-3072 active signing key, the signed broker-provenance mapper on
   `akb-web`, the native product administrator, and the exact AKB administrator
   projection.
3. It proves the permanent management credential, deletes the temporary
   bootstrap client, verifies that credential is rejected, and records a
   non-secret `bundled-keycloak-v3` retirement receipt in the AKB database.
4. A subsequent init-container run is read-only and succeeds without either
   original one-time value only when its current Keycloak and AKB identities
   exactly match that durable receipt.

Ordinary-user browser SSO uses server-side token custody: the browser receives
an opaque HttpOnly handle and readable double-submit CSRF value, while AKB
encrypts the Keycloak refresh/ID token set and never persists an access token.
The bootstrap maps the `identity_provider` user-session note into both ordinary
token profiles so AKB can bind each callback and refresh to the exact enabled
broker alias rather than trusting `kc_idp_hint`.
That custody becomes ready once its own configuration is complete — the
browser-session encryption key is the last piece an operator supplies — and
readiness is a property of that profile alone, never of the provider list.
Readiness is not a way in, though: a person still needs somewhere to sign in,
which is either an enabled upstream provider or this installation's own realm
(see [Sign in without an upstream provider](#sign-in-without-an-upstream-provider)).
`/admin` uses the dedicated confidential client and requires a fresh native
Keycloak password ceremony; a brokered upstream identity is not accepted as the
recovery administrator.

The browser-facing AKB origin and Keycloak-to-AKB back-channel are separate
deployment concerns. `keycloak_backchannel_logout_uri` is registered on the
`akb-web` client and defaults to the public AKB callback for simple installs.
This Kubernetes overlay sets it to the exact in-cluster backend Service URL so
Keycloak never tries to deliver a signed logout token to its own loopback
interface. Changing this client metadata on an existing standalone v2 install
requires a bounded client-update authority; the permanent provider manager is
intentionally unable to mutate clients.

## Required operator inputs

When using `deploy/k8s/profiles/standalone-sso/deploy.sh` or
`deploy/k8s/profiles/standalone-sso-secret-manager/deploy.sh`, provide these
public values as environment variables. The common deployer validates them and
replaces the public placeholders in one render, before anything is applied:

- `SSO_AKB_PUBLIC_URL` (for example `https://akb.example.com`)
- `SSO_KEYCLOAK_PUBLIC_URL` (for example `https://auth.akb.example.com`)
- `SSO_PRODUCT_ADMIN_USERNAME` and `SSO_PRODUCT_ADMIN_EMAIL`
- the immutable `akb-backend` and `akb-frontend` image references
- TLS issuer/secret names and storage classes as needed

An operator-specific Kustomize overlay may still patch those fields directly,
but it must be passed with `KUSTOMIZE_DIR` and use the same coherent origins.

The following Secrets are deliberately absent from Kustomize output. Create
them with a secret manager, Sealed Secrets, or `kubectl create secret`; never
commit their values.

| Secret | Required keys | Lifecycle |
|---|---|---|
| `akb-keycloak-db-credentials` | `POSTGRES_DB=keycloak`, `POSTGRES_USER=keycloak`, `POSTGRES_PASSWORD` | durable |
| `akb-secret` | `db_password`, `secret.yaml`, SSO runtime and stable platform projection keys | durable |
| `akb-keycloak-bootstrap` | `client-secret` | one-time |
| `akb-keycloak-upgrade` | `client-secret` | optional; one-time legacy-profile upgrade only |
| `akb-product-admin-bootstrap` | `password` | one-time |

The mounted `secret.yaml` must include the AKB database password, an
independent browser-session encryption key, and three independently generated
confidential-client secrets:

```yaml
db_password: <AKB PostgreSQL password; also projected as akb-secret/db_password>
system_hmac_secret: <independent random value>
sso_session_epoch: <installation-owned UUID>
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

The unified Secret Manager deployment creates these projections for manual,
bundled OpenBao, bundled HashiCorp Vault, and external Vault-compatible modes.
For a fresh isolated installation:

```bash
NAMESPACE=akb-sso-example \
SSO_AKB_PUBLIC_URL=https://akb.example.com \
SSO_KEYCLOAK_PUBLIC_URL=https://auth.akb.example.com \
SSO_PRODUCT_ADMIN_USERNAME=admin \
SSO_PRODUCT_ADMIN_EMAIL=admin@example.com \
SECRET_ENGINE=openbao \
SECRET_PROFILE=development \
REGISTRY=registry.example.com \
bash deploy/k8s/profiles/standalone-sso-secret-manager/deploy.sh
```

The SSO profiles automatically select this Kustomize tree. The Secret profile
first makes the complete contract ready, then the deployer applies AKB, the
dedicated Keycloak database, Keycloak, and both ingresses. It waits for both
databases and Keycloak before accepting the backend rollout. Reusing a KV path
created for local auth is rejected; authentication cutover is a separate
migration, not an implicit secret rewrite.

`sso_session_epoch` is not a credential. Generate it once with
`python -c 'import uuid; print(uuid.uuid4())'` and keep it stable across normal
restarts. The ConfigMap's positive `auth_runtime_generation` is also stable for
an exact restart. Increase that generation for every auth-mode transition or
epoch rotation. PostgreSQL rejects stale and same-generation conflicting
starts; an accepted greater generation removes ordinary and product-admin
browser handles plus back-channel logout fences before readiness.

### Upgrade from a pre-epoch backend

This first cutover is an explicit stop-the-world operation, not a rolling
upgrade. Use the new backend image for the preflight, in this exact order:

1. Scale the backend to zero and stop every other client of the AKB database.
2. Set `auth_runtime_generation: 1`, persist `sso_session_epoch`, and temporarily
   add `sso_session_epoch_upgrade: stop-the-world-v1` to `app.yaml`.
3. Run `cd backend && uv run python
   scripts/sso_session_epoch_preflight.py prepare-upgrade`. It exits nonzero if
   another database client is connected. On success it applies the compatible
   nullable-column bridge, purges pre-epoch sessions/fences, and enables the
   database legacy-write guard. Starting the application directly cannot
   activate a required bridge, even with the acknowledgement configured.
4. Remove `sso_session_epoch_upgrade`, then start only the new backend at the
   exact prepared generation/mode/epoch tuple.

For an image rollback, first scale the new backend to zero and stop every other
database client. With the new image and its current config, run `cd backend &&
uv run python scripts/sso_session_epoch_preflight.py prepare-rollback`. Only
after it succeeds, remove `auth_runtime_generation`,
`sso_session_epoch_upgrade`, and `sso_session_epoch` from the old image's
configuration and start the old backend. The preparation purges all current
SSO browser authority and reopens legacy writes while retaining a generation
floor; a later re-upgrade must use a greater generation. No browser session
survives rollback. The preflight `status` command reports only contract state
and legacy-row presence, never epoch/generation values or credentials.

Generate the browser-session key as an unpadded 32-byte base64url value:

```bash
python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
```

Keep the encryption key stable across backend restarts. Replacing this single
key makes ordinary-user envelopes unreadable and requires a fresh login; use
the epoch change above when the intended operation is a complete, auditable
ordinary/admin SSO-session revocation. Neither operation rotates a Keycloak
realm signing key.

Render and validate the public overlay before applying:

```bash
kubectl create namespace akb --dry-run=client -o yaml | kubectl apply -f -
kubectl kustomize --load-restrictor=LoadRestrictionsNone \
  deploy/k8s/profiles/standalone-sso > rendered-standalone-sso.yaml
kubectl apply --dry-run=client --validate=false \
  -f rendered-standalone-sso.yaml
```

Apply only after all durable and first-install one-time Secrets exist. Wait for the backend
pod's `bootstrap-standalone-sso` init container and main container to complete,
then inspect the init log. Its JSON report contains only IDs, key metadata, and
the exact role names; it never contains credential values.

## Sign in without an upstream provider

A bundled-Keycloak install has a realm of its own, and an installation that has
no external identity provider to broker to can use it as the place people
authenticate:

```yaml
sso_local_realm_login_enabled: true
sso_local_realm_display_name: "This workspace"
```

`/api/v1/auth/config` then offers one provider of type `local-realm` under the
reserved alias `local`, whose `login_url` is `/api/v1/auth/sso/local/login`; that
route redirects to this realm's own authorize endpoint with no broker hint
attached. Off by default, so an installation that
registers no provider keeps the behaviour it has rather than silently gaining a
login.

The two kinds are not interchangeable and neither accepts the other's shape. A
brokered sign-in carries the `identity_provider` claim and it must be present
and equal to the selected alias; a direct realm sign-in cannot carry it, because
nothing brokered it, and it must be absent. The claim never becomes optional:
that would let a token minted through one provider be presented for another.

Registering this realm as its own upstream provider is a different thing and
stays refused (`provider_issuer_is_broker`) -- it would be an authorize loop,
not a login.

This realm already holds a realm-local native identity: the product
administrator is one. So enabling this does not introduce a plane that was
absent; it decides that ordinary people also hold an account here rather than at
an upstream. Whoever arrives this way is a pending admission and still needs an
administrator's approval, exactly like anyone arriving through a broker.

## Upgrade an existing receipt

An installation that already recorded `bundled-keycloak-v1` retired its
original master-realm bootstrap client before the signed broker-provenance
mapper existed. A `bundled-keycloak-v2` installation has that mapper but records
the original public-origin back-channel callback. The permanent
`akb-sso-manager` deliberately lacks `manage-clients`, so a rollout that must
change either client resource cannot widen the manager's standing authority.

If a v2 installation keeps the same public callback (the effective setting is
still `https://<public-akb>/api/v1/auth/keycloak/backchannel-logout`), the init
container proves the exact v2 and v3 read-backs and promotes the durable receipt
without mutation or an upgrade credential. The v3 receipt retains that effective
callback, so a later callback change remains an exact, supported migration. It
reports `mode=upgrade-v2-to-v3-readback`. A v1 receipt always needs the one-time
authority to add the mapper. A v2 receipt needs it only when moving to a
different callback, including this overlay's in-cluster backend URL. A v3
receipt needs the same bounded authority when its recorded callback differs
from the newly configured callback.

Before rolling this version onto such an installation, create exactly one
temporary upgrade service account named `akb-bootstrap-upgrade-v2`. Keycloak's
supported recovery command requires every Keycloak node to be stopped. Follow
the upstream [temporary admin service account procedure](https://www.keycloak.org/server/bootstrap-admin-recovery)
and use the same database settings as the server. The opt-in
`legacy-profile-upgrade-job.yaml` captures those settings but is intentionally
not a Kustomize resource, so a fresh install never creates this authority. The
historical client ID `akb-bootstrap-upgrade-v2` remains stable so an interrupted
older migration can be recovered without creating a second identity.

For the default `akb` namespace, the bounded sequence is:

```bash
openssl rand -base64 48 | tr -d '\n' | kubectl -n akb create secret generic \
  akb-keycloak-upgrade \
  --from-file=client-secret=/dev/stdin
kubectl -n akb scale statefulset/keycloak --replicas=0
kubectl -n akb wait --for=delete pod -l app=akb-keycloak --timeout=180s
kubectl apply -f deploy/k8s/components/sso/legacy-profile-upgrade-job.yaml
kubectl -n akb wait --for=condition=complete \
  job/akb-keycloak-profile-upgrade-authority --timeout=180s
kubectl -n akb delete job akb-keycloak-profile-upgrade-authority
kubectl -n akb scale statefulset/keycloak --replicas=1
kubectl -n akb rollout status statefulset/keycloak --timeout=300s
```

Then deploy the new AKB overlay. Its init container must report
`mode=upgrade-v1-to-v3`, `mode=upgrade-v2-to-v3`, or
`mode=upgrade-v3-callback` and
`receipt_profile=bundled-keycloak-v3`. The lifecycle first validates the exact
receipt-bound source read-back using the permanent manager, reconciles only the
`akb-web` client metadata and, when required, signed broker-provenance mapper,
revalidates the complete
v3 profile through the permanent manager, deletes the temporary upgrade client,
proves both its old and newly requested tokens are rejected, and only then
writes or compare-and-swaps the v3 receipt. Retrying after a partial client
update accepts only the exact receipt source or exact target metadata, so the
process is convergent without accepting arbitrary drift. It does not require
or reset the existing product-admin password.

Delete `akb-keycloak-upgrade` only after that report and a subsequent
`mode=readback` restart succeed. If the init container reports
`keycloak_upgrade_credential_required`, do not grant `manage-clients` to
`akb-sso-manager`; complete this one-time procedure instead. A failed migration
deliberately leaves the temporary authority available for repair. If deletion
succeeds but the v3 receipt write does not, use the same official recovery
procedure to create the exact upgrade client again before retrying.

## Retire one-time material

The init container deletes the temporary Keycloak client only after all
durable recovery paths and the AKB administrator projection have been read
back. Its successful report also means the exact retirement receipt was
written and read back from AKB PostgreSQL. After that report succeeds:

1. Preserve the product-admin password through an approved installer handoff;
   the administrator needs it for the forced first-login password change.
2. Replace both first-install Secret values with fresh, unrelated retirement
   sentinels. Keep
   the Secret objects because the default first-boot manifest deliberately
   requires them; this prevents an empty Keycloak database from initializing
   without any recovery credential.
3. Restart the Keycloak StatefulSet so the original bootstrap value is no
   longer in its process environment.
4. Restart the backend Deployment. Its init container must complete in
   `readback` mode using only `akb-sso-manager`.
5. Confirm `/admin` can complete native login and that the ordinary local
   login/register endpoints remain unavailable.

Do not remove or replace either first-install one-time value before the first successful
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
