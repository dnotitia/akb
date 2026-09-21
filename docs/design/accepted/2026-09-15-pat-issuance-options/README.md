---
status: accepted
stage: implemented
created: 2026-09-15
updated: 2026-09-21
baseline: a96b531e741fd2633ca379bb2dac3cdc048e05dc
---

# Personal access token issuance options

## 1. Outcome and scope

Expose expiration, coarse read/write permissions, and Vault write restrictions in
the existing token creation flow. Keep name-only creation available and disclose
the actual authority before submitting. A successful creation reveals the secret
only in the current setup session, with immediate copy feedback.

This implementation covers the shared connection setup on Home and Settings → Agent
connections, token-list metadata, and safe replacement of an existing token. It
applies to both local human authentication and SSO. It does not add token-secret
recovery, service-key UI, organizational lifetime policy, or a new Vault read
isolation model. Deployment and live Vault updates are outside this implementation.

**Accepted approach:** a shared advanced form backed by a versioned issuance
contract, with backend enforcement before exposing the UI. A purely
frontend change would preserve unsafe compatibility and replacement behavior.

## 2. Findings from the current code

| Evidence at the baseline | Consequence for this design |
| --- | --- |
| [ConnectionSetup](../../../../frontend/src/components/connection-setup.tsx) creates with `createPAT(name.trim())`; both compact and workspace layouts share it | Extend this component through one shared form, not a second Settings-only creator |
| [API client](../../../../frontend/src/lib/api.ts) exposes positional name/scopes/expires_days with an `any` response and no vault_scope | Add explicit request/receipt types and validation |
| [CreatePATRequest and create_token](../../../../backend/app/api/routes/auth.py) accept name, expires_days, scopes, key_class, vault_scope | Existing fields are insufficient as proof that an older server enforces a submitted restriction; do not probe with a token-creating request |
| [create_pat](../../../../backend/app/services/auth_service.py) defaults to no expiration and read+write; `if expires_days` treats zero as no expiration | Preserve basic defaults; validate advanced expiration strictly; do not silently convert invalid expiration to unlimited |
| [normalize_token_scopes](../../../../backend/app/services/auth_service.py) accepts read, write, admin; omitted means read+write; an empty list is invalid | Expose Read only and Read and write presets; do not present admin as account-administrator elevation |
| [VaultScope](../../../../backend/app/models/vault_scope.py) is literal prefix OR exact name; null is unscoped; empty concrete scope is invalid | Serialize null and a selected empty set differently; never change an invalid selection into null |
| Ordinary Vault access uses scope to restrict writes, while [SQL scope tests](../../../../backend/tests/test_vault_scope_sql_e2e.sh) require scoped SQL reads to be confined too | Label the control **Vault write restriction** and explain the SQL exception; never promise read isolation for ordinary document reads |
| [list_pats](../../../../backend/app/services/auth_service.py) returns scopes and expires_at but omits vault_scope | Add restriction metadata; absent metadata is unknown, not unrestricted |
| [handleReissue](../../../../frontend/src/pages/settings/tokens-section.tsx) revokes first and then calls `createPAT(p.name)` | Reissue loses expiration/permission/Vault restrictions; replacement must review and preserve them |
| [REST authorization](../../../../backend/app/api/deps.py) permits a write PAT to reach the existing mint route; create_token does not attenuate against the caller's token restrictions | A restricted PAT can mint an unrestricted child. Close this path on the legacy route as well as the new route before shipping the feature |
| [Administrator mint routes](../../../../backend/app/api/routes/access.py) check is_admin and call the same mint service without parent restrictions | A scoped administrator PAT can use the admin aliases to mint an unrestricted token for itself; self-route checks alone are insufficient |

Read-only route reproduction confirmed the last issue with an injected write-only
PAT carrying prefix `restricted-`: a name-only child request reaches create_pat
with no Vault scope, no expiration, and default read+write. This was a mocked
authorization/route reproduction, not an exploit against a deployed tenant.

### Authority model to communicate

- Coarse permissions permit API operation classes; they do not grant new account
  or Vault privileges. `write` alone does not implicitly supply `read`.
- For ordinary Vault mutations: current user ACL ∩ coarse write permission ∩
  selected Vault write restriction, plus existing operation-specific policy.
- Ordinary document reads remain within the user's existing readable Vaults,
  even outside the write restriction. Raw `akb_sql` is stricter and also confines
  reads to the token-scoped PostgreSQL role.
- A prefix is literal `startsWith`, not a glob or a collection path. `team-`
  matches `team-docs`, but `team-*` is invalid. Exact names and prefixes form a
  union. Future matching Vaults may become writable when the user's ACL permits.
- Scope never grants missing ACL access. Revoking a user grant or suspending the
  account still removes effective access. A read-only token does not become a
  scoped data-sharing credential by selecting Vaults.

## 3. Product and interaction decisions

### Default creation

Keep name-only creation and the existing OAuth/Use a saved token choices. Opening
Advanced options must not change any value. The initial summary states:

> No expiration · Read and write · No additional Vault write restriction.
> Your existing account permissions still apply.

Recommend choosing an expiration and limiting writes, but do not preselect a
Vault or silently narrow a token. Default compatibility is an explicit tradeoff;
this change does not impose a new mandatory lifetime policy.

### Advanced form

```text
Token name                  [Work laptop                         ]
▾ Advanced options
  Expiration                [No expiration                      ▾]
                            7 / 30 / 90 / 365 days / Custom date and time
  Permissions               ( ) Read only   (●) Read and write
  Vault write restriction   (●) No additional restriction
                            ( ) Selected Vaults or name prefixes
    Vaults                  [Search accessible Vaults…            ]
    Selected                [project-docs ×] [team-notes ×]
    Name prefixes           [team-                ] [Add]
    Reads are not restricted to this selection. SQL reads are stricter.

  Before you create
  Expires … · Read and write · Writes: selected Vaults OR team-…
  Prefixes also cover future matching Vaults you can write.
                                                    [Create token]
```

- Always keep the summary visible above the submit action, including when
  advanced controls are collapsed. Collapsing does not discard restrictions.
- For Read only, keep the scope selection and explicitly state that writes are
  disabled and the selection still restricts SQL reads, not ordinary document
  reads. Do not discard or null a selected scope when changing permission presets:
  doing so would widen SQL reads, especially when replacing an existing token.
- Fetch accessible Vault names when selected mode opens. Retain selected values
  if search filtering, loading, or refresh changes. Do not select all results.
  A failed list request leaves selected mode intact with Retry and manual exact
  name/prefix entry; never switch to unrestricted mode.
- Display current Vault matches only as a preview. Explain zero matches and
  future grants; do not falsely reject a valid future prefix. A syntactically
  valid exact name need not exist yet. Current matches are not an authorization
  guarantee and must not disclose inaccessible Vaults.
- Both Home compact setup and Settings workspace setup use the same form and
  controller. In the workspace layout, expanded controls remain within the
  Prepare access panel; prevent long name chips from widening the grid.

### Validation and expiration

| Input | Rule |
| --- | --- |
| Name | Trim; v1 requires 1–255 Unicode code points, reported by capabilities. This is a proposed v1 UI/API bound; the existing database column is TEXT, not length-limited |
| Permissions | UI emits exactly `["read"]` or `["read","write"]`; reject empty/unknown arrays before submitting |
| Selected Vaults | At least one exact name or prefix; each string must match `^[a-z0-9][a-z0-9-]*$`; trim and deduplicate; no wildcard, slash, uppercase conversion, or guessed prefix |
| Expiration preset | Positive integer days; no booleans, strings, fractional or negative numbers; server rejects date arithmetic overflow |
| Custom expiration | Real local date/time with visible IANA timezone and UTC preview; reject invalid/past instants and unresolvable DST ambiguity; send aware RFC3339 UTC |
| Mutually exclusive expiration | Send either expires_days, expires_at, or neither; never both |

The server's clock is authoritative and checks expiration again before insertion.
A preset means server issuance time plus N×24 hours, not local end-of-day. A
custom value is an exact instant; never round it up to whole days. Show the actual
server-returned expires_at after creation. Allow correction if time advances past
a custom value while the form is open.

For existing legacy clients, omitted/null/zero expires_days keep their historical
unlimited meaning. Negative, noncanonical types and overflow must receive a field
validation error rather than minting an already-expired token or returning 500.
These validation changes need explicit release notes and updates to any expiry
test fixtures; they do not change stored tokens. The new contract rejects zero.

## 4. Versioned server contract and old-server behavior

### Capability discovery

Add authenticated `GET /api/v1/auth/tokens/capabilities`, with `Cache-Control:
no-store`. Version 1 reports the caller user_id, the supported issuance version,
the two UI permission presets, expiration modes and any enforced limits, and
`vault_scope_semantics: "write_restriction_sql_read_write"`.

Capabilities are display input, not authorization. Pin them to the current auth
snapshot. A missing endpoint (404/405/501) means legacy support. A 401/403, timeout,
5xx, or malformed capability is an error state, not evidence of an older server;
offer reauthentication/retry rather than downgrading silently.

### Issuance

Add `POST /api/v1/auth/tokens/issuance` with strict extra-field rejection:

```json
{
  "contract_version": 1,
  "expected_user_id": "<current-user-uuid>",
  "name": "Work laptop",
  "scopes": ["read", "write"],
  "vault_scope": {"prefixes": ["team-"], "extra_vaults": ["project-docs"]},
  "expires_days": 30
}
```

Custom expiration substitutes an aware UTC `expires_at` for expires_days.
No-expiration omits both. Require scopes and vault_scope explicitly in v1 so
missing restrictions cannot become defaults. Fix key_class to PAT in this route.
No DB migration is needed: the existing expires_at/scopes/vault_scope columns
store the result. Extend the mint service to accept a validated absolute expiry.

The receipt includes contract_version, user_id, token_id, name, token, prefix, issued_at,
key_class, normalized scopes, canonical vault_scope, and expires_at. Frontend
validates identity, version, PAT class and exact intended restrictions before
showing configuration snippets. The server derives issued_at and relative expiry
from the same UTC instant; the client checks their difference against N×24 hours
without relying on its own clock. Custom expiry must equal the requested instant.
If a receipt is malformed or contradicts the request, do not advertise a usable
restricted token or retry creation automatically. Show “creation result needs
checking”, reload token metadata, and offer explicit revocation of a positively
identified new token. Never revoke by name, since names are not unique.

Return stable validation codes and field paths (for example
`vault_scope.prefixes.0`, `scopes`, `expires_at`) in a 422 response; the client maps
these to inline errors and the summary. Do not parse English error messages to
choose a field. Identity/authorization conflicts remain form-level failures.
Unknown legacy 400/422 responses stay form-level and never trigger a downgrade.

### Close the minting bypass

Before publishing capability v1, require an active **human** credential for both
the new issuance route and legacy POST /auth/tokens: local JWT, verified human
OAuth, or ordinary SSO browser session with CSRF. PATs and service keys must not
mint children through either path, including PATs belonging to administrators.
Keep existing administrator service-key issuance policy for genuine human admin
credentials; this implementation adds no service-key form.

The administrator aliases `/admin/users/{user_ref}/tokens` and
`/admin/users/{user_ref}/managed-tokens` must use the same issuer policy. Reject
PAT callers there regardless of target user or administrator status. Their
documented managed-control-plane use needs an explicit machine issuer exception:

- Allow only active service credentials whose token ID is in an operator-managed
  `admin_token_issuer_ids` allowlist (default empty), with an administrator owner,
  no Vault scope, and coarse read+write authority. A scoped or write-only service
  key remains rejected. The exception applies only to the administrator routes.
- Treat that credential as intentionally authorized to provision tokens, not as a
  limited PAT whose lifetime/scopes must be inherited. Audit issuer ID, target and
  requested restrictions; never log raw secrets.
- Inventory and migrate existing control-plane consumers before enforcement.
  Existing administrator PAT automation must move to an explicitly provisioned
  service issuer; never allow all administrator PATs as a compatibility fallback.
  The actual external consumer inventory is an implementation prerequisite, not
  something established by this repository-only investigation.

Centralize this policy at the two production mint call sites (auth routes and
the shared administrator mint helper), passing the issuer to authorization. Test
all four public issuance routes. No new UI becomes available until these aliases
are covered and machine issuers have been deliberately configured.

Do not infer human authorization from user.is_admin alone. Validate auth_method,
account_kind, current account state and the expected identity for v1. Reuse the
existing account row lock so concurrent suspension cannot leave a live token.
For administrator issuance, revalidate both issuer and target inside the mint
transaction, locking their user rows in UUID order. For a machine issuer also
recheck the live token row, expiration, class, scopes and allowlist membership;
an earlier HTTP authorization snapshot is not sufficient after revocation or
suspension. Handle the self-target case with one user lock.
Audit every public mint entry point during implementation; an unguarded alias
would preserve the bypass. Any pre-existing automation that mints PATs using a
PAT must migrate to a human-authorized issuance flow. Child-token attenuation is
a separate alternative, not an incomplete fallback.

### Compatibility matrix

| Situation | Behavior |
| --- | --- |
| v1 supported, basic creation | Send explicit default scopes/null scope through v1; preserve name-only UX |
| v1 supported, advanced values | Submit v1 once and verify its receipt |
| Legacy capability endpoint absent | Show advanced support unavailable; offer an explicit “Create with server defaults” basic action |
| Restricted draft on a legacy server | Keep the draft; require the user to review/reset to server defaults before basic creation; never retry after dropping fields |
| Capability says v1 but POST lands on an old replica | 404/405 is a failed advanced attempt; retain draft, no automatic legacy request |
| Unknown/error capability | Retry or reauthenticate; do not classify as legacy |

Legacy fallback sends only `{name}` to the existing route and labels its receipt
as server-default issuance, without claiming validated advanced restrictions.
Never send advanced fields to an unknown endpoint that may silently ignore them.
Deploy and drain old mint handlers before enabling v1 discovery, because the old
PAT-to-PAT mint path remains reachable during a mixed-version rollout.

## 5. Token list and replacement

Add canonical vault_scope to list_pats and define typed metadata in the client.
Render permissions, expiration/expired status, and the write-restriction summary.
The current “Active tokens” title is inaccurate when expired rows are returned;
use “Tokens” with per-row status. Never render a missing scope field as “All”.
Distinguish PAT and service-key metadata; service keys do not enter this PAT
replacement flow.

Replace the current revoke-then-name-only reissue with a reviewed replacement:

1. Load complete metadata and open the shared form with the original scopes,
   Vault restriction and **absolute** expiration. Preserve unfamiliar existing
   scopes as unsupported; do not silently map admin/write-only to a broader preset.
2. Require explicit review of changes. If the original expiration passed, require
   the user to choose a new expiration; do not extend it automatically.
3. Create the replacement and show its one-time receipt. The original remains
   usable if creation fails; explain the overlap while both exist.
4. Offer an explicit “Revoke original token” action after creation. If revocation
   fails, retain the new secret and show that the original is still active.

If metadata is incomplete on a legacy server, disable automatic replacement and
offer independent new-token creation plus separate revocation. A restriction is
never lost because a list response omitted a field.

## 6. Secret handling, asynchronous state and accessibility

Use the [project design system](../../../../frontend/DESIGN_SYSTEM.md): shared
Input, Label, Button, SelectMenu, Dialog/ConfirmDialog, Alert and CodeSnippet.
Use existing color/radius/motion tokens, one orange Create action, and text/icon
feedback in both themes. The disclosure exposes aria-expanded/aria-controls;
inputs have persistent labels and linked inline errors. On failed submit, expand
invalid fields and focus a linked error summary. Copy success uses a polite status
message; clipboard failure keeps the secret selectable for manual copy.

Store drafts and secrets only in component memory, never storage, URLs, logs,
analytics, notifications or persistent query caches. Keep the one-time secret
until explicit dismissal/navigation; copy does not consume it. Explain that setup
snippets also contain the secret. Masking must also hide the secret from the
accessibility tree; remove the current masked-but-sr-only full-secret pattern.

Capture authSessionSnapshot before discovery and issuance, use the snapshot's
credential/CSRF header, and verify isCurrentAuthSession before displaying results.
Clear any current secret and snippets when the account/session changes. An old
success must never appear in the new user's UI. Do not invoke cleanup with the
new user's credentials. A timeout or lost response may have minted a token: no
automatic retry; show the token list and an explicit next action instead.

Draft editing and an undismissed secret participate in dirty/navigation guards;
an in-flight request participates in busy guards. Prevent duplicate submit with
an immediate ref guard as well as disabled UI. Default basic mode must retain
current connection onboarding behavior. Switching between new-token, saved-token,
and OAuth modes preserves the issuance form session, including draft restrictions,
validation errors and uncertain-result review. Start capability discovery only
when the new-token form is first opened; returning to it must not silently reset
the draft or permit another mint without the explicit result-review action.

## 7. Implementation sequence and acceptance checks

| Step | Files/responsibility | Acceptance |
| --- | --- | --- |
| 1. Backend issuance boundary | auth/access routes, shared issuer policy, explicit machine-issuer configuration and tests | Restricted mint bypass rejected on self/admin aliases; local/SSO human and allowlisted admin service cases enforced, wrong expected identity rejected, CSRF enforced |
| 2. Contract and metadata | strict v1 schemas, capability route, list_pats, expiry service | defaults preserved; null vs missing/empty distinguished; absolute/relative expiry validated; no secret in lists or validation output |
| 3. Shared form/controller | new PAT form/model/client module, ConnectionSetup | basic unchanged; advanced summary/serialization identical on Home and Settings; capability errors fail safely |
| 4. Management | TokensSection and typed PAT metadata | restrictions/status visible; replacement preserves constraints and does not revoke before successful issuance |
| 5. Integrated verification | existing token-scope/SQL tests, form tests, browser E2E | demonstrate the actual read/write matrix, async safety, one-time copy and old-server behavior |

Required regression cases:

- Name-only creation; empty/unknown scopes; empty concrete Vault scope; malformed
  names/prefixes; null vs missing; duplicate normalization; zero/negative/overflow
  expiration; custom UTC and DST/timezone boundaries; past date at submit.
- Human local/SSO issuance, SSO CSRF failure, PAT child-mint denial on self and both
  administrator aliases, admin-owned PAT denial, unapproved/scoped service denial,
  explicit allowlisted admin service success, concurrent suspension and stale identity.
- Read-only denies writes; selected-scope denies out-of-scope ordinary writes but
  preserves permitted ordinary reads; out-of-scope SQL reads denied; ACL revocation
  and future prefix matching remain effective.
- Old server returning 404, server silently ignoring unknown fields, mixed-version
  discovery/POST, malformed receipts, timeout after commit, and missing list scope.
  A failed advanced request must never cause a second unrestricted mint.
- Replacement of an expiring scoped PAT, expired original, write-only/admin
  metadata, creation failure and old-token revocation failure.
- Account change/new same-account SSO cookie during issuance, delayed success/401,
  duplicate submit, manual copy failure, dismiss/unmount cleanup and navigation.
- Keyboard-only disclosure and Vault selection, labelled errors/focus, screen
  reader masking, narrow viewport, long names, light/dark and reduced motion.

Run frontend design:check/typecheck/lint/test and repository scripts/check.sh for
implementation. Use real PostgreSQL and the repository-owned isolated runtime for
scope/expiry/credential behavior. The implemented validation results are recorded below.

## 8. Alternatives and research

| Alternative | Decision |
| --- | --- |
| Frontend-only fields on legacy POST | Reject: no positive support guarantee, missing list scope, and unsafe replacement/mint bypass remain |
| Expand public auth/config | Prefer separate authenticated capabilities: account-bound issuance does not need to depend on the SSO provider catalog |
| Convert custom date to expires_days | Reject: whole-day rounding can grant a token longer than the user's chosen time |
| Redefine vault_scope to restrict all reads | Defer: changes document browsing/search and established write-broad/read-broad behavior; requires a separate cross-surface design |
| Make every default short-lived/read-only | Defer policy change: violates the requested unchanged basic flow; offer clear recommendations and explicit choices |
| Support child PAT mint with attenuation | Defer: must prove coarse scopes, prefix unions, lifetimes, and service-class authority never widen; human self-mint plus explicit privileged machine issuers is the bounded prerequisite |

[GitHub's PAT guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
supports choosing expiration and limited permissions/resources. Adopt those UI
patterns, but do not copy GitHub repository-isolation semantics onto AKB's Vault
write scope. [W3C's form notification guidance](https://www.w3.org/WAI/tutorials/forms/notifications/)
supports field-associated errors and a keyboard-reachable error summary. The
local UI/UX skill's form-validation recommendations agree with this approach.

These are AKB design decisions, not externally imposed policy. Research reviewed
on 2026-09-15; baseline references above document the pre-change findings.

## 9. Design review status

Read-only backend investigation and a separate design review found and addressed
the administrator mint alias, missing list scope, and the risk of widening SQL
reads when switching to Read only. The accepted design is implemented. External
control-plane consumer migration remains a release prerequisite; deployment has
not been performed.

## 10. Implementation and verification

- The shared advanced form is available in Home connection setup and Settings →
  Agent connections. Token metadata and explicit create-then-revoke replacement
  use the same reviewed options.
- Backend validation: 134 unique unit, real PostgreSQL, and compatibility tests passed,
  including exact expiry, transaction rollback, issuer changes, public mint
  aliases, and SSO browser CSRF. The new PostgreSQL suite is registered in CI.
- All required frontend gates passed: design checks, type checks, lint, and
  1,029 tests across 133 files. Explicit America/New_York timezone tests also
  passed. The repository-wide `scripts/check.sh` passed, including a secret scan
  of new files through a temporary index; the actual Git index was not changed.
- Independent backend review identified UTC conversion overflow at supported
  year boundaries. Both schema and service now return a field validation error;
  regression tests cover the upper and lower boundaries.
- A real browser test passed against the disposable repository runtime: 30-day
  scoped issuance, allowed/denied writes, ordinary read semantics, child-mint
  denial, exact replacement expiry/scope, and explicit original revocation.
  Desktop and 390px mobile screenshots were inspected; mobile has no horizontal
  overflow. No existing developer deployment was modified.

To repeat the browser scenario, start the repository-owned local runtime with
frontend support as described in [the CI runtime guide](../../../../scripts/ci/README.md),
then run against its frontend origin (replace the example port if necessary):

```sh
cd frontend
AKB_FE_E2E_MODE=real AKB_PAT_ISSUANCE_E2E=1 \
  AKB_FRONTEND_URL=http://127.0.0.1:18101 \
  pnpm exec playwright test e2e/pat-issuance-live.spec.ts --project=chromium
```

The test creates and deletes its own user and Vaults. It requires local
registration and account self-service cleanup enabled in the isolated runtime.

### Review follow-up — 2026-09-21

Fixed the P2 credential-mode switching issue by keeping the issuance form mounted
once opened and hiding it when another mode is selected. Two regression scenarios
(saved token and OAuth) first failed against the old behavior, then passed after
the fix. They cover retained dirty state, uncertain-result warnings, prevention of
another mint until explicit review, and preservation of the read-only, 30-day,
Vault-prefix restrictions on the next explicitly requested issuance.
