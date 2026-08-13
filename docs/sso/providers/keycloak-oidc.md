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
- scopes `openid profile email`;
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

The provider catalog does not advertise identity-migration support yet. Exact
old/new subject prelink and readiness are a separate migration slice; an IdP
being configurable is not evidence that existing AKB accounts were preserved.
