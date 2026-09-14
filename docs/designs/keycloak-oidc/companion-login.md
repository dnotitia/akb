# Companion BFF account completion

Each registered BFF (including Reef) owns its browser OIDC code + PKCE login
and encrypted server-side session. AKB owns account adoption and enrollment. This **new custom API**
reuses the PR #531 account policy without an AKB browser visit or a second
OIDC flow. Neither ordinary `/auth/me` nor refresh, PAT or MCP requests gain
account-adoption authority.

## Deployment registration

`keycloak_companion_login_clients` defaults to `{}` (disabled). In SSO mode,
register each allowed OIDC client ID as a key with `provider_aliases` and
`public_keys` (key-ID to RSA SubjectPublicKeyInfo PEM). Keys must be at least
2048 bits; up to eight keys allow rotation. The client must also be present
in `keycloak_companion_client_ids_by_origin`, which remains only the human
API `azp` allowlist. AKB's ordinary/admin clients cannot be registered here.
The canonical issuer stays `keycloak_issuer`; API access audience stays
`api_oauth_audience_effective`. Enabling completion requires `public_base_url`
to be an HTTPS origin (optional trailing slash). Config never appears in
public `/auth/config`. Like AKB's browser callback, completion does not re-read
the provider-management catalog after token issuance: Reef chooses an enabled
provider at login start, and AKB checks the current registered BFF provider
allowlist on every completion. Removing that registration blocks new completions.

The BFF signing key is a **dedicated deployment credential**, independent
of the Keycloak client secret and user credentials. AKB stores only its public
key. Register a new `kid` before rotating the BFF; remove the old key after
in-flight callbacks have drained. Removing a client or key immediately blocks
new completion requests.

## Exact wire contract

`POST /api/v1/auth/sso/companion/complete`

Headers, each exactly once:

- `Authorization: Bearer <Keycloak access token>`
- `X-AKB-ID-Token: <Keycloak ID token>`
- `X-AKB-Login-Assertion: <BFF signed compact JWS>`

JSON fields: `provider_alias` (1–63 canonical alias characters), and `nonce`
(20–512 base64url characters). No extra fields. The BFF sends its saved provider
and original nonce after consuming browser state. It sends no refresh token.

Assertion protected header is exactly `{alg:"RS256",typ:"JWT",kid:"<key ID>"}`.
Payload requires `iss` and `sub` equal to the registered OIDC client ID,
`aud` equal to the HTTPS AKB origin plus the exact endpoint path above,
integer `iat` and `exp` with lifetime at most 60 seconds (no clock leeway),
a random lowercase UUID `jti`, and `request_hash`.

`request_hash` is base64url without padding of SHA-256 of UTF-8 JSON for:

```
[provider_alias,nonce,access_token,id_token]
```

Serialize the array compactly, with no whitespace or Unicode escaping. All
wire fields are restricted to ASCII. Values must not be changed after signing.
This is not an OAuth token endpoint or an RFC 7523 client-assertion profile.

AKB verifies this signature against registered keys, then independently
verifies the Keycloak pair: pinned issuer/JWKS/RS256, API access-token profile,
exact registered `azp`, ID-token audience/client, signed provider in both
tokens, expiry/issue time, nonce, subject, session and `at_hash`. Only then does
it call the shared account policy with the verified provider alias.

The BFF signature delegates responsibility for checking browser state/cookie
binding and PKCE to that registered BFF. AKB cannot independently inspect the
BFF's browser cookie. A compromised trusted BFF remains inside this trust
boundary; registering arbitrary clients would undermine it.

## Outcome, replay and failure

Success is `{user:{id,username,email,display_name,is_admin}}`, `Cache-Control:
no-store`. No AKB session, cookie or token is returned. Reef checks `/auth/me`
using the same access token and requires the same user ID before issuing its
own session. Stable denials retain `membership_required`,
`account_suspended`, and `identity_conflict`. Credential failures are sanitized.

Migration 101 adds one short-lived assertion replay table. An assertion is
consumed in a committed transaction before account projection; replay is
refused even after account refusal or a downstream failure. An assertion must
remain live at DB consumption; tombstones are retained a further minute to
close verification/consumption timing races. Only client ID, JTI and expiry are
stored, never raw tokens, identity metadata or completion results.

There is no separate login transaction, receipt or cached completion response.
A new assertion with a fresh JTI and valid token pair calls the public
`project_verified_principal_with_reason` boundary again. PR #531 owns exact
subject/address locking, account mutation, pending-admission commits and stable
refusals. Concurrent requests converge on the same identity. Existing accounts
are checked against current policy, including suspension; they do not receive
a cached success. A user can explicitly start a fresh OIDC login after failure.

New-user PostgreSQL role synchronization follows the same existing best-effort
lifecycle and periodic reconciliation policy as AKB's browser callback. This
endpoint does not add a separate role-sync receipt or retry mechanism.

## Rollout and observability

Deploy AKB with completion disabled first; then register the reviewed BFF
public key/client/provider mapping and deploy Reef's callback integration.
Keep authoritative email domain and verified-email policy unchanged. A 404,
410 or failed completion must fail closed: no legacy exchange fallback,
redirect, `/auth/me` polling or automatic token-refresh loop.

Never log tokens, assertions, nonce, request payloads or response profiles.
AKB's global redactor covers both new credential headers. At ingress/APM
configuration, exclude `Authorization`, `X-AKB-ID-Token`, and
`X-AKB-Login-Assertion` from request capture; keep header/body capture disabled
for this route. AKB does not manage external ingress/APM settings.

Local verification uses generated RSA keys and isolated PostgreSQL, including
signature/token substitution refusals, same-subject concurrency, replay,
fresh-request account revalidation, post-success suspension, pending commit,
and replay refusal after account failures. Existing PR #531 tests cover local-user ID,
PAT preservation and account conflict/recovery/service guards. Real Microsoft
login and deployment changes remain separate operator verification.
