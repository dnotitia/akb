# Keycloak OIDC behind Keycloak

Provider type: `keycloak-oidc`

This integration brokers an upstream Keycloak realm through the Keycloak realm
owned by an AKB SSO installation. The two issuers must be distinct. They may be
on separate servers or separate realms, but the upstream issuer cannot be the
AKB broker realm itself.

## Upstream client

Create a confidential OpenID Connect client in the upstream realm with:

- standard authorization code flow enabled;
- implicit flow, direct access grants, and service accounts disabled;
- PKCE method `S256`;
- scopes `openid profile email` and the Keycloak built-in `basic` default
  client scope so access tokens contain the canonical `sub` claim;
- the exact redirect URI shown by AKB after the provider is saved.

The redirect URI has this Keycloak broker shape:

```text
https://<broker-host>/realms/<akb-realm>/broker/<alias>/endpoint
```

Both the browser and AKB's broker must be able to reach the configured issuer.
Use the issuer published by the upstream realm, not an admin-console URL or a
server-internal hostname that produces a different `iss` claim.

## Configure in AKB

1. Sign in at `/admin` with the product-administrator identity.
2. Under **SSO providers**, enter a stable alias, the user-facing button label,
   upstream issuer, client ID, and client secret.
3. Save the disabled configuration.
4. Copy the read-back redirect URI into the upstream client's exact redirect
   URI allowlist if it was not registered already.
5. Enable the provider.

AKB derives the discovery URL as
`<issuer>/.well-known/openid-configuration`. The imported document supplies
only allowlisted OIDC endpoints. Because this provider type is specifically
Keycloak, its authorization, token, JWKS, user-info, logout, and introspection
endpoints must be the standard paths beneath that exact issuer; metadata that
redirects broker back-channel traffic to another origin is rejected. The
resulting broker profile validates
signatures through JWKS, requires the exact issuer, uses
`client_secret_basic`, requests `openid profile email`, enables PKCE S256, and
starts disabled and hidden.

Saving a provider asks the broker Keycloak to fetch that HTTPS discovery URL.
Treat product-admin access as authority to configure identity-network egress,
and enforce the installation's approved IdP destinations with DNS, firewall,
service-mesh, or Kubernetes NetworkPolicy controls. Do not grant `/admin` to a
tenant that must not choose those destinations; use delegated platform control
for that topology.

AKB deliberately leaves `trustEmail`, upstream-token storage, and read-token
role creation disabled. Keycloak's first-broker-login flow may create its
internal broker user, but that does not grant an AKB role or administrator
status. AKB account projection and authorization remain separate and exact.

To rotate the upstream client secret, disable the provider, edit it with the
new secret, save, and enable it again. Leaving the secret field blank preserves
the existing value; no UI or API can read it back.

## Existing-account continuity

Changing from an upstream realm to an AKB broker changes the trusted identity
from `(upstream issuer, upstream sub)` to `(broker issuer, broker sub)`. Do not
join those identities by email or username. For each existing account:

1. Disable the provider and keep it hidden from ordinary login.
2. Resolve the existing AKB `users.id` from its exact upstream issuer and
   subject. Local-only, missing, inactive, service, or conflicting accounts
   require manual resolution.
3. Using a one-time Keycloak operator credential, create a native,
   passwordless broker user and attach the exact federated identity with
   `identityProvider=<alias>` and `userId=<upstream sub>`.
4. Call the authenticated preflight endpoint with the provider alias, existing
   AKB user ID, upstream subject, and broker subject. AKB derives both issuers
   server-side, verifies the disabled managed provider, native enabled broker
   user, absence of every local credential or credential-registration required
   action, and exactly one federated link to the selected provider, then
   verifies the old AKB binding without writing.
5. Call `identity-migrations/apply` with the same body and product-admin CSRF
   header. AKB adds only the broker identity row to the same user. It does not
   update the user profile, adopt by email, revoke a PAT, or move a Vault role.
6. Read back `state=linked`, then enable the provider. Keep the old binding for
   the rollback window.

The relevant paths are:

```text
POST /api/v1/admin/sso/providers/<alias>/identity-migrations/preflight
POST /api/v1/admin/sso/providers/<alias>/identity-migrations/apply
POST /api/v1/admin/sso/providers/<alias>/identity-migrations/rollback
```

All three accept this shape; callers do not supply either issuer:

```json
{
  "existing_user_id": "<canonical AKB user UUID>",
  "upstream_subject": "<opaque upstream sub>",
  "broker_subject": "<opaque broker user UUID/sub>"
}
```

Apply and rollback require the provider to remain disabled and use the SSO
product-admin double-submit CSRF boundary. Operations are idempotent and audit
only subject digests at the admin API boundary. The domain event records the
exact old/new identity so an operator can reconcile the transaction.

For rollback, disable the provider first, call the AKB rollback endpoint and
read back `state=ready_to_link`, then use the one-time Keycloak operator to
remove the federated link and broker user. The permanent `akb-sso-manager`
credential intentionally cannot perform these user mutations.

## Least-privilege split

The permanent management service account has only these realm-management role
and client-scope mappings:

```text
manage-identity-providers
query-clients
query-users
view-clients
view-realm
view-users
```

It has no `manage-users`. Provider lifecycle and exact prelink read-back are
available at runtime; broker user creation, federated-link mutation, and final
cleanup stay explicit one-time Keycloak operator actions.

Run `deploy/keycloak-dev/broker-chain/run.sh` before enabling a release. The
disposable two-Keycloak fixture completes Authorization Code + PKCE login,
projects the broker access token to the same AKB user, proves PAT and Vault
continuity, rejects an upstream token even when it carries the AKB API
audience, then exercises rollback and operator cleanup.
