---
status: accepted
stage: implementation
created: 2026-09-15
updated: 2026-09-15
baseline: 30968585
---

# Local account self-service lifecycle

## Problem and scope

Account settings need explicit controls for ending all login sessions and deleting
the current account. The previous self-delete endpoint cascaded through owned
vaults. A confirmation screen needs a server contract that reports ownership
blockers and enforces them again when deletion commits.

This change supports local human accounts. SSO accounts require a separate
identity-provider logout, reauthentication, and reprovisioning policy and remain
unsupported. Personal access tokens (PATs) survive session revocation but are
removed when the account is deleted.

## API contract

All paths below are relative to `/api/v1`. Mutations require an active local
human JWT and `account_self_service_enabled: true`. PATs and service credentials
cannot perform these mutations. GET responses use `Cache-Control: no-store`.

| Endpoint | Contract |
| --- | --- |
| `GET /my/account/lifecycle` | Versioned identity, capabilities, deletion blockers and effects |
| `GET /my/account/deletion-blockers` | Owned vaults, including archived vaults; UUID cursor, default limit 20, maximum 100 |
| `POST /my/account/session-revocations` | Requires `expected_user_id`; ends all local login sessions, including the caller |
| `POST /my/account/deletion` | Requires `expected_user_id`, `confirm_username`, and `current_password` |
| `DELETE /my/account` | Retired; returns 410 with `account_deletion_contract_required` |

Preview schema version 1 includes:

- `user_id` and `username` to bind the displayed account to the operation.
- `revoke_sessions`: `supported`, `scope: local_sessions`, `includes_current`,
  `affects_pats`, and an optional explanatory `reason`.
- `deletion`: `supported`, `allowed`, `reason`,
  `confirmation: username_and_current_password`, structured `blockers`, and
  `effects.active_pats_revoked` / `effects.shared_publications_preserved` counts.

Preview is advisory. A mutation always rechecks current identity, account state,
authorization and relevant blockers. Unknown or missing contracts disable the UI;
clients must never fall back to the retired endpoint.

Session revocation returns `{user_id, revoked_before}`. Account deletion returns
`{deleted: true, user_id}` only after the database transaction commits.

Username confirmation prevents selecting the wrong account. Password confirmation
provides reauthentication. The deletion request uses the same NFC normalization as
registration and login, before wrapping the password in `SecretStr`. Validation
errors omit request values. Failed confirmations consume a separately committed
per-account budget of five attempts per minute.

## Deletion policy and transaction

Deletion is blocked when the account:

- Owns any vault, including an archived vault. Ownership must be transferred or
  the vault separately deleted first.
- Is a protected recovery administrator or retired recovery identity.
- Is an administrator with no other eligible active local human administrator.
- Requires an issued password change, or the cleanup worker is unavailable.

Other administrators retain their existing account-management policy. The admin
route normalizes UUIDs and rejects self-targeting at the service boundary.

The deletion transaction uses `READ COMMITTED` and a bounded lock timeout:

1. Lock eligible administrator rows in UUID order, then lock the target user with
   `FOR UPDATE`. Recheck account state, identity, username and password hash.
2. Count surviving administrators only within the locked ID set. An administrator
   promoted after the lock query cannot satisfy the guard because it could be
   demoted concurrently without waiting for those locks. A retry can observe a
   newly eligible administrator.
3. Recheck vault ownership. The owner foreign key conflicts with the user lock,
   preventing a concurrent vault creation or ownership transfer from bypassing
   the ownership blocker. Deadlocks, lock timeouts and FK races roll back and
   produce `409 account_state_changed`.
4. Insert user/token role cleanup records into an independent outbox. Detach the
   account from grants and shared publications; preserve those shared resources.
5. Emit the account-deleted event and delete the user. User-owned identity,
   notification, membership and credential rows follow existing FK cascades.

No Git, object-store or vector-store deletion runs in this path. All database
changes roll back together. Requests already executing before revocation are
outside the cancellation guarantee.

## Local JWT revocation

Migration 101 adds `users.session_generation BIGINT NOT NULL DEFAULT 0`, constrained
to nonnegative values. Login holds a user share lock while reading the generation
and issuing the JWT. Revocation increments the counter under the same row-lock
ordering. Verification requires an exact integer claim matching the database.
Boolean, string, fractional and negative claims are rejected.

Tokens without a generation claim remain valid only while the stored generation
is zero and their issue time passes the legacy cutoff check. Once the generation
increases, those tokens are rejected. This avoids future-dating new tokens to
escape a whole-second timestamp cutoff.

Migration 101 also installs a cutoff-update trigger. Old processes only update
`tokens_revoked_before`; the trigger advances the generation for those writes,
including equal or backdated cutoff values. New writers that already incremented
the generation are not incremented twice. The cutoff remains monotonic and the
trigger's changes participate in rollback.

`init.sql` runs before pending migrations on existing installations. Its trigger
installation checks that the generation column exists; migration 101 then installs
the column and trigger atomically before new issuers serve traffic.

## Durable role cleanup

Migration 102 creates `account_deletion_cleanup` keyed by `(role_kind, resource_id)`.
It deliberately has no cascading FK to users or tokens. Rows record enqueue time,
attempts, next attempt, completion and a non-secret error class.

A worker claims a bounded batch with `FOR UPDATE SKIP LOCKED` and uses the existing
RoleSync drop primitive. Missing roles are successful no-ops; failures retry with
exponential backoff. The worker verifies that the account/token no longer exists
before dropping its role. Post-commit cleanup failure never turns a completed
account deletion into a misleading synchronous failure.

The worker refreshes a database heartbeat. Deletion requires a heartbeat within
60 seconds. Authenticated `/health` exposes `account_deletion_cleanup` with pending
count, oldest pending time and readiness. The existing role reconciler remains a
secondary recovery mechanism.

## Security settings UI

The Security tab follows the existing [design system](../../../../frontend/DESIGN_SYSTEM.md).
It explains that session revocation also signs out the current session while PATs
remain active. The danger zone shows deletion effects and owned-vault links.
Deletion proceeds through impact/password review and exact username confirmation.

Each operation captures an authentication snapshot and pins its request credential.
A response from an earlier account or session cannot clear a newer session. Only a
validated successful receipt clears the matching credential and private caches.

While a lifecycle mutation is pending, background authentication failures do not
interrupt its result handling. A 30-second timeout bounds pending state. Network
errors, timeouts, malformed receipts and 401 responses never imply successful
deletion. The UI provides status rechecking or explicit reauthentication, and
preserves cancellation focus and navigation protection during submission.

## Rollout and compatibility

1. Apply migration 101 and its cutoff trigger before new JWT issuers accept traffic.
   Apply migration 102 and deploy the API and cleanup worker.
2. Replace or drain all old issuers and verifiers. Their claimless tokens are
   rejected by new verifiers once an account's generation exceeds zero; the trigger
   protects revocation but does not guarantee seamless mixed-version login. Old
   processes also retain the retired self-delete endpoint. The default Kubernetes
   `Recreate` strategy avoids overlapping old and new request handlers.
3. Verify the worker heartbeat and enable `account_self_service_enabled: true`.
   The default is false; unsupported deployments display an explanatory state.
4. Validate deployment behavior with disposable accounts. Do not use real accounts
   for destructive smoke tests. A rollback to generation-unaware verifiers needs a
   verified forced-logout procedure, such as rotating the local signing keyset.

No component version bump or deployment is included in this change.

## Validation

- PostgreSQL tests cover rapid login/revoke, legacy cutoff-only revocation with the
  feature disabled, password changes, equal/backdated timestamps, rollback, row-lock
  ordering, and init/migration/restart compatibility.
- Deletion tests cover archived vaults, ownership races, concurrent administrator
  deletion, administrator promotion/demotion, durable cleanup retries, canonical
  UUID self-targeting, password validation and NFC/NFD confirmation.
- Frontend tests cover supported/unsupported contracts, auth snapshots, background
  401 handling, timeout, cancellation and confirmation flows.
- `frontend/e2e/account-security-live.spec.ts` runs against the isolated runtime
  with `AKB_ACCOUNT_SELF_SERVICE_E2E=1`: two JWTs are revoked, PAT remains valid,
  then reauthentication and account deletion invalidate both JWT and PAT.
- `scripts/check.sh` runs the repository static, SDK, frontend and secret checks.
  See [the runtime guide](../../../../scripts/ci/README.md) for the isolated E2E gate.
