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
