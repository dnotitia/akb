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
to the other mode. Legacy configuration may be translated during an explicit,
bounded migration, but it does not become a third runtime mode.

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
source. If parsing, signature validation, claim validation, account projection,
or account-status validation fails, the request is rejected. The system does
not try another profile or fall back to local authentication.

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
| Permanent | One canonical mode; no hybrid or failure fallback; mode-selected verification; no AKB user session minted in `sso`; common principal-to-actor authorization; separate ordinary and product-admin surfaces; PAT/service orthogonality | Changing one requires an ADR that explicitly supersedes this record. |
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

Legacy hybrid compatibility, if required, may help an upgrade select and
persist one canonical mode; it must not trial multiple verifiers or keep both
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
| Mode boundary | Mode-specific positive paths; wrong-mode and malformed credentials; header-directed verifier and key-source attacks; no verifier fallback; shared `AkbActor` authorization; no AKB user-session issuance in `sso`; any bounded symmetric-session migration. |
| Installation and admin bootstrap | Canonical configuration and upgrade validation; key persistence and rollover for the selected local profile; explicit product-admin provisioning; mode-appropriate admin recovery; any bounded legacy-hybrid migration. |
| Login and IdP control | UI and API capability agreement by mode; enabled-provider projection; fail-closed login-options behavior; disabled-provider and secret redaction; separation of ordinary login from product-admin control. |
| Browser session cutover | OIDC access, refresh, expiry, logout, revocation, and account suspension across AKB and first-party clients; proof that the `sso` path does not issue, store, or forward an AKB user session token. |

Permanent tests assert externally observable authority, allow/deny, projection,
and authorization outcomes. Versioned tests name the profile or schema version
they cover. Migration-only tests state their deletion gate. Tests must not
freeze internal call graphs, class names, file names, field ordering, or an
exhaustive route and claim matrix.

## Open Decisions

The following remain open until their owning implementation phase:

- the first asymmetric local profile's algorithm, key parameters, claims,
  storage, publication, and rotation details;
- immediate reauthentication versus a bounded legacy symmetric-session window,
  including the window's exact duration;
- whether legacy hybrid installations require a bounded compatibility release
  or must select a canonical mode before upgrade;
- the exact configuration migration and invalid-combination diagnostics;
- OIDC admission, just-in-time provisioning, and explicit account-linking
  policies within the common projection boundary;
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
