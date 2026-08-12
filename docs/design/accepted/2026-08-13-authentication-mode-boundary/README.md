---
status: accepted
stage: design
created: 2026-08-13
updated: 2026-08-13
---

# Authentication Mode Boundary

This is the Phase 0 architecture decision for
[#357](https://github.com/dnotitia/akb/issues/357). Implementation is pending.
The record fixes the authority and security boundaries that later phases must
preserve without fixing their route layout, claim schema, or code structure.
Public revision and review summaries are recorded in
[`rounds/`](rounds/README.md) and [`feedback/`](feedback/README.md).

## Relationship to Workspace Account Governance

This ADR partially supersedes
[Workspace Account Governance](../2026-07-10-workspace-account-governance/README.md).
The supersession is prospective and limited to these human-authentication
contracts:

- human-auth mode defaults now come from the single canonical
  `auth_mode=local|sso` choice;
- browser session authority and issuance are mode-specific, including the rule
  that `sso` does not mint an AKB user session token;
- local and OIDC human login are not a permanent hybrid runtime mode; and
- HS256 compatibility for legacy AKB human sessions is migration-only, not a
  permanent compatibility promise.

The older ADR remains authoritative for durable `users.id` account projection,
exact external identity binding, account status, PAT and service identities,
Vault ownership and grants, PostgreSQL roles, and the shared AKB authorization
boundary. Its account-governance schema, lifecycle, suspension, and
administrative controls are likewise unchanged by this ADR except where they
refer specifically to the superseded human-session behavior above.

The older text remains the historical record of its applied rollout. Where the
two records conflict within the four categories above, this ADR governs the
target architecture; outside them, the older ADR continues to govern.

## Context

Authentication settings that evolve independently can accidentally describe a
hybrid deployment, make the login UI disagree with backend policy, or let an
untrusted token influence how it will be verified. They also blur two separate
responsibilities: proving an identity and authorizing an AKB account.

AKB needs one install-time choice for human authentication while preserving its
existing account and authorization model. Personal access tokens (PATs) and
service credentials must continue to work as non-browser credentials under
their own policies.

## Decision

### One canonical mode

Every installation has exactly one canonical install-time setting,
`auth_mode=local|sso`:

```text
auth_mode = local | sso
```

`local` and `sso` are mutually exclusive security modes. There is no new
hybrid mode, and an unavailable or failed authentication path never falls back
to the other mode.

A fresh install with a missing or unknown `auth_mode` fails installation or
startup before serving an authentication surface. It must not silently default
to `local`. Only an explicit legacy migration may derive a mode from existing
settings, and only when those settings identify one mode unambiguously. The
migration persists the derived canonical mode before normal startup; ambiguous
legacy state fails closed and requires an explicit operator choice.

### Credential authority

In `local` mode, AKB remains the human login authority and continues to issue
an AKB application session token. New tokens use a named, versioned asymmetric
verification profile. The concrete algorithm, key parameters, claims, key
storage, and rotation protocol belong to that profile rather than to the
permanent `local` contract.

In `sso` mode, AKB trusts only the configured OIDC authority for human login
and user access credentials. A successful login does not mint an AKB user
session token. Refresh credentials, if used, remain under the OIDC authority's
protocol and are not accepted as AKB API bearer credentials.

Every versioned OIDC verifier profile binds three mandatory semantic
invariants: the exact configured issuer, the intended AKB audience/resource,
and the accepted credential/token type. A cryptographically valid credential
that is missing or mismatches any of them is rejected before account
projection, with no retry against another profile and no authentication
fallback. Concrete identifiers and the provider-specific representation of
these semantics belong to the versioned profile; this ADR does not prescribe
claim field names.

PATs and service credentials are orthogonal to the human login mode. Each
trusted API surface declares whether it accepts them and what scope they carry;
selecting `local` or `sso` neither reclassifies them as user sessions nor grants
them universal access.

### Fail-closed verifier selection

The canonical mode and the trusted credential surface select the allowed
credential class and verifier profile before token-controlled header fields
are considered. A selected profile may read a JOSE header only to enforce that
the token exactly matches that profile's algorithm and trusted key source.

The untrusted `alg` header never selects an algorithm, verifier, issuer, or key
source. Exact issuer, intended AKB audience/resource, and accepted
credential/token type are validated by the already-selected profile. A
mismatch is rejected before a verified principal is accepted or account
projection begins. If parsing, signature validation, other profile validation,
projection, or account-status validation fails, the request is rejected. The
system does not try another profile or fall back to local authentication.

### One authorization path

All accepted human, PAT, and service credentials converge after verification:

```text
trusted mode and credential surface
  -> selected verifier profile
  -> VerifiedPrincipal
  -> account projection
  -> AkbActor
  -> shared AKB authorization
```

`VerifiedPrincipal` records only identity and credential facts established by
the selected verifier. Account projection binds those facts to an AKB account.
`AkbActor` then carries account state, product-admin status, grants, and
credential scope into the existing authorization boundary. OIDC roles do not
silently become AKB permissions.

These names define architectural contracts, not required class names or file
locations. Their representations may evolve without splitting authorization
by login mode.

### Separate user and product-admin surfaces

Ordinary login and product-admin recovery/control are distinct entry points
and policy surfaces.

- In `local` mode, ordinary login exposes only enabled local login,
  registration, and password capabilities.
- In `sso` mode, ordinary login exposes only enabled, configured OIDC login
  options. Local login, registration, password recovery, and hidden local
  escape paths remain unavailable in both UI and backend policy.
- If public login options cannot be loaded or validated, ordinary login is
  unavailable; it is not guessed to be local.
- Product-admin access uses a mode-appropriate bootstrap and recovery path
  that remains separate from ordinary login. In `sso` mode it must not reopen
  AKB local user login.
- Product-admin authorization is still an explicit AKB authorization decision
  represented by `AkbActor`, not an automatic consequence of authenticating at
  an OIDC provider.

Exact URLs, request and response shapes, bootstrap credentials, and recovery
mechanics are deliberately deferred.

## Contract Lifetimes

Every implementation contract and its tests must be classified as one of the
following.

| Class | Contracts fixed by this ADR | Change or retirement rule |
| --- | --- | --- |
| Permanent | One required canonical mode with no silent default; no hybrid or failure fallback; mode-selected verification; exact OIDC issuer, AKB audience/resource, and credential/token type validation before projection; no AKB user session minted in `sso`; common principal-to-actor authorization; separate ordinary and product-admin surfaces; PAT/service orthogonality | Changing one requires an ADR that explicitly supersedes this record. |
| Versioned | Local asymmetric session profile; OIDC access-token profile; public login-options and admin-control schemas | Introduce a new named version with compatibility, migration, and retirement rules. Do not silently widen an existing verifier profile. |
| Migration-only | Acceptance of legacy symmetric AKB sessions and any compatibility for legacy hybrid configuration | Must have a bounded entry condition and the retirement criteria below. Remove its code, configuration, and tests when retired. |

### Migration-only retirement criteria

Legacy symmetric sessions are never newly issued once the asymmetric local
profile is active. An upgrade must choose either immediate reauthentication or
a bounded acceptance window. If a window is chosen, the legacy verifier is
available only in `local` mode and only for tokens issued before cutover. It is
removed after the maximum validity of those tokens and the declared rollback
window have both elapsed. The legacy signing secret, configuration, verifier,
and migration tests are removed together.

Legacy hybrid compatibility, if required, may derive and persist one canonical
mode only through an explicit migration and only from unambiguous legacy state.
It must not silently choose `local`, trial multiple verifiers, or keep both
ordinary login methods active. It is removed once every supported upgrade path
persists `auth_mode`, ambiguous legacy combinations are rejected, and the
declared rollback window has closed. Its compatibility flags, adapters, and
tests are removed together.

The exact duration of either window is an open release decision, not a
permanent product capability.

## Just-in-Time Test Ownership

Phase 0 validates this ADR as documentation. Executable behavior tests belong
to the phase that introduces the behavior and begin with that phase's smallest
failing test.

| Later phase | Tests it owns |
| --- | --- |
| Mode boundary | Mode-specific positive paths; missing or mismatched exact OIDC issuer, intended AKB audience/resource, and accepted credential/token type; wrong-mode and malformed credentials; header-directed verifier and key-source attacks; rejection before projection; no verifier retry or fallback; shared `AkbActor` authorization; no AKB user-session issuance in `sso`; any bounded symmetric-session migration. |
| Installation and admin bootstrap | Fresh-install rejection of missing or unknown `auth_mode`; explicit unambiguous legacy derivation and persistence; rejection of ambiguous legacy state; key persistence and rollover for the selected local profile; explicit product-admin provisioning; mode-appropriate admin recovery; any bounded legacy-hybrid migration. |
| Login and IdP control | UI and API capability agreement by mode; enabled-provider projection; fail-closed login-options behavior; disabled-provider and secret redaction; separation of ordinary login from product-admin control. |
| Browser session cutover | OIDC access, refresh, expiry, logout, revocation, and account suspension across AKB and first-party clients; proof that the `sso` path does not issue, store, or forward an AKB user session token. |

Mode-boundary acceptance requires:

- a credential matching the selected OIDC profile's exact issuer, intended AKB
  audience/resource, and accepted credential/token type can proceed to
  projection;
- a missing or mismatched value for any of those three invariants is rejected
  before projection creates or resolves an account; and
- rejection does not invoke another verifier, derive another credential type,
  or fall back to local authentication.

Installation-boundary acceptance requires:

- a fresh install with missing or unknown `auth_mode` fails closed before the
  service accepts authentication traffic; and
- a legacy migration derives and persists a mode only from unambiguous state,
  while ambiguous state requires an explicit choice.

Permanent tests assert externally observable authority, allow/deny, projection,
and authorization outcomes. Versioned tests name the profile or schema version
they cover. Migration-only tests state their deletion gate. Tests must not
freeze internal call graphs, class names, file names, field ordering, or an
exhaustive route and claim matrix. OIDC tests assert the issuer,
audience/resource, and credential-type semantics rather than provider-specific
claim field names.

## Open Decisions

The following remain open until their owning implementation phase:

- the first asymmetric local profile's algorithm, key parameters, claims,
  storage, publication, and rotation details;
- the concrete OIDC issuer and AKB audience/resource identifiers, accepted
  credential type, and provider-specific representation selected by the first
  versioned OIDC profile;
- immediate reauthentication versus a bounded legacy symmetric-session window,
  including the window's exact duration;
- whether legacy hybrid installations require a bounded compatibility release
  or must select a canonical mode before upgrade;
- the exact configuration migration and invalid-combination diagnostics;
- whether a later versioned decision should change the existing OIDC admission,
  just-in-time provisioning, or explicit account-linking policies; until then,
  Workspace Account Governance remains authoritative;
- browser-side session transport and server-side token custody, refresh,
  logout, and revocation mechanics;
- product-admin bootstrap credentials, recovery ceremony, and exact control
  endpoints; and
- versioned public login-options and admin-control API shapes.

No implementation should infer one of these choices from this ADR. The owning
phase must record the decision with its version and migration consequences.

## Non-Goals

- Implementing backend, frontend, installer, or configuration changes in
  Phase 0.
- Defining exhaustive route, credential, error, or OIDC claim matrices.
- Fixing concrete algorithm names, key sizes, claim names, key formats, or
  rollover mechanics in the permanent contract.
- Prescribing internal classes, files, call graphs, libraries, or provider
  hosting details.
- Adding a hybrid login mode, an algorithm fallback, or a failure escape to
  local authentication.
- Replacing PATs or service credentials, or moving AKB authorization into OIDC
  roles.
- Selecting provider-specific administration, embedded-client login, or
  post-quantum token designs.

## Consequences

The mode boundary becomes simpler to reason about and test, and authentication
providers can change without duplicating AKB authorization. The tradeoff is an
explicit upgrade and session migration for installations that currently rely
on symmetric local sessions or hybrid behavior. Deferring profile and protocol
details keeps that migration adaptable while the permanent fail-closed
boundaries remain reviewable.
