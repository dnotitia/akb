---
status: accepted
stage: applied
created: 2026-07-10
updated: 2026-07-10
---

# Workspace Account Governance

## Decision

AKB keeps its existing local user UUID as the authorization principal. OIDC is
an external authentication binding, not a replacement user model:

```text
verified OIDC (issuer, subject)
  -> external_identities.user_id
  -> users.id
  -> existing vault ownership, grants, events, JWTs, PATs, and PG roles
```

The extension is additive and opt-in. Standalone AKB retains local password
authentication and verified-email OIDC JIT by default. A managed deployment can
pre-provision exact external identities, disable JIT, and disable every local
password lifecycle operation server-side.

## Compatibility Contract

- Existing `users.id` values never change.
- Existing JWT `sub`, HS256 validation, PAT formats, token IDs, scopes, Vault
  ownership, grants, audit actor IDs, and PostgreSQL role names do not change.
- Existing users migrate to `account_status=active` and `account_kind=human`.
- Existing INSERT statements remain valid because both columns have defaults.
- `local_auth_enabled` defaults to `true`.
- `keycloak_enrollment_mode` defaults to `open`.
- Existing active JWTs and PATs continue to resolve after migration.
- The pre-change backend must register, log in, and mint a PAT against the
  expanded schema before release.

## Schema

`users` gains:

```text
account_status: active | suspended  DEFAULT active
account_kind: human | service       DEFAULT human
```

Stable external identities live separately:

```text
external_identities
- id UUID
- user_id UUID -> users.id
- issuer TEXT
- subject TEXT
- email_snapshot TEXT NULL
- created_at / last_seen_at

UNIQUE (issuer, subject)
```

Email is mutable profile data. Only the exact verified `iss` and `sub` claims
form the external key. Multiple bindings may point to one user only through an
explicit administrator request that names the existing AKB user ID, which is
the reviewed issuer-migration path.

Token-role DDL cleanup is post-commit and therefore has a durable retry ledger:

```text
account_token_cleanup
- token_id UUID primary key
- user_id UUID
- requested_at / completed_at
- attempts / last_error
```

The bearer token is deleted before this row is written; the ledger stores only
non-secret token IDs.

## Authentication Policy

`local_auth_enabled=false` rejects all of these through one service guard:

- registration;
- username/password login;
- self-service password change;
- administrator password reset; and
- CLI password reset.

The error is 403 with code `local_auth_disabled` and is raised before password
hashing or verification. The setting does not disable OIDC, JWT/PAT resolution,
MCP OAuth, or service credentials. `keycloak_sso_only` remains a UI redirect
hint and cannot bypass or replace this server policy.

OIDC admission is one of:

| Mode | Exact binding | Verified-email JIT/link |
| --- | --- | --- |
| `open` | allow | allow under the existing email policy, then persist binding |
| `invite_only` | allow | deny with `membership_required` |
| `disabled` | deny | deny |

Resolution order is exact `(issuer, subject)` first. Exact bindings do not
require an email claim. The open-mode fallback requires the configured email
verification policy and refuses to attach a second subject to an already-bound
user. OIDC/service profile identity fields are externally managed and cannot
be self-edited.

## Account Types

Human users may be local or OIDC-backed. Service users are created only through
an administrator API with:

- `account_kind=service`;
- `auth_provider=service`;
- an unusable password sentinel;
- `is_admin=false`; and
- no first-user bootstrap participation.

Service users authenticate only with issued non-interactive credentials. The
password change/reset and OIDC paths reject them. Password lifecycle requests
for Keycloak or service users fail with the stable
`password_lifecycle_unavailable` code.

## Administrative API

The existing AKB administrator boundary owns explicit operations for:

- ensuring a human user and exact external identity;
- resolving by external identity or AKB user ID;
- ensuring a service user;
- projecting human `is_admin` state;
- suspending or activating an account;
- minting an exact-user scoped token through the existing token endpoint; and
- revoking an exact user-owned token with strict PG role cleanup.

There is no generic proxy endpoint. Every request produces a bounded audit
event and carries the authenticated administrator actor ID.

## Suspension Transaction

Suspension locks the user and atomically:

1. sets `account_status=suspended`;
2. advances `tokens_revoked_before`;
3. deletes every token and returns its token ID;
4. records pending token-role cleanup rows; and
5. emits session and account suspension events.

Every resolver checks active status. Local login and JWT resolution hold a
shared user-row lock until authentication is decided, so they cannot read
`active` before a concurrent suspension and return a usable authentication
result after that suspension commits. PAT resolution remains atomic with token
deletion and checks the joined user status.

After commit, strict `DROP ROLE IF EXISTS akb_token_<id>` runs for every token.
Failure returns `credential_cleanup_incomplete`, while account and bearer denial
remain committed. Repeating suspension retries pending ledger rows. The normal
best-effort hook and startup role reconciler remain secondary safety nets.

Activation changes only account status. It never recreates a JWT, PAT, service
key, password, or deleted token role.

## Rollout

1. Apply migration 043 with compatibility defaults.
2. Prove the pre-change backend against the expanded schema.
3. Deploy the new backend with default `open` and local auth enabled.
4. Backfill exact bindings and resolve collisions without changing user IDs.
5. Prove administrator ensure/suspend/token operations.
6. For a managed deployment, enable `invite_only` only after all active users
   are bound; disable local auth only after direct known-password denial is
   proven.

Rollback before managed cutover may use the pre-change backend because the
schema is additive. After invite-only/local-auth cutover, rollback must keep a
backend that enforces both admission and local-password policy or block human
authentication ingress until such a backend is restored.

## Verification

- Migration is idempotent and preserves a legacy user ID.
- Pre-change registration, login, and PAT mint pass on the expanded schema.
- Default local login and open OIDC behavior remain compatible.
- Exact subject binding survives email changes.
- Invite-only rejects an unbound realm user.
- Suspended users fail local, OIDC, JWT, PAT, service, publishable, and MCP OAuth
  resolution paths.
- Login/JWT/OIDC suspension races fail after the suspension commits.
- Service users cannot log in or receive a password reset.
- Concurrent ensure calls converge to one user.
- Strict token-role cleanup is durable and retryable.

The executable release gate is:

```bash
AKB_TEST_DSN=postgresql://... scripts/check-workspace-account-governance.sh
```

It runs the focused policy/static suite and starts the pinned pre-change backend
against the expanded schema before exercising registration, local login, and
PAT minting.

## Non-Goals

- Making OIDC mandatory for standalone AKB.
- Re-keying users or changing token formats.
- Encoding workspace membership or product roles in Keycloak.
- Deleting user-owned Vaults during offboarding.
- Treating email as a permanent identity key.
