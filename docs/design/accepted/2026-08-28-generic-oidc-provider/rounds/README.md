# Decision rounds

## 2026-08-28 — Provider-specific versus issuer-driven control

The initial repair option was a Microsoft Entra-specific contribution with
tenant-pinned endpoint paths and client authentication. Review rejected that as
unnecessary vendor coupling for a protocol discovery problem.

The accepted revision adds one standards-based `oidc` contribution, keeps the
existing strict Keycloak profile intact, and uses Microsoft Entra ID plus a
distinct Keycloak realm as interoperability fixtures. Provider-specific code is
reserved for a real client-authentication or claim-policy difference that the
generic contract cannot state safely.
