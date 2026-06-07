# Keycloak OIDC login (optional external IdP) — Design

**Status**: implemented + locally validated (real Keycloak 26 + browser e2e), awaiting review
**Started**: 2026-06-07
**Reference impl**: `seahorse-mcp-agent-server` (`app/auth.py`, `app/routers/auth.py`)
**AKB build**: httpx + pyjwt only (no new deps); PG-backed transient store (HA-safe)

## Statement

AKB gains an **optional** Keycloak OIDC login path alongside the existing
local username/password + PAT auth. Keycloak handles **authentication
only** (the front door). AKB's internal user model, JWT issuance, and the
entire PostgreSQL-ACL authorization model stay exactly as they are today.

When `keycloak.enabled = false` (the default), nothing changes — every
existing code path runs untouched. When enabled, Keycloak becomes an
additional way to obtain an **AKB-issued** JWT; it does not replace the
AKB JWT, PAT, or PG-role machinery.

## Why Keycloak is authN-only (the core constraint)

The seahorse agent server uses the Keycloak `id_token` directly as its
runtime identity (HttpOnly cookie, JWKS-verified per request, identity =
`email`/`preferred_username`/`sub`). **AKB cannot do this.**

AKB's authorization is bound to the internal `users.id` UUID:

- PG-native RBAC keys every role on it — `akb_user_<uid>`
  (`role_sync.py:94`), and `SET LOCAL ROLE akb_user_<uid>` runs every
  `akb_sql` query (`user_sql_executor.py:112`).
- `vault_access(vault_id, user_id, role)`, vault ownership, PAT
  (`tokens.user_id`), and JWT revocation (`tokens_revoked_before`) all
  reference the internal UUID.

If we validated Keycloak tokens directly in `resolve_token()` and used the
Keycloak `sub`, then PG roles, PATs, and vault ownership would all break —
there would be no `akb_user_<sub>` role, no `vault_access` rows for that
identity, etc.

**Therefore: Keycloak authenticates a person at the front door; AKB then
maps that person to an internal user row (by email) and issues its own
JWT via the existing `create_jwt()`.** The internal UUID — and everything
keyed on it — is preserved.

## Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Keycloak role | authN-only; **no** role/group mapping | is_admin & vault grants stay AKB-internal. Smallest blast radius; extend later. |
| First login | **JIT auto-provision** by email | Any realm member who authenticates gets an AKB user + `akb_user_<uid>` role via the existing `on_user_create` hook. |
| Token delivery to SPA | **one-time code exchange** (60s TTL) | Keeps AKB's Bearer + localStorage model; no token in URL history; no cookie-auth rewrite of the API client. |
| Default | `enabled = false` | Local auth is the baseline; Keycloak is opt-in per deployment. |

## Flow

```
[SPA]  GET /api/v1/auth/keycloak/login?redirect=/
         → 302 to Keycloak authorization_endpoint
           (state CSRF + PKCE S256, scope="openid profile email")

[Keycloak login screen]

       GET /api/v1/auth/keycloak/callback?code&state
         1. verify+consume state (PG; PKCE code_verifier for public clients)
         2. exchange code → tokens at token_endpoint (httpx)
         3. verify id_token locally: JWKS RS256, kid match, aud/iss/exp
            (pyjwt RSAAlgorithm.from_jwk; JWKS cached, refetched once on
             key rotation / kid miss)
         4. resolve internal user by email:
              - found    → user_id
              - not found→ provision_external_user(email, name, sub):
                             INSERT users (auth_provider='keycloak')
                             + RoleSync.on_user_create(uid)   [existing hook]
         5. token = create_jwt(uid, username)                 [existing fn]
         6. one_time_code = issue(token, ttl=60s)
         7. 302 to {frontend}/auth/callback?code=<one_time_code>

[SPA]  /auth/callback
         POST /api/v1/auth/keycloak/exchange { code }
         → { token, user }      ← identical shape to POST /auth/login
         → setToken(token)      ← existing frontend code, unchanged
         → navigate(redirect)
```

The `/exchange` response intentionally mirrors the existing
`/auth/login` response (`{token, user:{id,username,email,display_name,is_admin}}`)
so the SPA reuses `setToken()` and the rest of the Bearer flow verbatim.

## Token validation (ported from seahorse, AKB deps)

- **Local JWKS, no remote introspection.** Fetch JWKS from
  `keycloak_jwks_uri`, cache in-process, match `kid` from the token
  header, build the key with `jwt.algorithms.RSAAlgorithm.from_jwk(...)`
  and decode with pyjwt `algorithms=["RS256"], audience=keycloak_client_id,
  issuer=keycloak_issuer, options={"require": ["exp","iat","aud","iss","sub"]}`.
  On a `kid` miss the JWKS is refetched once (key rotation) before failing.
- This validation applies **only to the Keycloak `id_token` during
  callback**. It is not on the per-request hot path — once AKB issues its
  own JWT, `resolve_token()` handles every subsequent request exactly as
  today (HS256 AKB JWT or `akb_` PAT).

## Backend changes (file by file, as built)

**New**
- `backend/app/services/keycloak_oidc.py` — `KeycloakOIDC` class +
  module singleton `get_keycloak_oidc()`. Builds the authorization URL,
  exchanges code for tokens (httpx, honoring `keycloak_verify_ssl` via a
  dedicated client), fetches/caches JWKS, verifies the id_token with
  **pyjwt** RS256 (`RSAAlgorithm.from_jwk`), refetching JWKS once on a
  `kid` miss. Also hosts the PG-backed transient store helpers
  (`_store_issue` / `_store_consume`) and the one-time exchange-code
  helpers (`issue_exchange_code` / `redeem_exchange_code`).
- Transient store is the PG table `oidc_transients` (HA-safe across
  replicas) — NOT an in-process dict. Holds both `kind='state'` (CSRF +
  PKCE verifier + redirect path) and `kind='exchange'` (one-time code →
  `{token, user}`). Single-use via `DELETE … RETURNING`, TTL-bounded,
  opportunistically GC'd on issue.

**Modified (additive only)**
- `backend/app/api/routes/auth.py` — add `GET /auth/config` (public),
  `GET /auth/keycloak/login`, `GET /auth/keycloak/callback`,
  `POST /auth/keycloak/exchange`, `GET /auth/keycloak/logout`. Each SSO
  route `_require_keycloak()`s → **404 when disabled**. Same-site
  `_safe_redirect_path()` guard blocks open-redirects; callback failures
  bounce to `/auth?sso_error=<code>`. The callback also stashes the KC
  `id_token` in the exchange payload so the SPA can pass it as
  `id_token_hint` for a prompt-free RP-initiated logout.
- `backend/app/services/auth_service.py` — add
  `login_with_keycloak_claims(claims) -> {token, user}` (JIT-provision by
  email + `on_user_create`, dedup on re-login, `ConflictError` if a
  `local` account owns the email). `login()` now rejects non-`'local'`
  providers; `verify_password()` is hardened against the non-bcrypt
  sentinel. `create_jwt()` / `resolve_token()` **unchanged**.
- `backend/app/config.py` — flat `keycloak_*` settings + derived-endpoint
  properties (see below). `keycloak_enabled` defaults `false`.
- `backend/app/services/lifecycle.py` — fail-fast validation when
  enabled; close the Keycloak httpx client on shutdown.

**Schema (migrations 033, 034 + init.sql)**
- `033` — `users.auth_provider TEXT NOT NULL DEFAULT 'local'`. SSO users
  get `'keycloak'` + a sentinel `password_hash`; local `/auth/login` is
  rejected for non-`'local'` providers.
- `034` — `oidc_transients` table (stays empty when SSO is off).

## Config (flat `keycloak_*` keys)

Flat keys (not a nested block) match the existing `jwt_*` / `s3_*` /
`embed_*` convention AND avoid the shallow `app.yaml`+`secret.yaml` merge
clobbering a nested mapping — so the secret can live in `secret.yaml`
independently.

```yaml
# config/app.yaml  (non-secret)
keycloak_enabled: false                # default — local auth only
keycloak_server_url: https://auth.example.com   # browser-facing → issuer
keycloak_internal_url: ""              # optional backchannel (server→KC); blank → server_url
keycloak_realm: akb
keycloak_client_id: akb-web
keycloak_public_client: false          # true → PKCE, no client_secret
keycloak_verify_ssl: true
keycloak_redirect_uri: https://akb.example.com/api/v1/auth/keycloak/callback
keycloak_post_login_path: /auth/callback
keycloak_exchange_code_ttl_secs: 60
# config/secret.yaml
keycloak_client_secret: "<from Keycloak client credentials>"
```

- `keycloak_client_secret` lives only in `secret.yaml` (gitignored) —
  matches `feedback_oss_no_secrets`. (AKB reads no env vars, so the
  seahorse `client_secret_env` indirection is replaced by secret.yaml.)
- **Split-horizon** `keycloak_internal_url`: when set, the **backchannel**
  calls (token + JWKS) use it while the **issuer** and browser-facing
  authorization/logout endpoints stay on `keycloak_server_url`. Solves
  both prod (internal vs ingress URL) and the local docker
  `localhost`-vs-`container-DNS` gap. Blank → single-URL (the common case).
- OIDC endpoints are computed properties off the realm issuer — no
  `.well-known` fetch.
- Vendor name `keycloak` in keys is fine: external product name, not an
  internal storage driver, so `feedback_driver_neutral_naming` (qdrant/
  vector_*) does not apply.

## Frontend changes

- `frontend/src/pages/auth.tsx` — when `/auth/config` reports
  `keycloak.enabled`, render an "SSO 로그인" button linking to
  `/api/v1/auth/keycloak/login?redirect=<current>`. Existing
  username/password form stays as the local fallback.
- `frontend/src/pages/auth-callback.tsx` (new) — read `?code`, POST to
  `/auth/keycloak/exchange`, `setToken(token)`, navigate. No keycloak-js /
  oidc-client dependency (same as seahorse's plain-fetch SPA).
- `frontend/src/lib/api.ts` — no change to the Bearer transport; only add
  the two new endpoint helpers.

## Differences from seahorse (and why)

| Aspect | seahorse | AKB | Why different |
|---|---|---|---|
| Runtime identity | Keycloak id_token (cookie) | AKB-issued JWT | AKB authZ keyed on internal UUID |
| Token transport | HttpOnly cookies | Bearer + localStorage (unchanged) | Avoid rewriting API client + PAT model |
| Per-request verify | JWKS on every request | JWKS only at callback | AKB JWT/PAT path already exists |
| First login | allow-list gate | JIT auto-provision | Decision locked; simpler onboarding |
| Roles | `realm_access.roles` → admin | authN-only | Keep authZ AKB-internal for v1 |

## Out of scope (v1) / future

- Keycloak group → `vault_access` sync, and `realm_access.roles` → is_admin.
  Hook point exists (`RoleSync`); revisit once authN ships.
- Token refresh: AKB JWT is short-lived and re-obtained by re-login through
  Keycloak (SSO session makes this seamless). A silent-refresh endpoint can
  be added later if needed.
- RP-initiated logout IS implemented (`GET /auth/keycloak/logout` →
  Keycloak `end_session_endpoint`, seamless via `id_token_hint`; the SPA
  triggers it only for SSO-originated sessions via a localStorage marker,
  so local-auth logout is untouched). Still out of scope: Keycloak
  front-channel/back-channel single-logout that notifies *other* RPs.
- Account linking: a same-email collision with a non-`keycloak` account is
  rejected by default (`ConflictError`, no silent merge). Opt-in
  `keycloak_link_by_email` (default false) links the SSO identity to the
  existing account — keeping its `user_id`, PATs, vault ownership and
  grants — and flips `auth_provider` to `keycloak`. A cross-provider link
  requires the id_token's `email_verified` to be true (independent of
  `keycloak_require_verified_email`) so a relaxed realm can't take over an
  account by asserting its email. This is what the **managed akb-platform**
  needs: its operator pre-provisions an AKB user (+PAT) per member via
  `/auth/register` (local), and the same member then logs in via SSO —
  without linking, every pre-provisioned member is locked out. (Platform
  wiring: set `keycloak_link_by_email: true` + `keycloak_require_verified_email:
  true` in the per-tenant AKB backend config. PAT *rotation* after a member
  has SSO-linked still needs an admin mint API — separate follow-up, since
  the operator currently mints by logging in with the now-retired password.)

## Local test harness

Optional overlay `docker-compose.keycloak.yaml` + realm fixture
`deploy/keycloak-dev/akb-realm.json` (realm `akb`, confidential client
`akb-web` with a throwaway dev secret, test user `alice/alice-password`).

```
docker compose -f docker-compose.yaml -f docker-compose.keycloak.yaml up
# then set keycloak_* in config/app.yaml + secret.yaml (see overlay header)
```

## Validation (done 2026-06-07)

Validated against a real **Keycloak 26** (realm imported) + throwaway
Postgres, exercising the actual backend modules and a real browser.

- **Backend integration (20/20)** — config endpoint derivation; OIDC
  JWKS RS256 verify of a real id_token; tampered-token rejection; issuer
  enforcement; JIT provisioning (`auth_provider='keycloak'`, sentinel
  hash); `akb_user_<uid>` + `akb_authenticated` PG roles created; dedup on
  re-login (exactly one row); local-login rejected for SSO accounts;
  `ConflictError` when a local account owns the email; one-time exchange
  code single-use.
- **HTTP routes (enabled)** — `/auth/config` → `enabled:true`;
  `/auth/keycloak/login` → 302 to Keycloak with correct params;
  open-redirect guard; `/exchange` bad code → 400; invalid `state` →
  302 `/auth?sso_error=invalid_state`; local register/login regression OK.
- **Full browser e2e (Playwright)** — `/auth/keycloak/login` → Keycloak
  login page → `alice` → callback issued one-time code → `/exchange` →
  AKB JWT → `/auth/me` (auth_method `jwt`, display_name synced) → PAT
  issuance works → code replay 400.
- **enabled=false (optional gate)** — `/auth/config` → `enabled:false`;
  all three SSO routes → 404; local login still works; no Keycloak code
  runs at boot.

Outstanding before merge: run the full E2E suites (`test_mcp_e2e.sh`,
`test_pg_rbac_e2e.sh`, `test_security_edge_e2e.sh`) against a compose
stack as the standard pre-merge gate.
