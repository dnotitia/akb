# Keycloak OIDC boundary

**Status:** Phase 2 local verifier/admin provisioning active; ordinary SSO
browser session cutover staged unavailable

The accepted
[Authentication Mode Boundary](../../design/accepted/2026-08-13-authentication-mode-boundary/README.md)
and
[Workspace Account Governance](../../design/accepted/2026-07-10-workspace-account-governance/README.md)
are authoritative. This document records the current implementation shape.

## Route-selected token profiles

Human REST and delegated-human routes select exactly one verifier from the
canonical `auth_mode`:

- `local` selects `local-session-rs256-v2` only;
- `sso` selects `keycloak-access-v1` only.

`keycloak-access-v1` is a Keycloak 26-compatible RS256 access-token profile.
It pins the configured issuer and that issuer's JWKS, requires the selected
AKB resource audience, and validates the complete versioned claim policy
before account projection. API tokens additionally require `azp` to equal
`keycloak_client_id` or a configured companion client ID. The v1 API profile
does not add a speculative realm-specific API scope: issuer, audience,
authorized party, human token kind, and the complete claim policy are its
boundary.

MCP uses a distinct audience and route profile. It accepts token-store PAT or
service credentials in both modes, and may accept `keycloak-access-v1` only
when MCP OAuth is enabled. MCP scope enforcement remains authoritative and DCR
client IDs are not constrained by the static human API `azp` list. Local AKB
session JWTs are never MCP credentials.

No token header selects an algorithm, issuer, key source, or fallback
verifier. Token-supplied key URLs/material are rejected. An unknown `kid` may
cause one refresh from the same pinned JWKS endpoint and then fails.

`local-session-rs256-v2` is RS256 over an installation-owned RSA-3072 key.
The active private key and bounded public JWKS are explicit persistent
operator inputs; startup never creates ephemeral signing material. Tokens bind
an RFC 7638 `kid`, exact deployment issuer and API audience, JOSE type,
profile/token-use claims, `jti`, and numeric lifetime claims. The public-only
keyset is available from `/api/v1/auth/jwks` in local mode. The Phase 2 cutover
uses immediate reauthentication: the legacy HS256 verifier and issuance path
are absent, so an old session fails rather than trying another profile.

## Exact AKB account projection

Only a completely verified `VerifiedPrincipal` reaches account projection.
An existing account resolves exclusively through the exact `(issuer,
subject)` external-identity binding. Email and preferred username are mutable
profile data and uniqueness guards, never lookup, adoption, or linking keys.

In `open` enrollment, an unbound principal may atomically create one new AKB
human user and its exact identity binding. Existing email or username rows
cause an identity conflict regardless of `auth_provider`. In `invite_only`, an
exact prebound identity is required. Concurrent creation for the same subject
converges on one binding.

External JIT users are always non-admin, including the first user in an empty
database. OIDC realm/client roles never become AKB admin status or Vault
grants. Phase 2 owns explicit product-admin seeding for an exact identity.
`keycloak_link_by_email` remains readable only as a deprecated Phase 3
migration/readiness input; canonical runtime rejects `true`.

## Browser routes are staged unavailable

Phase 1 does not expose a browser token transport:

| Mode | `/auth/keycloak/login`, callback, exchange, logout |
| --- | --- |
| `local` | `404`; Keycloak may exist solely for MCP OAuth and is not called |
| `sso` | stable `503 browser_session_not_ready` before state, token exchange, projection, code redemption, or logout calls |

The former browser callback-to-exchange path and its AKB human-session minting
entry point are removed. The frontend callback page performs no network
exchange, token write, or SSO identity marker write. Phase 4 owns server-side
access/refresh token custody, browser session transport, refresh, logout, and
revocation before these routes can become available.

## Public capability schema v1

`GET /api/v1/auth/config` publishes only non-secret capabilities:

```json
{
  "schema_version": 1,
  "auth_mode": "sso",
  "local_auth": { "enabled": false },
  "keycloak": {
    "enabled": true,
    "browser_session_ready": false,
    "login_url": null
  },
  "mcp_oauth": { "enabled": false }
}
```

`local_auth.enabled` derives from `auth_mode`. `keycloak.enabled` means the
ordinary human SSO authority, so local mode with Keycloak enabled solely for
MCP still publishes it as `false`. Until Phase 4, browser readiness is false
and `login_url` is null. MCP OAuth remains orthogonal.

Clients accept only schema v1 with a known mode and internally consistent
capabilities. Fetch failure, non-success status, malformed JSON, old/missing
shape, unknown version/mode, or mode contradiction yields a deny-all
unavailable state; clients never infer local mode.
