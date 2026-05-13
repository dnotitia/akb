# Password recovery — design

**Date**: 2026-05-13
**Status**: Approved (brainstorming)
**Owner**: kwoo24
**Related**: `auth_service.py` (`register`, `login`, `verify_password`, `hash_password`), `users` table in `backend/app/db/init.sql:14-23`, existing admin routes in `backend/app/api/routes/access.py`

## Problem

There is no way to recover a forgotten password today. The login form on `frontend/src/pages/auth.tsx` has no "Forgot password?" link. The backend exposes `register`, `login`, `me`, and PAT management — and nothing else. A user who forgets their password is locked out forever.

The repo is open-source and self-hosted. Operators are expected to deploy it without external services, so a password-recovery design that hard-requires SMTP, an SMS gateway, or an external IdP would push setup burden onto every operator. Yet the existing auth surface has **no** out-of-band identity-verification channel — `email` is a column on `users` but is not verified at registration, JWTs are stateless (no sessions table), there are no security questions, no MFA, no recovery codes.

The architectural reality: any *self-service* reset requires adding a new verification channel (cost: registration UX or operator config). Avoiding that means recovery is *administrator-mediated* — a human admin verifies identity out-of-band (Slack / in person / whatever) and issues a temporary credential.

We pick the admin-mediated path. It matches the codebase's prevailing simplicity (one `users` table, stateless JWT, no MFA, no email verification) and follows the dominant OSS pattern documented by Gitea, GitLab, Nextcloud, Mastodon, Sentry, Authentik, et al. — all of which ship an admin UI reset action plus a server-side CLI for the admin-lockout case. Self-service can be layered on later (SMTP-optional or backup codes) without touching this design's primitives.

## Non-goals

- **Self-service forgot-password via email.** No SMTP integration. The login page's "Forgot password?" link routes to a static "contact your admin" page.
- **Backup recovery codes.** Out of scope for the initial implementation. Considered and deferred — registration UX cost is not justified at the current user scale (single user / small team).
- **MFA / TOTP / WebAuthn.** Out of scope.
- **Email verification of the `email` column at registration.** The column exists but is unused; this design does not change that. If SMTP self-service is added later, verification becomes relevant then.
- **One-time reset URLs / reset-token table.** The temp-password model uses the existing `password_hash` column directly; no new `password_reset_tokens` table is introduced. Token-based reset URLs are a stronger UX (no admin-visible password) but were rejected as over-engineering for the threat model and codebase simplicity.
- **Force "must change password" on first login.** Considered — would require a new `users.must_change_password` column, login-response flag, frontend redirect logic. Deferred on YAGNI grounds. The admin tells the user "log in with this temp password and change it from settings"; user is trusted to follow.
- **Revoking existing JWTs on password reset.** JWT is stateless in the current codebase and cannot be revoked without adding a tracking column (`password_changed_at` on `users`, checked at JWT verify time). Old JWTs continue to validate until their natural expiry (`jwt_expire_hours`). PAT revocation on reset is also out of scope — the existing PAT lifecycle stands. Both are documented gaps; if the threat model later requires immediate invalidation, the work is small (single column + check) but is not in this spec.
- **Rate limiting on the admin reset endpoint.** Admins are trusted; rate limit is not a meaningful protection. Audit (event log) instead.

## Architecture

```
┌────────────────────────────────────────────────────────┐
│  password_service.py  (NEW)                            │
│                                                        │
│    generate_temp_password() -> str                     │
│      secrets.token_urlsafe(9) → "Xk7m-Pq4r-A9wL"       │
│      (~50 bits entropy, dash-grouped for readability)  │
│                                                        │
│    async reset_password(user_id, actor_id) -> str      │
│      1. resolve user (404 if missing)                  │
│      2. temp = generate_temp_password()                │
│      3. users.password_hash = bcrypt(temp)             │
│      4. emit_event('auth.password_reset',              │
│                    actor=actor_id,                     │
│                    payload={user_id, username,         │
│                             method: 'admin_ui'|'cli'}) │
│      5. return temp                                    │
└────────────────────────────────────────────────────────┘
        ▲                                  ▲
        │ is_admin == true                  │ container shell
        │                                   │ (no auth — physical access)
[REST handler]                            [CLI handler]
POST /admin/users/{id}/                   python -m app.cli
     reset-password                         reset-password <username>


┌────────────────────────────────────────────────────────┐
│  auth_service.change_password(user_id, current, new)   │
│    1. verify_password(current, row.password_hash)      │
│    2. validate new (≥8 chars, ≠ current)               │
│    3. users.password_hash = bcrypt(new)                │
│    4. emit_event('auth.password_changed',              │
│                  actor=user_id, payload={user_id})     │
└────────────────────────────────────────────────────────┘
                       ▲
                       │ JWT or PAT (logged-in user)
[REST handler]
POST /auth/change-password
```

The two backend services are intentionally separated: `password_service.reset_password` is the **admin/CLI** capability (no current-password proof required), `auth_service.change_password` is the **user** capability (proof of current password required). The `users.password_hash` mutation lives in `password_service` to keep the temp-password generation + audit colocated; `auth_service.change_password` mutates `password_hash` directly without going through `password_service` because the semantics differ (no temp generation, different audit event).

## Backend

### New file: `backend/app/services/password_service.py`

```python
"""Password reset (admin/CLI-mediated).

Used by:
  - REST POST /admin/users/{user_id}/reset-password (admin auth)
  - CLI `python -m app.cli reset-password <username>` (shell access)

Both call reset_password() and surface the returned temp password to the
caller. The caller is responsible for getting that password to the user
out-of-band (Slack DM, in person, etc.). The temp password is bcrypt-hashed
into users.password_hash and is never persisted in plaintext.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from typing import Literal

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories.events_repo import emit_event
from app.services.auth_service import hash_password


def generate_temp_password() -> str:
    """12-char URL-safe random password with dash grouping for readability.

    secrets.token_urlsafe(9) → ~12 base64url chars → ~50 bits entropy.
    Dash-grouped (4-4-4) so the admin can read it aloud or copy-paste
    without breaking on selection.
    """
    raw = secrets.token_urlsafe(9)
    # Trim to 12 chars (token_urlsafe(9) → 12 chars), then group.
    raw = raw[:12]
    return f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}"


async def reset_password(
    *,
    username: str,
    actor_id: str | None,
    method: Literal["admin_ui", "cli"],
) -> tuple[str, str]:
    """Generate a temp password, replace the user's password_hash, emit audit.

    Returns (temp_password, username). `actor_id` is None for CLI invocations
    (no authenticated principal); audit event carries `method` to distinguish.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id, username FROM users WHERE username = $1",
                username,
            )
            if row is None:
                raise NotFoundError("User", username)

            temp = generate_temp_password()
            await conn.execute(
                "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
                hash_password(temp),
                row["id"],
            )
            await emit_event(
                conn,
                "auth.password_reset",
                ref_type="user",
                ref_id=str(row["id"]),
                actor_id=actor_id,
                payload={
                    "user_id": str(row["id"]),
                    "username": row["username"],
                    "method": method,
                },
            )
    return temp, row["username"]
```

### Modified: `backend/app/services/auth_service.py`

Append after `login`:

```python
class BadPasswordChange(Exception):
    """Raised when change_password is called with input the user can correct.

    Distinct from ValidationError (which the global handler maps to 422 — a
    pydantic-shaped validation failure). These cases are HTTP 400: the
    request reached the service, the user just chose a bad new password.
    The route maps this to HTTPException(400) so the frontend can render an
    inline form error.
    """


async def change_password(user_id: str, current: str, new: str) -> None:
    """Change own password. Verifies current; rejects too-short or unchanged."""
    if len(new) < 8:
        raise BadPasswordChange("New password must be at least 8 characters")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_hash FROM users WHERE id = $1",
            uuid.UUID(user_id),
        )
        if row is None:
            raise NotFoundError("User", user_id)
        if not verify_password(current, row["password_hash"]):
            raise AuthenticationError("Current password is incorrect")
        if verify_password(new, row["password_hash"]):
            raise BadPasswordChange("New password must differ from current")

        async with conn.transaction():
            await conn.execute(
                "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
                hash_password(new),
                uuid.UUID(user_id),
            )
            await emit_event(
                conn,
                "auth.password_changed",
                ref_type="user",
                ref_id=user_id,
                actor_id=user_id,
                payload={"user_id": user_id},
            )
```

`ValidationError` already exists or follows the existing exceptions pattern in `app.exceptions`; verify before implementing.

### Modified: `backend/app/api/routes/auth.py`

Append after the existing PAT routes:

```python
from fastapi import HTTPException, status


class ChangePasswordRequest(NFCModel):
    current_password: str
    new_password: str


@router.post("/auth/change-password", summary="Change own password")
async def change_password_route(
    req: ChangePasswordRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    from app.services.auth_service import change_password, BadPasswordChange
    try:
        await change_password(user.user_id, req.current_password, req.new_password)
    except BadPasswordChange as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return {"ok": True}
```

`AuthenticationError` → 401 and `NotFoundError` → 404 are handled by the existing global exception handlers in `main.py`. The new `BadPasswordChange` is mapped explicitly to 400 in the route (the global `ValidationError` handler returns 422, which is wrong for these user-correctable cases — see the BadPasswordChange docstring).

### Modified: `backend/app/api/routes/access.py`

Append the admin reset endpoint next to the existing `/admin/*` routes (around line 139-150):

```python
@router.post("/admin/users/{user_id}/reset-password", summary="[admin] Reset a user's password to a generated temp")
async def admin_reset_user_password(
    user_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    from app.services.password_service import reset_password
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT username FROM users WHERE id = $1", uuid.UUID(user_id))
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    temp, username = await reset_password(
        username=row["username"],
        actor_id=user.user_id,
        method="admin_ui",
    )
    return {"temporary_password": temp, "username": username}
```

(Adjust to existing helper functions / utility imports in `access.py`; admin gating already follows this pattern in neighbouring routes.)

### New file: `backend/app/cli.py`

```python
"""AKB management CLI.

Invoke via:
    docker compose exec backend uv run python -m app.cli <subcommand> [args]
or, on a server with the backend installed:
    python -m app.cli <subcommand> [args]

Subcommands:
    reset-password <username>   Generate a temp password for the given user.
                                 Prints the temp password to stdout. Caller
                                 must share it with the user out-of-band.
"""
from __future__ import annotations

import asyncio
import sys


async def _reset_password(username: str) -> int:
    from app.services.password_service import reset_password
    from app.exceptions import NotFoundError

    try:
        temp, uname = await reset_password(
            username=username, actor_id=None, method="cli",
        )
    except NotFoundError:
        print(f"User not found: {username}", file=sys.stderr)
        return 1
    print(f"Temporary password for {uname}: {temp}")
    print("Share this with the user out-of-band. It cannot be retrieved again.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("Usage: python -m app.cli <subcommand> [args]", file=sys.stderr)
        print("Subcommands: reset-password <username>", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "reset-password":
        if len(argv) != 2:
            print("Usage: python -m app.cli reset-password <username>", file=sys.stderr)
            return 2
        return asyncio.run(_reset_password(argv[1]))
    print(f"Unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

### New file: `backend/app/__main__.py`

```python
"""Allow `python -m app reset-password <user>` as a shorthand for `python -m app.cli ...`."""
from app.cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
```

Both `python -m app.cli reset-password <user>` and `python -m app reset-password <user>` should work after this. The two-file split exists because Python's `-m app` looks for `app/__main__.py`, but `-m app.cli` looks for `app/cli.py` — providing both makes the CLI discoverable either way.

## Frontend

### New page: `frontend/src/pages/auth-forgot.tsx`

Pure static informational page:

```tsx
<div className="...">
  <h1>Forgot your password?</h1>
  <p>
    Contact your administrator to reset your password. They will provide you
    with a temporary password you can use to log in. Once logged in, change
    it from <strong>Account Settings</strong>.
  </p>
  <Link to="/auth">Back to login</Link>
</div>
```

### Modified: `frontend/src/pages/auth.tsx`

Add a "Forgot password?" link below the login form's submit button. Hidden in register-tab. Route target: `/auth/forgot`.

### New page: `frontend/src/pages/account-settings.tsx`

Three-field form (current / new / confirm) calling `POST /auth/change-password`. Validates locally that `new === confirm` and `new.length >= 8` before sending. Surfaces 401 / 400 errors inline. On success: clear form + success toast "Password changed."

### New component: `frontend/src/components/admin-reset-password-dialog.tsx`

Modal opened from the admin users page row action. Two states:

- **Confirm**: "Generate a temporary password for `<username>`? They will be able to log in with it immediately." [Cancel] [Generate]
- **Result**: Large mono display of the temp password + copy button. Warning: "Share this with the user out-of-band (Slack, etc.). It cannot be retrieved again." [Done]

### Modified: `frontend/src/pages/settings.tsx` (admin tab)

The admin-user table lives inside `settings.tsx`'s `"admin"` tab (rendered only when `user.is_admin`). It already uses `adminListUsers` / `adminDeleteUser` and renders one row per user with a delete action. Add a second row action — "Reset password" button (key icon) — that opens `AdminResetPasswordDialog`. No separate admin-users page exists or should be created.

### Modified: `frontend/src/lib/api.ts`

```typescript
export const changePassword = (current_password: string, new_password: string) =>
  api<{ ok: true }>("/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password, new_password }),
  });

export const adminResetPassword = (userId: string) =>
  api<{ temporary_password: string; username: string }>(
    `/admin/users/${userId}/reset-password`,
    { method: "POST" },
  );
```

### Routing

Add to the router config:
- `/auth/forgot` → `<AuthForgotPage />`
- `/settings/account` → `<AccountSettingsPage />` (or wire under an existing settings tree)

## Data flow

### Scenario A: User forgets password, admin available

1. User clicks "Forgot password?" on `/auth` → `/auth/forgot` displays static guidance.
2. User contacts admin out-of-band (Slack, etc.).
3. Admin opens `/admin/users` → clicks "Reset password" on the user's row.
4. `AdminResetPasswordDialog` confirms → calls `adminResetPassword(user_id)` → backend bcrypt-replaces `password_hash`, emits `auth.password_reset` event with `method="admin_ui"`, `actor_id=admin.user_id`.
5. Admin sees temp password in modal → copies → sends to user out-of-band.
6. User pastes temp password into the login form → backend `login()` succeeds (it's just a normal bcrypt-verified login now).
7. User navigates to `/settings/account` → enters current=temp, new=desired → backend `change_password()` updates hash, emits `auth.password_changed` event.

### Scenario B: Admin themselves forgets password (lockout)

1. Operator with container shell access runs:
   ```
   docker compose exec backend uv run python -m app.cli reset-password admin-username
   ```
2. CLI calls `password_service.reset_password(username, actor_id=None, method="cli")`.
3. Same DB update + audit event (with `actor_id=null`, `method="cli"`).
4. CLI prints temp password to stdout.
5. Admin uses temp password to log in, then changes via `/settings/account`.

### Scenario C: User logged in, wants to rotate password

1. User → `/settings/account` → change-password form with current/new/confirm.
2. `POST /auth/change-password` → `auth_service.change_password()` verifies current → updates hash → emits `auth.password_changed`.
3. Success toast.

No admin involvement, no CLI, no temp password.

## Error handling

| Scenario | Backend | Frontend |
|---|---|---|
| Admin resets non-existent user | 404 | Toast "User not found" |
| Non-admin calls reset endpoint | 403 | Toast "Admin only" (button should not be visible to non-admins anyway) |
| User calls change-password with wrong current | 401 | Inline error "Current password is incorrect" |
| User calls change-password with new <8 chars | 400 | Inline error "Password must be at least 8 characters" |
| User calls change-password with new == current | 400 | Inline error "New password must differ from current" |
| CLI: user not found | stderr "User not found: X", exit 1 | (n/a) |
| CLI: missing argument | stderr "Usage: ...", exit 2 | (n/a) |
| DB unreachable during reset | 500, no event emitted (txn rolled back) | Toast generic error, admin re-tries |

## Testing

### Backend

**Unit** — `backend/tests/test_password_service.py` (new):
- `test_generate_temp_password_format`: length, dashes, charset.
- `test_generate_temp_password_uniqueness`: 100 invocations produce 100 distinct strings.
- `test_reset_password_updates_hash_and_emits_event`: real PG fixture (matches `test_collection_service.py` pattern); after reset, login with temp password succeeds, login with old password fails, `events` table has `auth.password_reset` row with correct payload.
- `test_reset_password_user_not_found`: raises `NotFoundError`.

**Unit** — extend `backend/tests/test_auth_service.py` (new or extend existing):
- `test_change_password_success`
- `test_change_password_wrong_current_raises`
- `test_change_password_too_short_raises`
- `test_change_password_same_as_current_raises`
- `test_change_password_emits_event`

**E2E** — `backend/tests/test_auth_password_e2e.sh` (new):
- register → login → POST /auth/change-password → re-login with new pw → 200.
- wrong-current change-password → 401.
- register admin + register user → admin login → POST /admin/users/{id}/reset-password → response has `temporary_password` → that pw logs the user in → user changes own.
- non-admin calls /admin/users/{id}/reset-password → 403.
- /admin/users/<random-uuid>/reset-password → 404.
- CLI smoke: `docker compose exec backend uv run python -m app.cli reset-password <user>` exit 0, stdout contains `Temporary password for`, the printed pw logs the user in via the REST API.

### Frontend

**Vitest**:
- `auth-forgot.test.tsx`: renders the static page, link back to /auth exists.
- `account-settings.test.tsx`: form validates new===confirm, new>=8, mock API call on submit, success toast on 200, inline error on 401.
- `admin-reset-password-dialog.test.tsx`: confirm step renders username, generate button calls mock API, result step shows temp password and copy button, dialog dismisses on Done.

## Files touched (summary)

**Backend — new**:
- `backend/app/services/password_service.py`
- `backend/app/cli.py`
- `backend/app/__main__.py`
- `backend/tests/test_password_service.py`
- `backend/tests/test_auth_password_e2e.sh`

**Backend — modified**:
- `backend/app/services/auth_service.py` — `change_password` function
- `backend/app/api/routes/auth.py` — `POST /auth/change-password` route + request model
- `backend/app/api/routes/access.py` — `POST /admin/users/{user_id}/reset-password` route
- `backend/tests/test_auth_service.py` — change_password unit tests (extend or new file)

**Frontend — new**:
- `frontend/src/pages/auth-forgot.tsx`
- `frontend/src/pages/account-settings.tsx`
- `frontend/src/components/admin-reset-password-dialog.tsx`
- `frontend/src/pages/__tests__/auth-forgot.test.tsx`
- `frontend/src/pages/__tests__/account-settings.test.tsx`
- `frontend/src/components/__tests__/admin-reset-password-dialog.test.tsx`

**Frontend — modified**:
- `frontend/src/pages/auth.tsx` — "Forgot password?" link
- `frontend/src/lib/api.ts` — `changePassword`, `adminResetPassword`
- `frontend/src/pages/settings.tsx (admin tab)` (or equivalent admin users page) — row action button
- frontend router config — `/auth/forgot`, `/settings/account` routes
