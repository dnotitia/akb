---
status: proposal
stage: planning
created: 2026-09-12
updated: 2026-09-12
issue: dnotitia/akb#529
---

# Provider-Authoritative Email Domains (browser login only)

## Context

An installation that authenticates against a single corporate directory has no
way to say so. With `keycloak_enrollment_mode: invite_only`, every user on the
directory's domain is refused once with `membership_required`, an arrival is
recorded, and an administrator binds each one — mechanically (issuer matches,
email domain allowed, exactly one active human account carries the address, no
binding yet). Switching to `open` does not help: the insert collides on the
email unique constraint and the person is refused with
`ExternalIdentityConflictError` instead.

The governing records are
[Workspace Account Governance](../accepted/2026-07-10-workspace-account-governance/README.md)
(exact `(issuer, subject)` binding; email is mutable profile data, never an
identity key) and the
[Generic OIDC Provider](../accepted/2026-08-28-generic-oidc-provider/README.md)
reaffirmation that email never adopts an existing account. The refusal is
correct as a default and wrong as an absolute: where a single corporate
directory is the authority for its own domain, the only party that could assert
someone else's address is the directory administrator, who can already reset
that person's password and sign in as them.

`keycloak_enrollment_mode` is install-wide (`backend/app/config.py`), so a
fourth mode cannot describe two doors: the local realm (the installation's own
auth path, where the recovery admin lives) must keep behaving exactly as it
does. The decision belongs on the provider.

## Decision

Add one installation-owned setting, evaluated only on the **browser login**
path, only when no exact `(issuer, subject)` binding exists:

```yaml
keycloak_authoritative_email_domains_by_provider:
  workforce: ["example.com"]
```

| condition (browser callback, verified principal, alias-bound) | result |
| --- | --- |
| domain declared for this alias, an account has that email | **adopt** — write the binding, sign in |
| domain declared for this alias, no account has that email | **provision** — create, sign in (existing open-path) |
| domain not declared | unchanged — record the arrival, refuse |

Empty by default: every existing installation behaves exactly as it does now,
and a provider added later starts closed.

Scope is deliberately narrow:

- **Browser login only.** The callback already threads a verified
  `provider_alias` through `_require_signed_provider`
  (`backend/app/api/routes/auth.py`) for both access and ID tokens. The alias
  is passed into projection so the authority decision is keyed by the provider
  the flow selected. REST/MCP bearer verification
  (`KeycloakOIDC.verify_access_token`) does not inspect `identity_provider`
  today; extending trust there is a separate decision and is out of scope.
  PAT resolution (`_resolve_pat` in `backend/app/services/auth_service.py`) is
  untouched — it reads the token row plus active account and knows no
  enrollment, provider, or domain concept. Existing PATs keep working; only the
  account's `auth_provider` flips from `local` to `keycloak` at adoption, which
  disables local-password login for that account by the existing rule.
- **`disabled` stays disabled.** The authority overrides `invite_only` and the
  collision branch of `open` for declared domains only. It never admits
  anything when enrollment is `disabled`.
- **Exact match on domains.** Lowercase, strip one trailing dot, exact equality
  only — no subdomain inheritance. The `local` alias can never carry domains
  (fail-closed at config load).
- **Four adoption guards** (account ambiguity is impossible — `users.email` is
  unique): `email_verified` per the existing setting; target is an active human
  account (suspended must not be revived by signing in); target has no binding
  for this issuer yet (`external_identities` is unique on `(issuer, subject)`,
  not on `(issuer, user_id)` — a code check); target is not the recovery admin
  (a break-glass path is never claimable by an IdP assertion).
- **Notice, not proof.** No verification mail (none exists in this codebase).
  Provisioning already emits `auth.user_provisioned`; adoption emits a new
  distinct `auth.user_adopted` event (payload carries `is_admin` legibility,
  domain, issuer, subject, prior `auth_provider`) so subscribers can tell
  "created" from "claimed".

## Compatibility

Additive and default-closed. No schema change: the setting is a validated
`dict[str, list[str]]` on `Settings` next to `keycloak_enrollment_mode`, with
unknown-key rejection inherited from the existing config contract.

- `keycloak_link_by_email: true` remains rejected at canonical load; this
  feature is not that flag under another name (it is per-provider,
  domain-scoped, browser-only, guard-checked, and event-audited).
- Existing tests pinning "never adopts by email"
  (`backend/tests/test_workspace_external_identity.py`) stay green: they run
  with the default empty map.
- Rollback is a config revert plus code revert; bindings already written are
  exact `(issuer, subject)` rows indistinguishable from manually approved ones,
  which is intended.
- Helm: `sso.authoritativeEmailDomains` map rendered into
  `keycloak_authoritative_email_domains_by_provider`; unset renders nothing.

## Acceptance criteria

- Unit: domain normalization (case, trailing dot, IDNA/empty rejection);
  `local` alias rejected; unknown alias is inert (never matches a login).
- Postgres: declared-domain adopt writes the exact binding and clears the
  pending admission; declared-domain provision creates and signs in; undeclared
  domain still records + refuses; `open`-mode collision on a declared domain
  adopts instead of `identity_conflict`.
- Guards: unverified email refused; suspended account refused (and stays
  suspended); already-bound-for-issuer refused; recovery admin refused.
- Concurrency: two simultaneous adopts of the same email converge to one
  binding (advisory-lock pattern per the existing ensure path).
- Events: adopt emits `auth.user_adopted` (distinct from
  `auth.user_provisioned`) with admin legibility; provision path unchanged.
- PAT regression: pre-existing PATs resolve before and after adoption; no
  change to `_resolve_pat`.
- `scripts/check.sh` green; focused suites
  (`test_workspace_external_identity.py`,
  `test_pending_admission_postgres.py`, config-shape unit) green.
