# Keycloak OIDC boundary

**Status:** AKB Phase 4 ordinary browser-session custody active; companion BFF
and managed-platform cutover remain a cross-repository release gate

The accepted
[Authentication Mode Boundary](../../design/accepted/2026-08-13-authentication-mode-boundary/README.md)
and
[Workspace Account Governance](../../design/accepted/2026-07-10-workspace-account-governance/README.md)
are authoritative. This document records the current implementation shape.

## Route-selected token profiles

Human REST and delegated-human routes select exactly one verifier from the
canonical `auth_mode`:

- `local` selects `local-session-rs256-v2` only;
- `sso` selects `keycloak-access-v1`, or — for the one configured non-human
  client, and only when one is configured — `keycloak-service-authority-v1`.
  The two are disjoint by authorized party and neither is a fallback for the
  other; see [Non-human service authority](#non-human-service-authority).

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

## Non-human service authority

A workspace whose accounts all come from an identity provider has no way to
hand a control plane a first credential: every administrative call needs a
bearer, and obtaining one needs an account. `keycloak-service-authority-v1`
closes that circle with the one credential such a workspace already owns — a
client-credentials token from its own realm.

`keycloak_service_admin_client_id` names the exact OIDC client whose service
account is admitted. Blank, the default, keeps the profile inert: no
service-account token authorizes anything. Naming a client is a deliberate
grant, separate from `keycloak_management_client_id`, because it turns
possession of that client's secret into administrative authority over this AKB.
A client that is also an ordinary, companion, or `/admin` client is rejected at
configuration load, and rejected again at verification time.

It is a separate profile rather than a relaxation of `keycloak-access-v1`
because a client-credentials token is a different object. Measured against
Keycloak 26.0 and 26.4, the token this grant produces carries `iss`, `sub`,
`iat`, `exp`, `jti`, `typ`, `azp`, an empty `scope`, and Keycloak's `client_id`
/ `clientHost` / `clientAddress` markers — and **no `aud`, no `sid`, and no
profile claim at all**. Admitting it through the human profile would mean
dropping three requirements every human bearer must keep.

So the authority is pinned to what such a grant does establish: the pinned
realm signed it, and the named client obtained it. `aud` is not required —
Keycloak cannot put one on a client-credentials token without an operator
adding an audience mapper, and neither the `audience` nor the `scope` request
parameter changes that. A token that does carry an audience is still refused if
it carries the MCP route's. The profile further requires the `client_id` marker
to name the same client as `azp`, admits `preferred_username` only in the exact
`service-account-<client>` form, and refuses any token carrying `email`,
`email_verified`, or `name`: a machine principal has no person behind it, so a
bearer that describes one came from some other flow.

Selection between the two SSO profiles is by authorized party, and is not a
fallback: exactly one profile is tried, and its refusal is final. The peek that
selects is unverified and never an authorization input — each profile proves
signature, issuer, and its own exact `azp` before returning a principal.

The principal is resolved by `service_identities`, a table deliberately
separate from `external_identities`. That table means "an identity provider
identity belonging to a person here", and enrollment policy, email snapshots,
profile refresh, the browser-session surface, and the product-admin surface all
read it that way; a machine row there would make every one of them a place
where the invariant has to be re-established. One `service_identities` row is
one authority — an exact (issuer, client) pair — resolved to one AKB account
with `account_kind = 'service'`, `auth_provider = 'service'`, an unusable
password sentinel, and administrator status granted at creation. The realm
subject is recorded and refreshed for audit; keying on the client is what makes
a deleted-and-recreated client a refreshed binding rather than a second
administrator. A later demotion or suspension by an operator is respected on
the next request rather than silently re-granted.

Nothing about the human paths moves for this. The product administrator's exact
prebound identity, the no-just-in-time rule under `invite_only`, and the human
assertion are unchanged, and the named client can never authorize a human
route on either profile. The principal holds no token-store credential, so it
is also not the independent service administrator that recovery-administrator
retirement requires.

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

## Dedicated product-admin browser boundary

`/admin` is a separate route and policy surface in both modes:

| Mode | Product-admin authentication |
| --- | --- |
| `local` | Dedicated admin login endpoint authenticates local credentials, requires the live AKB `is_admin` decision, and returns `local-session-rs256-v2`. |
| `sso` | Dedicated confidential `akb-admin` client uses authorization code + PKCE + nonce. Only an exact, pre-bound `(issuer, subject)` active human with live AKB admin status is admitted. |

The admin client is excluded from `keycloak_human_client_ids`, and its `azp`
is explicitly refused by both API and MCP access-token profiles, so its token
cannot become a resource credential. The callback verifies the
fixed RS256 issuer/client/nonce/session profile and does not JIT-provision or
email-adopt an account. Its single-use state is bound to the initiating browser
through the hash of a short-lived HttpOnly cookie, preventing a callback copied
into another browser from creating an admin session. It discards all Keycloak
token material after proof,
then creates a short-lived AKB-owned opaque HttpOnly session and separate CSRF
cookie. Production HTTPS uses the same browser-enforced `__Host-`, `Secure`,
no-`Domain`, root-path profile as ordinary sessions. PostgreSQL stores only
SHA-256 hashes, the exact external-identity FK,
the bound issuer/subject snapshot, the Keycloak session ID, and expiry. Every
admin request rechecks the account, unchanged exact binding, status, kind,
provider, and `is_admin`; demotion deletes existing admin handles in the same
transaction, while binding mutation/removal also invalidates resolution. The
session lifetime is capped by both
`admin_browser_session_ttl_secs` and the verified ID-token expiry.

The admin session deliberately remains a separate short-lived proof and does
not share the ordinary-user refresh-token store. Its bounded lifetime limits
IdP-side revocation lag without broadening the recovery client's authority.

## Runtime provider control

In SSO mode, `/admin` manages an explicit registry of built-in upstream
providers. The first provider is `keycloak-oidc`, which brokers a distinct
Keycloak issuer behind the installation's Keycloak realm. Configuration always
lands disabled; enable and disable are separate mutations and each result is
read back from Keycloak. An enabled provider must be disabled before it can be
reconfigured.

The permanent realm-scoped management client is separate from the one-time
standalone bootstrap client. A missing management credential produces explicit
`delegated` control ownership and never falls back to a broader credential.
Provider client secrets are write-only and excluded from admin, public, event,
and audit views. The contribution contract and operator guide live under
[`docs/sso/`](../../sso/README.md).

## Ordinary browser-session custody

AKB's own SPA uses a server-selected authorization-code flow with PKCE, nonce,
single-use state, and a short HttpOnly browser-binding cookie. The callback URI
is derived only from `public_base_url`; request input cannot select a client,
callback, issuer, or verifier. Login may start only through an exact enabled
provider alias returned by the current Keycloak read-back.

After access- and ID-token verification and exact account projection, the
browser receives:

- an opaque, high-entropy `__Host-akb_sso_session` HttpOnly cookie;
- a separate readable `__Host-akb_sso_csrf` cookie used as a double-submit value.

On production HTTPS both use the browser-enforced `__Host-` prefix, `Secure`,
no `Domain`, and `Path=/`; the short OIDC binding cookie follows the same rule.
Explicit loopback HTTP development uses separate `akb_dev_*` names because a
browser correctly refuses `__Host-` without `Secure`. Session and CSRF cookies
are `SameSite=Lax` and root-scoped because protected AKB
surfaces exist at `/api/v1`, `/api/assets`, and `/health/vault`. The SPA sends
cookies only same-origin and adds `X-AKB-CSRF` to unsafe cookie-backed methods.
If an `Authorization` header is present, that Bearer credential owns the
request; rejection never falls through to a cookie session.

PostgreSQL stores only the opaque-handle and CSRF hashes, exact
`(issuer, subject)` identity FK, Keycloak `sid`, bounded expiry metadata, and an
AES-256-GCM envelope containing the refresh token, ID token, and scope. Access
tokens are never persisted. Every request rechecks the live AKB account and
exact identity binding. Near access-token expiry, one row lock serializes
refresh-token rotation. A non-locking expiry probe and bounded refresh gate
ensure waiting requests do not consume the PostgreSQL pool before admission;
invalid refresh deletes the session while a transient
Keycloak/JWKS outage rolls the transaction back and preserves it. Account
suspension deletes ordinary and admin handles in the state-change transaction.
Signed back-channel logout writes a short-lived `(issuer, sid, iat)` ordering
fence under the same advisory lock used by session creation, so a callback that
resumes after logout cannot recreate that session. Idle expiry never extends
the absolute lifetime, and active sessions per user are bounded.

The encryption key is an independent, installation-owned 256-bit base64url
secret. A blank key keeps ordinary browser SSO fail-closed during a staged
deployment; a malformed configured key fails startup. Replacing the only key
forces ordinary SSO reauthentication. Key-ring rotation is not part of this
MVP.

All ordinary sessions, product-admin sessions, and logout fences are bound to
the required non-secret `sso_session_epoch` UUID. The complete runtime boundary
also includes a positive, monotonic `auth_runtime_generation`. Normal restarts
must present the exact persisted generation/mode/epoch tuple; every mode change
or epoch rotation must increase the generation. PostgreSQL rejects stale and
same-generation conflicting starts, while an accepted greater generation
transactionally revokes every row in those three classes. Returning from
`local` to `sso` therefore cannot resurrect stale rows even when the UUID is
reused. This binding is separate from encryption-key custody and also covers
the admin session class, which stores no encrypted Keycloak token.

| Route | `local` | ready `sso` |
| --- | --- | --- |
| `GET /auth/sso/{alias}/login` | `404` | starts only an enabled provider |
| `GET /auth/keycloak/callback` | `404` | verifies and creates opaque session |
| `POST /auth/logout` | `404` | local handle delete, best-effort realm revoke, cookie clear |
| `POST /auth/keycloak/backchannel-logout` | `404` | signed exact issuer/`sid` revoke plus late-callback fence |
| provider-less login / AKB JWT exchange | `404` | permanently `410` |

When SSO is configured but browser custody is not ready, login/callback/logout
fail before issuing state or contacting the token endpoint. SSO never issues,
stores, or transports an AKB user JWT. Reef and Naut must instead own their
separate BFF callback/token custody and present their Keycloak access token to
AKB; that cross-product change remains part of the release gate.

## Public capability schema v2

`GET /api/v1/auth/config` publishes only non-secret capabilities:

```json
{
  "schema_version": 2,
  "auth_mode": "sso",
  "local_auth": { "enabled": false },
  "keycloak": {
    "enabled": true,
    "browser_session_ready": true
  },
  "providers": [
    {
      "provider_type": "keycloak-oidc",
      "alias": "workforce",
      "display_name": "Company SSO",
      "login_url": "/api/v1/auth/sso/workforce/login"
    }
  ],
  "mcp_oauth": { "enabled": false }
}
```

`local_auth.enabled` derives from `auth_mode`. `keycloak.enabled` means the
ordinary human SSO authority, so local mode with Keycloak enabled solely for
MCP still publishes it as `false`. `providers` contains only exact enabled,
non-drifted provider read-backs. A provider login URL is non-null only when the
browser-session encryption/client profile is ready; otherwise the same enabled
provider may be listed with `login_url: null`. MCP OAuth remains orthogonal.

Clients accept only schema v2 with a known mode and internally consistent
capabilities. Fetch failure, non-success status, malformed JSON, old/missing
shape, unknown version/mode, or mode contradiction yields a deny-all
unavailable state; clients never infer local mode.
