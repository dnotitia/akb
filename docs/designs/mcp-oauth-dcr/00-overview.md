# MCP OAuth + DCR — Resource Server, delegated AS

**Status**: resource-server and AKB browser-session boundaries implemented;
companion-product cutover remains a release gate
**Started**: 2026-06-25
**Related**: [`keycloak-oidc/00-overview.md`](../keycloak-oidc/00-overview.md) — current route-selected verifier and browser-session boundary

## Statement

AKB gains an **optional** OAuth 2.1 path so that web-hosted LLM clients
(claude.ai Custom Connectors, ChatGPT Custom Connectors, future Gemini
Connectors) can register themselves against an AKB deployment and call
`/mcp` with an access token instead of a Personal Access Token.

AKB serves as an **OAuth Resource Server (RS) only**. The Authorization
Server (AS) — including Dynamic Client Registration (RFC 7591),
`/authorize`, consent UI, `/token`, PKCE, refresh rotation — is
**delegated to a standards-compliant OIDC provider** (Keycloak in
Dnotitia's reference deployment; any DCR-capable OIDC IdP works).

When the operator does not configure an IdP, nothing changes — `/mcp`
keeps accepting `Bearer akb_<PAT>` only, and stdio-based clients
(Claude Desktop, Codex CLI via the `akb-mcp` proxy) work unchanged.

## Why Resource Server only (the core constraint)

Implementing a full OAuth 2.1 AS inside AKB would mean:

- `/.well-known/oauth-authorization-server`
- `POST /register` (DCR) + abuse mitigation (rate limit, software
  statement, optional Protected DCR via initial access tokens)
- `GET /authorize` + a hosted consent UI ("Claude wants to access
  vault X with scopes Y, Z")
- `POST /token` + PKCE enforcement + refresh token rotation
- Token revocation (RFC 7009) + introspection (RFC 7662)
- JWKS publication + key rotation

This is 1–2 KLOC of carefully reviewed security-sensitive code, and an
attack surface AKB is not in a position to harden faster than a
mainstream IdP. The AS surface area is also where the catastrophic
OAuth bugs live (consent bypass, redirect URI manipulation, PKCE
downgrade, refresh replay, open registration abuse). Keycloak — and
Auth0/Okta/etc. — have spent a decade specifically maturing this
surface.

By contrast, the Resource Server role is small and well-bounded:

1. Validate the inbound JWT (signature against IdP JWKS, `iss`, `aud`,
   `exp`).
2. Map the JWT subject to an internal AKB `users.id`.
3. Enforce the existing PG-native RBAC.

Steps 1 and 2 are implemented in
[`backend/app/services/keycloak_oidc.py`](../../backend/app/services/keycloak_oidc.py)
and the common verified-principal projector. REST and MCP select distinct
profiles before parsing any untrusted JOSE metadata.

## Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| AKB role in OAuth | Resource Server only | Keep AKB out of consent/registration/token-rotation security surface. |
| AS provider | Any RFC 7591-capable OIDC IdP (reference: Keycloak) | Already integrated; swappable per deployment. No code dep on Keycloak specifically. |
| DCR handling | Delegated to IdP entirely | AKB advertises the IdP's registration endpoint via `oauth-protected-resource` metadata; clients register there directly. |
| Identity mapping | Reuse the verified-principal account projection used by SSO | Exact issuer/subject projection stays shared after the route-selected verifier succeeds. |
| Default state | OAuth path disabled | When `oidc.enabled = false`, `/mcp` still accepts PAT only — current behavior preserved bit-for-bit. |
| Scope model | `akb:vault:read`, `akb:vault:write` (vault-wide); finer grain deferred | Matches existing vault-level role grants. Per-collection scopes are not in OAuth — they stay in AKB's authorization layer. |
| PAT compatibility | `/mcp` accepts namespaced PAT/service credentials and, when enabled, `keycloak-access-v1` | No breaking change for stdio/CLI users; local human-session JWTs are not MCP credentials. |
| Consent UI | Hosted by IdP, not AKB | AKB does not render or own the consent screen. Scope names and i18n strings live in the IdP realm config. |
| Audience claim | AKB validates `aud` includes its `resource` identifier | Prevents tokens issued for other RSes (or for the SPA itself) from being usable at `/mcp`. |

## What AKB ships

### 1. Protected-resource metadata endpoint

Per RFC 9728 (OAuth 2.0 Protected Resource Metadata). New file
`backend/app/api/routes/oauth_metadata.py`:

```http
GET /.well-known/oauth-protected-resource

200 OK
{
  "resource": "https://akb.example.com/mcp",
  "authorization_servers": ["https://kc.example.com/realms/akb"],
  "scopes_supported": ["akb:vault:read", "akb:vault:write"],
  "bearer_methods_supported": ["header"],
  "resource_documentation": "https://akb.example.com/docs/mcp"
}
```

When `oidc.enabled = false`, this endpoint returns `404 Not Found`. No
`authorization_servers` advertised means clients fall through to
PAT-only behavior.

### 2. MCP handler accepts its declared credential classes

The MCP HTTP boundary and tool-request context use the same explicit resolver.
Pseudocode of the boundary:

```python
auth_header = request.headers.get("authorization", "")
token = auth_header.removeprefix("Bearer ").strip()

user = await resolve_mcp_authorization(auth_header)
# The trusted MCP capability selects either token-store resolution for an
# akb_ credential or keycloak-access-v1 for the MCP audience. It never selects
# a verifier from an untrusted JOSE alg and never tries a local-session profile.

if not user:
    return _unauthorized_with_resource_metadata_hint()
```

The `WWW-Authenticate` response header on 401 includes a
`resource_metadata=` parameter (RFC 9728 §5) pointing clients at the
`.well-known` URL above so they can complete discovery.

### 3. Explicit MCP authorization resolver

`resolve_mcp_authorization` selects token-store resolution for namespaced
`akb_` credentials or, only when MCP OAuth is enabled, the pinned
`keycloak-access-v1` MCP profile:

```python
user = await resolve_mcp_authorization(authorization_header)
```

Only a completely verified principal reaches the common exact
`(issuer, subject)` account projector. There is no algorithm dispatch,
verifier fallback, or local-session JWT acceptance on MCP.

### 4. Audience constant

`MCP_RESOURCE = f"{settings.public_url}/mcp"` exposed from config so the
JWKS verifier can enforce `aud` matches. Tokens issued for the human API
resource MUST NOT be usable at `/mcp`, and vice versa.

### 5. Scope enforcement (optional in v1)

A small middleware that reads `scope` claim and maps to required
operation:

| MCP tool family | Required scope |
|---|---|
| `akb_search`, `akb_get`, `akb_browse`, `akb_grep`, `akb_drill_down`, `akb_history`, `akb_relations`, `akb_graph` | `akb:vault:read` |
| `akb_put`, `akb_update`, `akb_delete`, `akb_edit`, `akb_link`, `akb_unlink`, `akb_create_collection`, `akb_create_table`, `akb_alter_table`, `akb_publish`, `akb_unpublish`, file tools | `akb:vault:write` |

The table is per-tool, but the requirement is per-**call**: `akb_grep` is
read-grade only without its `replace` argument, which rewrites every
matching document. `_ARG_WRITE_TRIGGERS` (`mcp_server/server.py`) declares
such arguments and `_required_scope(name, args)` promotes the call to
`akb:vault:write` when one is present.

If neither scope is present, return `403 insufficient_scope`. The
existing PG-RBAC layer still gates per-vault access; scopes are an
additional coarse gate that the IdP can present at consent time.

## What the operator configures (Keycloak example)

This is **realm configuration**, not AKB code:

- Realm: `akb` (the existing realm from the `keycloak-oidc` design)
- Client scopes:
  - `akb:vault:read` — consent text: *"Read your AKB vaults"*
  - `akb:vault:write` — consent text: *"Create, edit, and delete AKB content"*
- DCR: enable "Trusted Hosts" policy (default Keycloak DCR is open;
  Trusted Hosts limits which client `redirect_uris` are accepted at
  registration time — set to `claude.ai`, `chat.openai.com`, etc.)
- Optional: Initial Access Token requirement (Protected DCR) for hostile
  internet exposure. Mint a one-shot token per partner.
- Optional: client policies forcing PKCE S256 on all dynamically
  registered clients.

Recommended: ship `deploy/k8s/internal/keycloak-realm-akb.json` with the
above scopes + policies pre-defined so operators only need to import it.

## Flow

### Discovery + DCR (one time per client)

```
[claude.ai backend]
    GET https://akb.example.com/.well-known/oauth-protected-resource
        → { authorization_servers: ["https://kc.example.com/realms/akb"], ... }

    GET https://kc.example.com/realms/akb/.well-known/openid-configuration
        → { registration_endpoint: ".../clients-registrations/openid-connect",
            authorization_endpoint, token_endpoint, jwks_uri, ... }

    POST .../clients-registrations/openid-connect
        Body: { "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/oauth/callback"],
                "token_endpoint_auth_method": "none",      # public client
                "grant_types": ["authorization_code","refresh_token"] }
        → { "client_id": "kc-generated-uuid", ... }
```

AKB never sees any of this. AKB code did not run.

### User authorization (per user, once per token lifetime)

```
[Claude UI]  user clicks "Connect AKB"
             → Claude redirects browser to Keycloak /authorize
                 with PKCE, scope="akb:vault:read akb:vault:write",
                 audience=https://akb.example.com/mcp

[Keycloak]   user logs in (or uses existing SSO session)
             consent screen lists the two scopes with the configured
             human-readable text
             → 302 back to claude.ai/api/oauth/callback?code=…

[Claude backend]
             POST kc/.../token  (code + verifier)
             → { access_token (JWT, aud=akb-mcp), refresh_token, expires_in }
```

### MCP request (every tool call)

```
[Claude]     POST https://akb.example.com/mcp
             Authorization: Bearer <jwt>
             body: { "method": "tools/call", "params": { "name": "akb_search", ... } }

[AKB /mcp]   MCP capability + mcp_oauth.enabled → keycloak-access-v1(access token, aud=MCP_RESOURCE)
             fixed verifier: JWKS sig + iss + aud + exp + scope contains akb:vault:read
             project exact (claims.iss, claims.sub) → users.id
             SET LOCAL ROLE akb_user_<uid>          (existing PG-RBAC path)
             dispatch tool
             → 200
```

## What this design explicitly does not do

- **Does not implement an AS inside AKB.** No `/register`, `/authorize`,
  `/token`, consent UI, refresh rotation, or JWKS publication is added.
- **Does not change PAT/service behavior.** Current stdio/CLI/Desktop token-store
  flows continue to work. Those credentials coexist with the MCP-audience
  Keycloak access-token profile on `/mcp`; AKB local-session JWTs do not.
- **Does not provide a browser SSO transport.** Local mode retains the legacy
  local-session login. In SSO mode, browser routes are fail-closed until Phase
  4 supplies server-side token custody; MCP OAuth remains exclusively the
  third-party MCP-client path.
- **Does not add per-collection scopes.** OAuth scope vocabulary stays
  coarse (`vault:read` / `vault:write`); fine-grained access continues
  to live in AKB's PG-RBAC + `vault_access` table.
- **Does not solve the "internal-only AKB + cloud LLM" reachability
  problem.** If AKB is not reachable from claude.ai's egress, OAuth
  cannot help — operator must expose via tunnel or public hostname. See
  the connector reachability matrix in the project memory.

## Open questions (resolve before implementation)

1. **Resource Indicators (RFC 8707).** Should AKB require clients to
   pass `resource=https://akb.example.com/mcp` on the token request so
   Keycloak can mint a narrow-audience token? Strong yes if Keycloak
   version supports it; defer if it forces a Keycloak upgrade.
2. **Token introspection vs. JWT-only.** Pure JWT validation is simpler
   and stateless. Introspection would let AKB see live revocation but
   adds a hop per request. Decision: JWT-only in v1; introspection
   deferred.
3. **PAT lifecycle parity.** PATs can be revoked instantly via
   `tokens_revoked_before`. OAuth access tokens cannot, short of
   shrinking TTL. Recommend `access_token` TTL ≤ 15 min + refresh, to
   keep blast radius small without introducing introspection.
4. **Multi-tenant deployments.** If a single AKB serves multiple
   tenants, do we want one realm per tenant or one realm with multiple
   audiences? Keep out of v1 — single realm assumed.
5. **OAuth Dynamic Client Registration Metadata extensions.** MCP spec
   has been adding optional fields (e.g. `software_id` per server).
   Track upstream spec PRs before locking metadata shape.
6. **Migration story for existing PAT users.** None needed — PAT path
   stays. Optional: surface "connect via OAuth" CTA in the AKB web UI
   for users who want to use claude.ai connectors.

## Out of scope

- ChatGPT/Anthropic "verified publisher" listing — orthogonal product
  decision, not a code concern.
- A first-party AKB consent screen — IdP owns it. If we ever decide we
  want AKB-branded consent, that becomes a separate design and pulls
  the consent surface onto the AS we'd then have to build.
- Replacing PAT entirely — not on the table; PAT is the right primitive
  for headless/CI and stdio proxy use.

## Implementation plan (sketch)

Sized rough; refine in implementation PR.

1. Add `MCP_RESOURCE` to `config.py`; gate everything on
   `settings.oidc.enabled`.
2. Extract `find_or_provision_user(claims)` from
   `keycloak_oidc.callback()` into `auth_service`.
3. Add `verify_jwt(token, audience)` to `keycloak_oidc` (audience param
   already validatable — the SSO path passes `audience=None` today;
   refactor to thread an explicit audience).
4. Add `resolve_oidc_jwt` to `auth_service`.
5. Patch `backend/mcp_server/http_app.py` token branch + 401
   `WWW-Authenticate` header.
6. Add `oauth_metadata.py` route + mount in `main.py`.
7. Realm config bundle in `deploy/k8s/internal/keycloak-realm-akb.json`
   (scopes + Trusted Hosts policy).
8. Tests:
   - Unit: JWT with wrong `aud` rejected; missing scope → 403;
     PAT still works; both token types in mixed traffic; metadata
     endpoint shape.
   - E2E (`backend/tests/test_mcp_oauth_e2e.sh`): mint a token via
     real Keycloak fixture, call `/mcp`, exercise a read + a write
     tool, exercise revocation by shortening TTL.
9. Docs:
   - `docs/mcp-clients/web-connectors.md` — how to add AKB as a custom
     connector in claude.ai / ChatGPT.
   - Update `README.md` connector section.

## References

- RFC 7591 — Dynamic Client Registration
- RFC 7592 — DCR Management Protocol (probably out of scope for v1)
- RFC 8707 — Resource Indicators
- RFC 9728 — Protected Resource Metadata
- RFC 9700 — OAuth 2.0 Security Best Current Practice
- MCP spec, "Authorization" section (current revision)
- Existing AKB design: [`keycloak-oidc/00-overview.md`](../keycloak-oidc/00-overview.md)
