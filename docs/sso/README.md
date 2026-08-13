# SSO provider control

AKB uses Keycloak as its SSO broker in `auth_mode: sso`. The broker is an
identity boundary, not an AKB authorization database: AKB continues to own
accounts, administrator status, Vault roles, PATs, and service credentials.

The ordinary login page is mode-exclusive:

- `local` shows AKB login, registration, and local password recovery only;
- `sso` shows only enabled upstream providers returned by the versioned public
  provider catalog. It never offers a local-password fallback.

Product administration remains separate at `/admin`. A product administrator
can configure an upstream provider while it is disabled, inspect the exact
redirect URI, and then enable or disable it without redeploying AKB.

Every installation owns a positive `auth_runtime_generation`; SSO mode also
requires the non-secret UUID `sso_session_epoch`. Keep the exact
generation/mode/epoch tuple stable for normal restarts. Increase the generation
for every mode change or epoch rotation. PostgreSQL accepts only an exact
restart or a greater generation, so stale and same-generation conflicting
replicas cannot reverse `sso → local → sso` or restore an older epoch. Every
accepted transition transactionally purges ordinary and product-admin browser
sessions plus back-channel logout fences. Audit and startup output contain
transition booleans and row counts, never epoch or generation values.

## Pre-epoch upgrade and rollback

The first upgrade from a build without session epochs is deliberately
stop-the-world; it is not a rolling mixed-writer upgrade. The schema bridge
keeps `session_epoch` nullable so the old image remains usable after a prepared
rollback, while current code always writes and resolves an exact non-null
epoch. A database trigger rejects legacy NULL writes as soon as the current
authority is activated.

Upgrade in this exact order:

1. Stop every backend and other database client.
2. Configure `auth_runtime_generation: 1`, the installation epoch, and the
   temporary `sso_session_epoch_upgrade: stop-the-world-v1` acknowledgement.
3. From the new image's `backend/` directory, run
   `uv run python scripts/sso_session_epoch_preflight.py prepare-upgrade`.
   It fails while another client is connected, applies the bridge, purges all
   pre-epoch sessions/fences, and enables the legacy-write guard atomically.
   Ordinary application startup cannot activate a required bridge, even when
   the acknowledgement is present.
4. Remove the temporary acknowledgement and start only the new image with the
   exact prepared generation/mode/epoch tuple.

Rollback in this exact order:

1. Stop every current backend and other database client.
2. Using the current image and config, run
   `uv run python scripts/sso_session_epoch_preflight.py prepare-rollback`.
   It fails while another client is connected, purges all current SSO browser
   authority, preserves the monotonic generation floor, and reopens legacy
   NULL writes.
3. Remove `auth_runtime_generation`, `sso_session_epoch_upgrade`, and
   `sso_session_epoch` from the old image's configuration, then start the old
   image. No SSO browser session survives the rollback.

A later re-upgrade must use a generation greater than the last current-image
generation; the retained floor rejects replay. `status` prints only the
contract state and whether legacy rows exist, never authority values.

## Provider lifecycle

```text
not configured → configured_disabled → enabled
                         ↑                │
                         └──── disable ───┘
```

Configuration and activation are deliberately separate. Updating an enabled
provider is refused until it is disabled. Every mutation is read back from
Keycloak before AKB reports success, and a drifted representation enters
`configuration_error` rather than being advertised for login.

Client secrets are write-only. They may be supplied when a provider is created
or rotated, but are never returned by an AKB API, included in the public login
catalog, or written to audit metadata. Leaving the secret blank while editing
an existing disabled provider preserves Keycloak's stored value.

## Control ownership

The admin API reports one of two control modes:

- `direct`: this AKB installation has its realm-scoped management credential,
  so `/admin` can manage its own provider instances;
- `delegated`: a platform or deployment operator owns provider changes out of
  band. AKB does not attempt a management call or silently broaden its access.

The standalone SSO bundle provisions a dedicated `akb-sso-manager` service
account with only the Keycloak realm-management roles needed for provider
control and read-only user/prelink verification. It has no `manage-users`;
broker-user and federated-link mutations require a separate one-time Keycloak
operator. It does not retain the bootstrap credential.

## Built-in providers

- [Keycloak OIDC behind Keycloak](providers/keycloak-oidc.md) is the first
  reference contribution.

Additional OSS providers should follow [Adding a provider](adding-a-provider.md).
The registry is explicit and code-reviewed; AKB does not load arbitrary
Keycloak JSON or runtime Python plugins.

Ordinary browser sessions are server-custodied. The browser receives an opaque
HttpOnly AKB handle and a readable double-submit CSRF cookie; it never receives
a Keycloak access, refresh, or ID token and SSO never mints an AKB user JWT.
AKB encrypts refresh and ID tokens with an installation-owned AES-256-GCM key,
keeps access tokens only in memory for the current request, and refreshes under
a per-session database lock behind a bounded, connection-free admission gate.
The ordinary `akb-web` client maps Keycloak's `identity_provider` user-session
note into both token profiles. AKB requires that signed alias to equal the
provider selected in its one-time state, then retains and rechecks the alias in
the encrypted session envelope; `kc_idp_hint` alone is never an authorization
boundary.
An invalid refresh deletes the session; a
transient Keycloak outage rolls back without converting it into a revocation.
Production HTTPS uses `__Host-` cookie names with `Secure`, no `Domain`, and
`Path=/`; loopback HTTP uses separate development-only names.

The public provider catalog advertises a login URL only when the browser
session encryption key and exact Keycloak client profile are ready. Enabled
providers may still be listed with a null login URL during a staged rollout.
Logout deletes the local handle first and then performs best-effort Keycloak
revocation. Signed Keycloak back-channel logout revokes only sessions matching
the exact broker issuer and `sid` (and `sub` when present), and writes a
short-lived ordering fence so an already-started callback cannot recreate the
logged-out session.

Existing-account migration is an exact identity operation, not a normal
first-login convenience. See the reference provider's
[continuity runbook](providers/keycloak-oidc.md#existing-account-continuity).
AKB verifies an operator-created Keycloak prelink and adds the broker
`(issuer, sub)` to the same AKB user without changing PAT or Vault ownership.
Email and username never select the target account.

## Primary references

- [Keycloak Server Administration Guide: identity brokering](https://www.keycloak.org/docs/latest/server_admin/#_identity_broker)
- [Keycloak Admin REST API: identity-provider instances](https://www.keycloak.org/docs-api/latest/rest-api/index.html#_identity_providers)
