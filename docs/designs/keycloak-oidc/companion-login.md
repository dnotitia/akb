# Companion application account completion

A companion application uses AKB accounts and APIs while owning its login
callback and session. Its backend-for-frontend (BFF) performs an OIDC
authorization code flow with PKCE against the same Keycloak issuer as AKB.
After verifying that login, a registered BFF can ask AKB to complete account
linking or enrollment through this server-to-server API. Browser navigation
stays between the application and its identity provider.

AKB applies the same account policy as its own browser callback: an exact
identity binding resolves the existing user; an explicitly configured
authoritative provider can adopt an eligible existing account by verified
email, preserving its AKB user ID. Enrollment, suspension, protected-account
and identity-conflict rules still apply. Registering a BFF does not by itself
enable email-based adoption. Ordinary `/auth/me`, refresh, PAT and MCP requests
do not gain account-adoption authority.

This is an opt-in AKB extension on top of OIDC, not a standard OAuth token
endpoint. It supports any operator-reviewed companion BFF meeting the contract
below; AKB has no dependency on a particular companion product or framework.

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
the provider-management catalog after token issuance: the BFF chooses an enabled
provider at login start, and AKB checks the current registered BFF provider
allowlist on every completion. Removing that registration blocks new completions.

The BFF signing key is a **dedicated deployment credential**, independent
of the Keycloak client secret and user credentials. AKB stores only its public
key. Register a new `kid` before rotating the BFF; remove the old key after
in-flight callbacks have drained. Removing a client or key immediately blocks
new completion requests.

Add the following to an existing SSO installation's `config/app.yaml`, replacing
the sample origin, client ID, provider alias and public key:

```yaml
keycloak_companion_client_ids_by_origin:
  "https://app.example.com": "example-app"
keycloak_companion_login_clients:
  example-app:
    provider_aliases: [workforce]
    public_keys:
      login-v1: |-
        -----BEGIN PUBLIC KEY-----
        <RSA SubjectPublicKeyInfo public key, at least 2048 bits>
        -----END PUBLIC KEY-----
```

The key body is a placeholder, not a usable key. AKB rejects malformed keys
at startup. `kid` must match `[A-Za-z0-9_-]{1,128}`. For Helm, use
`sso.companionClientIdsByOrigin` and `sso.companionLoginClients`; see the
[chart example](../../../deploy/helm/akb/README.md#companion-applications).
Neither configuration path provisions the companion's Keycloak client.

Before enabling a companion, its operator must:

1. Register a separate Keycloak client with its exact HTTPS callback URI and
   authorization code flow with S256 PKCE. Keep its OIDC credentials and
   assertion private key in that application's server-side secret store.
2. Configure access tokens for AKB's API audience and
   [human API token profile](00-overview.md#route-selected-token-profiles).
   The access and ID tokens must identify the same client, subject and session;
   the ID token must contain the original `nonce` and matching `at_hash`.
3. For a brokered provider, configure the trusted `identity_provider` claim
   in **both** tokens to match its AKB provider alias. For the reserved `local`
   alias (workspace realm login), that claim must be absent in both tokens.
   Register each offered alias in `provider_aliases`.
4. Review the existing authoritative-email-domain and enrollment policy.
   Domain authority is an explicit installation decision, not something a
   companion request may supply. See the
   [account adoption policy](../../design/proposal/2026-09-12-provider-authoritative-email-domains/README.md).

## Companion integration sequence

1. Read AKB's public `/api/v1/auth/config` provider capabilities at login start.
   Save a one-time browser-bound state, PKCE verifier, nonce and selected alias.
   Start the companion's own OIDC authorization request to Keycloak.
2. At the companion callback, consume and validate state/cookie binding, exchange
   the code using PKCE, and verify the ID token and nonce. Bind the selected
   provider to that login; do not accept it from a callback query parameter.
3. Sign and send the completion request below from the BFF server. Do this only
   for a completed browser login, never from refresh or ordinary API handling.
4. On success, call `/api/v1/auth/me` with the same access token and require the
   returned user ID to match completion. Then create the application's own
   server-side session and return the user to their original application page.
   An account denial or failed verification must not establish a session.

## Exact wire contract

`POST /api/v1/auth/sso/companion/complete`

Headers, each exactly once:

- `Authorization: Bearer <Keycloak access token>`
- `X-AKB-ID-Token: <Keycloak ID token>`
- `X-AKB-Login-Assertion: <BFF signed compact JWS>`

Set `Content-Type: application/json`. The body has exactly two fields:

```json
{"provider_alias":"workforce","nonce":"<saved one-time OIDC nonce>"}
```

`provider_alias` must match `[a-z0-9][a-z0-9._-]{0,62}`; `nonce` must match
`[A-Za-z0-9_-]{20,512}`. Replace the placeholder above with the actual saved
nonce. No extra fields are accepted. The BFF sends its saved provider
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
tokens (or absence of that claim for `local`), expiry/issue time, nonce,
subject, session and `at_hash`. Only then does
it call the shared account policy with the verified provider alias.

The BFF signature delegates responsibility for checking browser state/cookie
binding and PKCE to that registered BFF. AKB cannot independently inspect the
BFF's browser cookie. A compromised trusted BFF remains inside this trust
boundary; registering arbitrary clients would undermine it.

## Outcome, replay and failure

Success is `{user:{id,username,email,display_name,is_admin}}`, `Cache-Control:
no-store`. `id`, `username` and `email` are strings, `display_name` is a string
or null, and `is_admin` is a boolean. No AKB session, cookie or token is returned.

| HTTP status | Code | Companion behavior |
| --- | --- | --- |
| 200 | — | Verify `/auth/me`, then establish its own session |
| 401 | `authentication_failed` | End the attempt; check credentials or registration |
| 403 | `membership_required` | Explain that workspace membership is required |
| 403 | `account_suspended` | Explain that the account is suspended |
| 409 | `identity_conflict` | Explain that an administrator must resolve the account conflict |
| 422 | Request validation error | Correct the request format |

Account and credential errors use `{message,error,code,detail}`; credential
failures are sanitized. Request validation uses FastAPI's validation response.
An unavailable upstream or unexpected failure must also end the attempt;
clients must not interpret every failure as pending membership.

Migration 101 adds one short-lived assertion replay table. An assertion is
consumed in a committed transaction before account projection; replay is
refused even after account refusal or a downstream failure. An assertion must
remain live at DB consumption; tombstones are retained a further minute to
close verification/consumption timing races. Only client ID, JTI and expiry are
stored, never raw tokens, identity metadata or completion results.

There is no separate login transaction, receipt or cached completion response.
A new assertion with a fresh JTI and valid token pair calls the public
`project_verified_principal_with_reason` boundary again. That service owns exact
subject/address locking, account mutation, pending-admission commits and stable
refusals. Concurrent requests converge on the same identity. Existing accounts
are checked against current policy, including suspension; they do not receive
a cached success. A user can explicitly start a fresh OIDC login after failure.

New-user PostgreSQL role synchronization follows the same existing best-effort
lifecycle and periodic reconciliation policy as AKB's browser callback. This
endpoint does not add a separate role-sync receipt or retry mechanism.

## Rollout and observability

Deploy AKB with completion disabled first; then register the reviewed BFF
public key/client/provider mapping before enabling the companion integration.
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
and replay refusal after account failures. The shared account-policy tests cover
local-user ID/PAT preservation and account conflict/recovery/service guards.
Run the focused credential and chart suites from `backend/` with the locked
development environment:

```bash
uv run --locked --extra dev pytest tests/test_companion_login_unit.py \
  tests/test_companion_login_openapi_unit.py tests/test_helm_deployment_unit.py
```

The account-persistence suite, `tests/test_companion_login_postgres.py`, uses
the same isolated PostgreSQL setup as `tests/test_authoritative_email_domains_postgres.py`
and runs in the backend live-DB CI job. Real identity-provider login and
deployment changes remain separate operator verification.
