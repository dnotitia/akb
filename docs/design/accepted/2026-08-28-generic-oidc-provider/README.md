---
status: accepted
stage: implementation
created: 2026-08-28
updated: 2026-08-28
---

# Generic OIDC Provider Control

## Context

AKB's first runtime-managed upstream contribution, `keycloak-oidc`, proves a
strict Keycloak-to-Keycloak broker profile. Its discovery checks intentionally
pin Keycloak endpoint paths and verified-email behavior. Applying that provider
type to another standards-based issuer fails even when Keycloak successfully
imports the discovery document.

OIDC discovery already supplies the issuer and protocol endpoints. Requiring a
new vendor-specific provider module for every standards-compatible issuer adds
product coupling without improving the ordinary configuration workflow.

## Decision

AKB adds a built-in `oidc` provider type for issuer-driven upstreams while
retaining `keycloak-oidc` unchanged for compatibility and its stricter profile.

The generic contribution:

- requires an exact HTTPS issuer and derives the standard discovery URL;
- rejects the AKB broker realm as its own upstream;
- requires the imported discovery issuer to exactly match configuration;
- accepts only HTTPS authorization, token, JWKS, and optional user-info,
  logout, and introspection endpoints;
- renders a bounded authorization-code profile with PKCE S256, JWKS signature
  validation, `client_secret_post`, fixed scopes, no upstream token storage,
  and disabled-by-default activation;
- fingerprints the validated issuer/discovery/endpoint set and fails read-back
  closed after out-of-band drift; and
- does not inherit provider-specific identity-migration capability.

The endpoint set may span HTTPS origins because standards-based providers can
separate identity, token, key, and user-info services. Product-admin authority
therefore remains identity-network-egress authority; deployments narrow that
authority with network policy rather than vendor branches in application code.

The generic profile trusts the selected issuer for email profile data. AKB
continues to resolve identities only by signed broker issuer and subject; email
and username are mutable profile/collision fields and never adopt or authorize
an existing account. A provider needing different client-authentication or
claim-trust semantics remains an explicit provider contribution.

## Compatibility

Existing managed `keycloak-oidc` representations and their marker remain
unchanged. Editing one preserves its provider type. New admin-form
configurations use `oidc` when the backend advertises it and fall back to the
first advertised type during a mixed-version rollout.

A different Keycloak realm is a valid generic upstream. The exact broker realm
is not, even when it runs on the same server.

## Acceptance criteria

- Microsoft Entra ID discovery configures and reads back disabled without a
  vendor-specific code branch.
- A distinct Keycloak realm configures through the same generic contribution.
- HTTP, malformed, issuer-mismatched, and endpoint-drifted configurations fail
  closed with value-less errors.
- Secrets remain write-only and absent from admin, public, audit, test, and log
  views.
- Existing Keycloak-specific provider behavior remains green.

Decision history and PM feedback are recorded in [`rounds/`](rounds/README.md)
and [`feedback/`](feedback/README.md).
