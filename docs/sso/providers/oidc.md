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

### Redirect-registration ownership

AKB and its Keycloak broker derive and display the callback, but they do not
modify the upstream client registration. Registering that URI remains an
upstream-application administrator action. Automatic registration would
require provider-management credentials that are unrelated to OIDC login and
would make the generic provider depend on a vendor control plane.

One upstream client may allow several exact Web redirect URIs. Register one
for every public broker host, realm, and provider alias that will use that
client; a Kubernetes namespace name by itself does not determine the URI.
For example, a test and production installation can coexist in one client:

```text
https://<test-broker-host>/realms/<realm>/broker/<alias>/endpoint
https://<production-broker-host>/realms/<realm>/broker/<alias>/endpoint
```

The same client ID and credential then authorize every listed environment.
Separate upstream clients are preferred when test and production should have
independent credentials, consent, ownership, rotation, and incident scope.
Remove retired-environment URIs promptly. Wildcards, a guessed AKB API
callback, and a URI copied from another alias are not substitutes for the
displayed value.

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

Use the tenant-specific issuer rather than `common` or `organizations` when
the AKB installation is intended for one workforce tenant.

In the Microsoft Entra admin center:

1. Open **Identity → Applications → App registrations**, then select the
   application whose Application (client) ID is configured in AKB.
2. Open **Authentication → Add a platform → Web**. Do not register the
   Keycloak server callback as a Single-page application.
3. Paste the exact redirect URI shown by AKB after the provider is saved
   disabled, then save the platform configuration. Additional environments
   are additional Web redirect URIs on the same application, or separate app
   registrations when isolation is required.
4. Under **Certificates & secrets**, give AKB the client secret **Value** at
   creation time. The Secret ID is metadata and cannot authenticate a client.
5. Ensure the workforce profile supplies an email claim before using open
   enrollment. Email is profile/admission data only; AKB still keys identity
   by the signed broker issuer and subject and never adopts an existing user
   from an email match.

The standard `openid profile email` login does not require AKB to hold a
Microsoft Graph management credential. Tenant policy may still require an
administrator to grant consent for the application's requested scopes.

After Entra has accepted the redirect URI, return to AKB `/auth` and start the
login from the rendered provider button. Opening the Microsoft authorize URL
or the Keycloak broker callback directly does not create AKB's one-time state.

Common diagnostics:

- `AADSTS900971: No reply address provided` means the app registration has no
  usable Web reply address for this flow. Add the displayed broker endpoint.
- `AADSTS50011` means the request's redirect URI does not exactly match a URI
  registered on that application. Compare scheme, host, realm, alias, path,
  and trailing slash.
- `Missing state parameter in response from identity provider` usually means
  a callback or authorize endpoint was opened directly. Restart at AKB
  `/auth`; do not reuse an old callback URL.

Microsoft references:

- [Register an application and configure a Web redirect URI](https://learn.microsoft.com/en-us/graph/auth-register-app-v2)
- [Redirect URI restrictions and multiple-environment guidance](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url)
