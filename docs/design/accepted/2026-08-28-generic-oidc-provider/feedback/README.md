# PM feedback

## 2026-08-28

- Prefer issuer/discovery-driven OIDC configuration over adding a provider
  implementation for every vendor.
- Generic OIDC must also support a distinct upstream Keycloak realm.
- The broker realm must not be allowed to point to itself as an upstream.
- Keep configuration centered on issuer, client ID, and write-only secret
  rather than exposing vendor selection as the primary workflow.
