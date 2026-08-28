# Generic OIDC behind Keycloak

Provider type: `oidc`

This integration brokers a standards-based OpenID Connect issuer through the
Keycloak realm owned by an AKB SSO installation. It is issuer-driven rather
than vendor-driven: Microsoft Entra ID, another Keycloak realm, and other
compatible providers use the same contribution.

The upstream issuer must be distinct from the AKB broker realm. Another realm
on the same Keycloak server is valid; pointing the broker realm back at itself
is rejected because it creates a circular trust and login loop.

## Upstream client

Create a confidential OIDC client with:

- authorization code flow enabled;
- a client secret and `client_secret_post` token-endpoint authentication;
- PKCE method `S256`;
- scopes `openid profile email`;
- the exact broker redirect URI shown after AKB saves the provider disabled.

The redirect URI has this shape:

```text
https://<broker-host>/realms/<akb-realm>/broker/<alias>/endpoint
```

If the upstream supports RP-initiated logout, also register the displayed
logout response URI where that provider requires an allowlist:

```text
https://<broker-host>/realms/<akb-realm>/broker/<alias>/endpoint/logout_response
```

## Configure in AKB

1. Sign in at `/admin` with the product-administrator identity.
2. Enter a stable alias, the user-facing button label, exact HTTPS issuer,
   client ID, and client-secret value.
3. Save the disabled configuration.
4. Copy the read-back redirect URI into the upstream client registration.
5. Enable the provider only after the redirect allowlist is exact.

AKB derives `<issuer>/.well-known/openid-configuration` and asks its Keycloak
broker to import it. The discovery document's issuer must exactly equal the
configured issuer. Authorization, token, JWKS, and optional user-info, logout,
and introspection endpoints must be HTTPS. AKB copies only those allowlisted
fields into a fixed broker profile and stores a SHA-256 fingerprint of the
validated set; an endpoint changed out of band enters `configuration_error`
instead of remaining available for login.

Discovery endpoints are allowed to use different HTTPS origins because OIDC
providers legitimately split services (for example, an identity authority and
a separate user-info service). Product-admin access therefore includes
authority to select identity-network egress. Enforce installation-approved
destinations with DNS, firewall, service mesh, or Kubernetes NetworkPolicy when
that authority must be narrower.

The generic profile treats the selected issuer as the authority for its email
profile. AKB still resolves identity only by the signed broker `(issuer, sub)`;
email and username are mutable profile and collision fields and never select or
adopt an existing account. Providers used with open enrollment must supply an
email claim. Use a provider-specific contribution when an organization needs a
different client-authentication or claim-trust policy rather than making the
generic profile silently guess.

## Microsoft Entra ID example

For a single-tenant workforce application, use:

```text
Issuer:    https://login.microsoftonline.com/<tenant-id>/v2.0
Client ID: <Application (client) ID>
Secret:    <client secret Value, not Secret ID>
```

Register the broker endpoint as a **Web** redirect URI. Use the tenant-specific
issuer rather than `common` or `organizations` when the AKB installation is
intended for one workforce tenant.
