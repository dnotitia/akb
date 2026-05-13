# Password recovery Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add admin-mediated password reset (admin UI + CLI fallback) plus a self-serve change-password endpoint for logged-in users. No SMTP, no new tables, no reset-token table — admin generates a 12-char temp password that replaces `users.password_hash` directly; user logs in with it and changes it themselves.

**Architecture:** A new `password_service` module holds the shared "generate temp + bcrypt + audit" primitive used by both the admin REST endpoint and the CLI. A separate `change_password` function on `auth_service` handles the logged-in self-rotation path. Both emit events into the existing `events` outbox for audit. Frontend gets three small additions: "Forgot password?" link → static info page, change-password form on Account Settings, and a "Reset password" row action in the existing admin tab inside `settings.tsx`.

**Tech Stack:** Python 3.11 / FastAPI / asyncpg / bcrypt / PyJWT; React 19 + TypeScript + Vite + Vitest.

**Spec:** `docs/superpowers/specs/2026-05-13-password-recovery-design.md`

---

## File Map

**Backend — new**:
- `backend/app/services/password_service.py` — `generate_temp_password()` + `reset_password(username, actor_id, method)`
- `backend/app/cli.py` — `python -m app.cli reset-password <username>` entry point
- `backend/app/__main__.py` — `python -m app …` alias to `app.cli.main`
- `backend/tests/test_password_service.py` — unit tests for the service (real PG)
- `backend/tests/test_auth_change_password.py` — unit tests for `auth_service.change_password` (real PG)
- `backend/tests/test_auth_password_e2e.sh` — full HTTP + CLI integration

**Backend — modified**:
- `backend/app/services/auth_service.py` — `BadPasswordChange` exception + `change_password()` function
- `backend/app/api/routes/auth.py` — `POST /auth/change-password` route + `ChangePasswordRequest` body model
- `backend/app/api/routes/access.py` — `POST /admin/users/{user_id}/reset-password` route

**Frontend — new**:
- `frontend/src/pages/auth-forgot.tsx` — static "contact your admin" page
- `frontend/src/pages/account-settings.tsx` — change-password form (or extend an existing settings tab — see Task 8 decision)
- `frontend/src/components/admin-reset-password-dialog.tsx` — modal that confirms + shows the temp password once
- Vitest companions for all three

**Frontend — modified**:
- `frontend/src/pages/auth.tsx` — "Forgot password?" link under the login submit button
- `frontend/src/lib/api.ts` — `changePassword`, `adminResetPassword`
- `frontend/src/pages/settings.tsx` (admin tab block) — wire a "Reset password" row action that opens the dialog
- Router config — `/auth/forgot`, `/settings/account` (or wire change-password under existing `settings.tsx` profile tab — Task 8 decides)

---

## Task 1 — `password_service` module + unit tests

**Files:**
- Create: `backend/app/services/password_service.py`
- Create: `backend/tests/test_password_service.py`

**Context:** No app imports beyond `auth_service.hash_password`, `repositories.events_repo.emit_event`, and `db.postgres.get_pool`. Mirrors the lightweight-service pattern used by `collection_service.py`. Tests hit real Postgres via the existing `pool` fixture pattern from `test_collection_service.py` — the `conftest.py` shim makes `app.config` importable.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_password_service.py`:

```python
"""Unit tests for password_service."""
from __future__ import annotations

import os
import re
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN)
        await conn.close()
        return True
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    # mark/skip when DB unreachable (mirrors test_collection_service.py)
    pass


@pytest.fixture
async def pool():
    if not await _can_connect():
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")
    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
    # Schema is already present in the live DB this test targets
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
def patch_get_pool(monkeypatch, pool):
    """Make password_service.get_pool() return our test pool."""
    from app.services import password_service as ps
    async def _fake() -> asyncpg.Pool:
        return pool
    monkeypatch.setattr(ps, "get_pool", _fake)


@pytest.fixture
async def user(pool):
    """Insert a throwaway user and yield (id, username)."""
    from app.services.auth_service import hash_password
    uid = uuid.uuid4()
    uname = f"pwsvc-{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash) "
            "VALUES ($1, $2, $3, $4)",
            uid, uname, f"{uname}@t.dev", hash_password("orig-password"),
        )
    yield uid, uname
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        await conn.execute("DELETE FROM events WHERE ref_id = $1", str(uid))


def test_generate_temp_password_format():
    from app.services.password_service import generate_temp_password
    pw = generate_temp_password()
    # 12 base64url chars grouped 4-4-4 with dashes = 14 chars total
    assert re.fullmatch(r"[A-Za-z0-9_-]{4}-[A-Za-z0-9_-]{4}-[A-Za-z0-9_-]{4}", pw), pw


def test_generate_temp_password_uniqueness():
    from app.services.password_service import generate_temp_password
    seen = {generate_temp_password() for _ in range(100)}
    assert len(seen) == 100


async def test_reset_password_updates_hash_and_emits_event(pool, user):
    from app.services.auth_service import verify_password
    from app.services.password_service import reset_password
    uid, uname = user

    temp, returned_username = await reset_password(
        username=uname, actor_id="admin-uuid", method="admin_ui",
    )
    assert returned_username == uname
    assert re.fullmatch(r"[A-Za-z0-9_-]{4}-[A-Za-z0-9_-]{4}-[A-Za-z0-9_-]{4}", temp)

    # The new hash must verify against the returned temp password and reject the old.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_hash FROM users WHERE id = $1", uid,
        )
    assert verify_password(temp, row["password_hash"])
    assert not verify_password("orig-password", row["password_hash"])

    # Audit row landed with the right shape.
    async with pool.acquire() as conn:
        ev = await conn.fetchrow(
            "SELECT kind, ref_type, ref_id, actor_id, payload::text AS payload "
            "FROM events WHERE ref_id = $1 ORDER BY id DESC LIMIT 1",
            str(uid),
        )
    assert ev is not None
    assert ev["kind"] == "auth.password_reset"
    assert ev["ref_type"] == "user"
    assert ev["actor_id"] == "admin-uuid"
    import json
    payload = json.loads(ev["payload"])
    assert payload["user_id"] == str(uid)
    assert payload["username"] == uname
    assert payload["method"] == "admin_ui"


async def test_reset_password_cli_method_records_null_actor(pool, user):
    from app.services.password_service import reset_password
    uid, uname = user
    await reset_password(username=uname, actor_id=None, method="cli")
    async with pool.acquire() as conn:
        ev = await conn.fetchrow(
            "SELECT actor_id, payload::text AS payload FROM events "
            "WHERE ref_id = $1 ORDER BY id DESC LIMIT 1",
            str(uid),
        )
    assert ev["actor_id"] is None
    import json
    assert json.loads(ev["payload"])["method"] == "cli"


async def test_reset_password_user_not_found(pool):
    from app.exceptions import NotFoundError
    from app.services.password_service import reset_password
    with pytest.raises(NotFoundError):
        await reset_password(
            username=f"does-not-exist-{uuid.uuid4().hex[:6]}",
            actor_id=None, method="cli",
        )
```

- [ ] **Step 2: Run, see fail**

```bash
cd /Users/kwoo2/Desktop/storage/akb/backend && \
  AKB_TEST_DSN="postgresql://akb:akb@localhost:15432/akb" \
  uv run --extra dev pytest tests/test_password_service.py -v
```
Expected: `ModuleNotFoundError: No module named 'app.services.password_service'`.

- [ ] **Step 3: Implement the service**

Create `backend/app/services/password_service.py`:

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
from typing import Literal

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories.events_repo import emit_event
from app.services.auth_service import hash_password


def generate_temp_password() -> str:
    """12-char URL-safe random password with dash grouping for readability.

    secrets.token_urlsafe(9) → 12 base64url chars → ~50 bits entropy.
    Dash-grouped 4-4-4 so the admin can read it aloud or copy-paste
    without breaking on selection.
    """
    raw = secrets.token_urlsafe(9)[:12]
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

- [ ] **Step 4: Run, see pass**

```bash
cd backend && AKB_TEST_DSN="postgresql://akb:akb@localhost:15432/akb" \
  uv run --extra dev pytest tests/test_password_service.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/password_service.py backend/tests/test_password_service.py
git commit -m "feat(password-service): generate_temp_password + reset_password with audit"
```

---

## Task 2 — `change_password` + `BadPasswordChange` on `auth_service`

**Files:**
- Modify: `backend/app/services/auth_service.py`
- Create: `backend/tests/test_auth_change_password.py`

**Context:** Both the new exception class and the `change_password` function live in `auth_service.py` alongside the existing `login` / `register` because they share `hash_password` / `verify_password` and the same shape of DB access. `BadPasswordChange` is module-local — does not go into `app.exceptions` because it's only mapped to HTTP 400 by the auth route, not by a global handler.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_auth_change_password.py`:

```python
"""Unit tests for auth_service.change_password."""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")


async def _can_connect() -> bool:
    try:
        conn = await asyncpg.connect(_DSN)
        await conn.close()
        return True
    except Exception:
        return False


@pytest.fixture
async def pool():
    if not await _can_connect():
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")
    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
def patch_get_pool(monkeypatch, pool):
    from app.services import auth_service as auth
    async def _fake() -> asyncpg.Pool:
        return pool
    monkeypatch.setattr(auth, "get_pool", _fake)


@pytest.fixture
async def user(pool):
    from app.services.auth_service import hash_password
    uid = uuid.uuid4()
    uname = f"chgpw-{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, username, email, password_hash) "
            "VALUES ($1, $2, $3, $4)",
            uid, uname, f"{uname}@t.dev", hash_password("orig-12345"),
        )
    yield uid, uname
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
        await conn.execute("DELETE FROM events WHERE ref_id = $1", str(uid))


async def test_change_password_success(pool, user):
    from app.services.auth_service import change_password, verify_password
    uid, _ = user
    await change_password(str(uid), "orig-12345", "new-secret-67")
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT password_hash FROM users WHERE id = $1", uid)
    assert verify_password("new-secret-67", row["password_hash"])
    assert not verify_password("orig-12345", row["password_hash"])


async def test_change_password_wrong_current_raises(user):
    from app.exceptions import AuthenticationError
    from app.services.auth_service import change_password
    uid, _ = user
    with pytest.raises(AuthenticationError):
        await change_password(str(uid), "WRONG-current", "new-secret-67")


async def test_change_password_too_short_raises(user):
    from app.services.auth_service import change_password, BadPasswordChange
    uid, _ = user
    with pytest.raises(BadPasswordChange):
        await change_password(str(uid), "orig-12345", "short")


async def test_change_password_same_as_current_raises(user):
    from app.services.auth_service import change_password, BadPasswordChange
    uid, _ = user
    with pytest.raises(BadPasswordChange):
        await change_password(str(uid), "orig-12345", "orig-12345")


async def test_change_password_user_not_found_raises(pool):
    from app.exceptions import NotFoundError
    from app.services.auth_service import change_password
    with pytest.raises(NotFoundError):
        await change_password(str(uuid.uuid4()), "any", "new-secret-67")


async def test_change_password_emits_event(pool, user):
    from app.services.auth_service import change_password
    uid, _ = user
    await change_password(str(uid), "orig-12345", "new-secret-67")
    async with pool.acquire() as conn:
        ev = await conn.fetchrow(
            "SELECT kind, ref_type, ref_id, actor_id "
            "FROM events WHERE ref_id = $1 ORDER BY id DESC LIMIT 1",
            str(uid),
        )
    assert ev is not None
    assert ev["kind"] == "auth.password_changed"
    assert ev["ref_type"] == "user"
    assert ev["actor_id"] == str(uid)
```

- [ ] **Step 2: Run, see fail**

```bash
cd backend && AKB_TEST_DSN=... uv run --extra dev pytest tests/test_auth_change_password.py -v
```
Expected: `ImportError: cannot import name 'change_password' from 'app.services.auth_service'`.

- [ ] **Step 3: Add the exception + function**

Append to `backend/app/services/auth_service.py` (after `login`, before `# ── PAT operations ──`):

```python
import uuid
# (uuid is already imported at top of file — skip if it is)

from app.exceptions import AuthenticationError, NotFoundError
# (these may already be imported — skip duplicates)

from app.repositories.events_repo import emit_event
# (not yet imported here — add this one if missing)


class BadPasswordChange(Exception):
    """Raised when change_password is called with input the user can correct.

    Distinct from app.exceptions.ValidationError (which the global handler
    maps to 422 — a pydantic-shaped validation failure). These cases are
    HTTP 400: the request reached the service, the user just chose a bad
    new password. The route maps this to HTTPException(400) so the
    frontend can render an inline form error.
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

Before placing the imports, inspect the current top of `auth_service.py` to see which are already imported. Don't duplicate.

- [ ] **Step 4: Run, see pass**

```bash
cd backend && AKB_TEST_DSN=... uv run --extra dev pytest tests/test_auth_change_password.py -v
```
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/auth_service.py backend/tests/test_auth_change_password.py
git commit -m "feat(auth): change_password + BadPasswordChange exception"
```

---

## Task 3 — REST `POST /auth/change-password`

**Files:**
- Modify: `backend/app/api/routes/auth.py`

**Context:** This route is logged-in-user-only. `BadPasswordChange` is caught locally and mapped to 400; `AuthenticationError` (wrong current pw) and `NotFoundError` flow through the global handlers as 401 / 404. Body model uses `NFCModel` like the existing register/login bodies.

- [ ] **Step 1: Read the current shape**

```bash
sed -n '1,40p' backend/app/api/routes/auth.py
```

Confirm the existing `RegisterRequest` / `LoginRequest` / `CreatePATRequest` bodies all extend `NFCModel`. The `from fastapi import` line currently shows `APIRouter, Depends` — you'll need to add `HTTPException` and `status`.

- [ ] **Step 2: Update the imports**

In `backend/app/api/routes/auth.py`, change line 3:

```python
from fastapi import APIRouter, Depends, HTTPException, status
```

- [ ] **Step 3: Add the body model**

Near the other `*Request` classes (after `CreatePATRequest`):

```python
class ChangePasswordRequest(NFCModel):
    current_password: str
    new_password: str
```

- [ ] **Step 4: Add the route**

Append at the bottom of `auth.py`:

```python
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

- [ ] **Step 5: Smoke-check routes register**

```bash
cd backend && uv run --extra dev python -c \
  "from app.main import app; print([r.path for r in app.routes if 'change-password' in r.path])"
```
Expected: `['/api/v1/auth/change-password']`.

If the import fails locally due to `/data/vaults` missing (known dev issue), skip and rely on the container smoke check in Task 9.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/routes/auth.py
git commit -m "feat(api): POST /auth/change-password"
```

---

## Task 4 — REST `POST /admin/users/{user_id}/reset-password`

**Files:**
- Modify: `backend/app/api/routes/access.py`

**Context:** Existing admin routes in `access.py` follow the pattern `if not user.is_admin: raise ...`. The new route resolves `user_id` to a username (404 if missing) and delegates to `password_service.reset_password` with `method="admin_ui"` and `actor_id` = the admin's user_id.

- [ ] **Step 1: Inspect existing admin routes for style**

```bash
sed -n '139,160p' backend/app/api/routes/access.py
```

Note the pattern used by `admin_list_users` / `admin_delete_user` — that's what to mirror.

- [ ] **Step 2: Add the route**

Append at the bottom of `backend/app/api/routes/access.py` (or next to the existing `/admin/users/{user_id}` DELETE):

```python
@router.post(
    "/admin/users/{user_id}/reset-password",
    summary="[admin] Reset a user's password to a generated temp",
)
async def admin_reset_user_password(
    user_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")

    import uuid as _uuid
    from app.db.postgres import get_pool
    from app.services.password_service import reset_password

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username FROM users WHERE id = $1", _uuid.UUID(user_id),
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    temp, username = await reset_password(
        username=row["username"],
        actor_id=user.user_id,
        method="admin_ui",
    )
    return {"temporary_password": temp, "username": username}
```

If `HTTPException` and `status` are not yet imported at the top of `access.py`, **add them** to the existing `from fastapi import …` line. Inspect line 1-10 first.

- [ ] **Step 3: Smoke-check route registers**

```bash
cd backend && uv run --extra dev python -c \
  "from app.main import app; \
   print([r.path for r in app.routes if 'reset-password' in r.path])"
```
Expected: `['/api/v1/admin/users/{user_id}/reset-password']`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/routes/access.py
git commit -m "feat(api): POST /admin/users/{user_id}/reset-password"
```

---

## Task 5 — CLI: `python -m app.cli reset-password <user>`

**Files:**
- Create: `backend/app/cli.py`
- Create: `backend/app/__main__.py`

**Context:** Both `python -m app.cli reset-password <user>` and `python -m app reset-password <user>` should work. The two-file split exists because `python -m app` looks for `app/__main__.py`, while `-m app.cli` looks for `app/cli.py`.

- [ ] **Step 1: Create the CLI module**

Create `backend/app/cli.py`:

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
    from app.exceptions import NotFoundError
    from app.services.password_service import reset_password

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

- [ ] **Step 2: Create the module-level entry point**

Create `backend/app/__main__.py`:

```python
"""Allow `python -m app <subcommand>` as a shorthand for `python -m app.cli <subcommand>`."""
import sys

from app.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Smoke-check both entry points**

Against the running backend container (which already has the password_service):

```bash
docker compose exec backend python -m app.cli 2>&1 | head -5
docker compose exec backend python -m app 2>&1 | head -5
```

(The backend container installs deps via `pip install --no-cache-dir .` and does **not** ship `uv`. The system `python` already has every backend dep on PATH.)

Both should print the usage line and exit 2.

- [ ] **Step 4: Rebuild the container so the CLI works in production**

```bash
cd /Users/kwoo2/Desktop/storage/akb && docker compose up -d --build backend
until curl -sf http://localhost:8000/livez >/dev/null 2>&1; do sleep 2; done
```

- [ ] **Step 5: Live smoke against a real user**

```bash
# Bootstrap a test user against the live backend
U="cli-smoke-$(date +%s)"
curl -sk -X POST http://localhost:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$U\",\"email\":\"$U@t.dev\",\"password\":\"orig-12345\"}" >/dev/null

# Reset via CLI
TEMP=$(docker compose exec backend python -m app.cli reset-password $U \
  | awk -F': ' '/^Temporary password/{print $2}')
echo "Temp = $TEMP"

# Login with the new temp password
JWT=$(curl -sk -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$U\",\"password\":\"$TEMP\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))')
[ -n "$JWT" ] && echo "CLI reset → login OK" || echo "FAIL: no JWT after CLI reset"
```

Expected: `CLI reset → login OK`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/cli.py backend/app/__main__.py
git commit -m "feat(cli): python -m app.cli reset-password for admin lockout"
```

---

## Task 6 — Backend E2E suite

**Files:**
- Create: `backend/tests/test_auth_password_e2e.sh`

**Context:** Follows the same self-contained shell pattern as `test_vault_templates_e2e.sh` / `test_collection_lifecycle_e2e.sh`. Each test bootstraps its own users (the admin must be granted via `is_admin=true` — the only way to do that in a fresh DB is via direct UPDATE since there's no admin-promotion endpoint; see Step 1 below for the helper).

- [ ] **Step 1: Create the e2e file**

Create `backend/tests/test_auth_password_e2e.sh`:

```bash
#!/usr/bin/env bash
# E2E for password recovery:
#   1. Self-service /auth/change-password (correct/incorrect current, too short, same).
#   2. /admin/users/{id}/reset-password (admin happy path; 403 from non-admin; 404 on bogus id).
#   3. CLI reset-password parity (separate, simpler — already smoked in Task 5).
set -uo pipefail

BASE_URL="${AKB_URL:-http://localhost:8000}"
PASS=0
FAIL=0
ERRORS=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); ERRORS+=("$1: $2"); echo "  ✗ $1 — $2"; }

echo "╔══════════════════════════════════════════╗"
echo "║   Password Recovery E2E                  ║"
echo "║   Target: $BASE_URL"
echo "╚══════════════════════════════════════════╝"

# Helper: promote a user to admin via the backend container's Python.
# `docker compose exec` runs inside the backend image which pip-installed
# all backend deps (asyncpg is on PATH). NO uv — container does not ship it.
# stderr stays visible so failures surface in the test log instead of being
# masked as a downstream "non-admin → 403" misattribution.
promote_admin() {
  local uname="$1"
  docker compose exec -T backend python -c "
import asyncio, asyncpg
async def go():
    dsn = 'postgresql://akb:akb@postgres:5432/akb'
    conn = await asyncpg.connect(dsn)
    await conn.execute('UPDATE users SET is_admin = TRUE WHERE username = \$1', '$uname')
    await conn.close()
asyncio.run(go())
" >/dev/null
}

# Helper: register + login → echo JWT
register_and_login() {
  local uname="$1" pw="$2"
  curl -sk -X POST "$BASE_URL/api/v1/auth/register" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$uname\",\"email\":\"$uname@t.dev\",\"password\":\"$pw\"}" >/dev/null
  curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$uname\",\"password\":\"$pw\"}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'
}

# Helper: login → echo JWT (no register)
login_only() {
  local uname="$1" pw="$2"
  curl -sk -X POST "$BASE_URL/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$uname\",\"password\":\"$pw\"}" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))'
}

# Helper: user_id from JWT/me
me_user_id() {
  curl -sk "$BASE_URL/api/v1/auth/me" -H "Authorization: Bearer $1" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["user_id"])'
}

TS="$(date +%s)"
USER="pw-e2e-$TS"
USER_PW_OLD="orig-secret-12"
USER_PW_NEW="brand-new-secret-99"

# ── 1. Self-service change-password ────────────────────────
echo ""
echo "▸ 1. /auth/change-password"

JWT=$(register_and_login "$USER" "$USER_PW_OLD")
[ -n "$JWT" ] && pass "bootstrap user + login" || { fail "bootstrap" "no JWT"; exit 1; }

# Happy path
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/change-password" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"$USER_PW_OLD\",\"new_password\":\"$USER_PW_NEW\"}")
[ "$HTTP" = "200" ] && pass "happy path → 200" || fail "happy path" "got $HTTP"

# Old password rejected
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"$USER_PW_OLD\"}")
[ "$HTTP" = "401" ] && pass "old pw rejected" || fail "old pw" "got $HTTP"

# New password accepted
JWT=$(login_only "$USER" "$USER_PW_NEW")
[ -n "$JWT" ] && pass "new pw accepted" || fail "new pw login" "no JWT"

# Wrong current → 401
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/change-password" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"WRONG\",\"new_password\":\"another-secret-77\"}")
[ "$HTTP" = "401" ] && pass "wrong current → 401" || fail "wrong current" "got $HTTP"

# New too short → 400
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/change-password" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"$USER_PW_NEW\",\"new_password\":\"short\"}")
[ "$HTTP" = "400" ] && pass "too short → 400" || fail "too short" "got $HTTP"

# New == current → 400
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/change-password" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"$USER_PW_NEW\",\"new_password\":\"$USER_PW_NEW\"}")
[ "$HTTP" = "400" ] && pass "same as current → 400" || fail "same as current" "got $HTTP"

# ── 2. Admin reset ─────────────────────────────────────────
echo ""
echo "▸ 2. /admin/users/{id}/reset-password"

ADMIN="pw-e2e-admin-$TS"
ADMIN_PW="admin-secret-12"
register_and_login "$ADMIN" "$ADMIN_PW" >/dev/null
promote_admin "$ADMIN"
ADMIN_JWT=$(login_only "$ADMIN" "$ADMIN_PW")
[ -n "$ADMIN_JWT" ] && pass "admin bootstrap + login" || { fail "admin" "no JWT"; exit 1; }

# Need the user's UUID — me endpoint for the regular user
USER_JWT=$(login_only "$USER" "$USER_PW_NEW")
USER_ID=$(me_user_id "$USER_JWT")

# Admin reset
R=$(curl -sk -X POST "$BASE_URL/api/v1/admin/users/$USER_ID/reset-password" \
  -H "Authorization: Bearer $ADMIN_JWT")
TEMP=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("temporary_password",""))')
[ -n "$TEMP" ] && pass "admin reset returns temp pw" || fail "admin reset" "$R"

# Old pw no longer works
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER\",\"password\":\"$USER_PW_NEW\"}")
[ "$HTTP" = "401" ] && pass "user-old-pw rejected after admin reset" \
  || fail "old user pw" "got $HTTP"

# Temp pw works
NEW_USER_JWT=$(login_only "$USER" "$TEMP")
[ -n "$NEW_USER_JWT" ] && pass "user logs in with temp pw" \
  || fail "temp pw login" "empty"

# Non-admin calls admin endpoint → 403
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/admin/users/$USER_ID/reset-password" \
  -H "Authorization: Bearer $NEW_USER_JWT")
[ "$HTTP" = "403" ] && pass "non-admin → 403" || fail "non-admin" "got $HTTP"

# Bogus user_id → 404
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" -X POST \
  "$BASE_URL/api/v1/admin/users/00000000-0000-0000-0000-000000000000/reset-password" \
  -H "Authorization: Bearer $ADMIN_JWT")
[ "$HTTP" = "404" ] && pass "bogus user → 404" || fail "bogus user" "got $HTTP"

# ── Summary ────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════"
if [ $FAIL -eq 0 ]; then
  echo "✓ All $PASS tests passed"
  exit 0
else
  echo "✗ $FAIL failures (of $((PASS+FAIL)) total)"
  printf '  - %s\n' "${ERRORS[@]}"
  exit 1
fi
```

- [ ] **Step 2: Make executable + syntax check**

```bash
chmod +x backend/tests/test_auth_password_e2e.sh
bash -n backend/tests/test_auth_password_e2e.sh && echo "SYNTAX OK"
```

- [ ] **Step 3: Run live**

```bash
AKB_URL=http://localhost:8000 bash backend/tests/test_auth_password_e2e.sh
```
Expected: all 11 assertions pass.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_auth_password_e2e.sh
git commit -m "test(e2e): password recovery (change + admin reset)"
```

---

## Task 7 — Frontend `api.ts` helpers

**Files:**
- Modify: `frontend/src/lib/api.ts`

**Context:** Add two thin helpers. The existing `api()` wrapper handles errors / JSON parsing.

- [ ] **Step 1: Locate**

```bash
grep -n "adminListUsers\|adminDeleteUser" frontend/src/lib/api.ts
```

Confirm those exist; put the new helpers next to them.

- [ ] **Step 2: Add the helpers**

In `frontend/src/lib/api.ts`, near `adminListUsers`:

```typescript
export const changePassword = (current_password: string, new_password: string) =>
  api<{ ok: true }>("/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password, new_password }),
  });

export const adminResetPassword = (userId: string) =>
  api<{ temporary_password: string; username: string }>(
    `/admin/users/${encodeURIComponent(userId)}/reset-password`,
    { method: "POST" },
  );
```

- [ ] **Step 3: Typecheck**

```bash
cd frontend && pnpm tsc --noEmit
```
Expected: clean.

- [ ] **Step 4: Run the test suite (no regressions)**

```bash
cd frontend && pnpm vitest run
```
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(api-client): changePassword + adminResetPassword"
```

---

## Task 8 — Frontend pages + component (forgot, change-password, admin reset dialog)

**Files:**
- Create: `frontend/src/pages/auth-forgot.tsx`
- Create: `frontend/src/components/admin-reset-password-dialog.tsx`
- Modify: `frontend/src/pages/auth.tsx` (forgot link below submit)
- Modify: `frontend/src/pages/settings.tsx` (profile tab gets change-password form; admin tab gets reset-password row action)
- Create: `frontend/src/pages/__tests__/auth-forgot.test.tsx`
- Create: `frontend/src/components/__tests__/admin-reset-password-dialog.test.tsx`

**Context for the "where does change-password live" decision:** The spec proposed `/settings/account` as a separate page. Inspection of `settings.tsx` shows it already has a **`"profile"` tab** that renders the logged-in user's profile info. Putting the change-password form there (as a section beneath the existing profile fields) avoids a new route and matches the existing tab layout. We do **not** create `account-settings.tsx`; we extend the existing profile tab instead.

- [ ] **Step 1: Static forgot page**

Create `frontend/src/pages/auth-forgot.tsx`:

```tsx
import { Link } from "react-router-dom";

export default function AuthForgotPage() {
  return (
    <div className="max-w-md mx-auto px-4 py-16 fade-up">
      <div className="coord-spark mb-2">§ FORGOT PASSWORD</div>
      <h1 className="text-2xl font-semibold tracking-tight text-foreground mb-4">
        Forgot your password?
      </h1>
      <p className="text-sm text-foreground-muted leading-relaxed mb-3">
        Contact your administrator to reset your password. They will provide
        you with a temporary password you can use to log in.
      </p>
      <p className="text-sm text-foreground-muted leading-relaxed mb-6">
        Once logged in, change it from <strong>Settings → Profile</strong>.
      </p>
      <Link
        to="/auth"
        className="coord hover:text-accent transition-colors"
      >
        ← BACK TO LOGIN
      </Link>
    </div>
  );
}
```

- [ ] **Step 2: Add "Forgot password?" link to auth.tsx**

Find the submit button block (`<button type="submit" ...>`) in `frontend/src/pages/auth.tsx` (around line 462) and the closing `</form>` after it. Add the link **immediately below** the submit button, and **only when `mode === "login"`** (don't show in register tab):

```tsx
{mode === "login" && (
  <div className="text-center pt-2">
    <Link
      to="/auth/forgot"
      className="coord hover:text-accent transition-colors"
    >
      Forgot password?
    </Link>
  </div>
)}
```

Add `import { Link } from "react-router-dom";` at the top if it isn't there.

- [ ] **Step 3: Add the route**

Locate the router config (likely `frontend/src/main.tsx` or `frontend/src/App.tsx` — grep for `Routes` to find it):

```bash
grep -rn "createBrowserRouter\|<Routes>" frontend/src/ | head -5
```

Add the route:

```tsx
import AuthForgotPage from "@/pages/auth-forgot";
// …
<Route path="/auth/forgot" element={<AuthForgotPage />} />
```

- [ ] **Step 4: Write the forgot-page test**

Create `frontend/src/pages/__tests__/auth-forgot.test.tsx`:

```typescript
import { render, screen, cleanup } from "@testing-library/react";
import { describe, it, expect, afterEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import AuthForgotPage from "../auth-forgot";

afterEach(cleanup);

describe("AuthForgotPage", () => {
  it("renders heading + admin-contact guidance + back-to-login link", () => {
    render(
      <MemoryRouter>
        <AuthForgotPage />
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: /forgot your password/i })).toBeInTheDocument();
    expect(screen.getByText(/contact your administrator/i)).toBeInTheDocument();
    const back = screen.getByRole("link", { name: /back to login/i });
    expect(back).toHaveAttribute("href", "/auth");
  });
});
```

- [ ] **Step 5: Run forgot tests**

```bash
cd frontend && pnpm vitest run pages/__tests__/auth-forgot
```
Expected: 1 passed.

- [ ] **Step 6: Admin reset password dialog component**

Create `frontend/src/components/admin-reset-password-dialog.tsx`:

```tsx
import { useEffect, useState } from "react";
import { AlertTriangle, Check, Copy, Key, Loader2 } from "lucide-react";
import { adminResetPassword } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface Props {
  userId: string;
  username: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function AdminResetPasswordDialog({ userId, username, open, onOpenChange }: Props) {
  const [working, setWorking] = useState(false);
  const [tempPassword, setTempPassword] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) {
      setTempPassword(null);
      setCopied(false);
      setError("");
      setWorking(false);
    }
  }, [open]);

  async function handleGenerate() {
    setError("");
    setWorking(true);
    try {
      const r = await adminResetPassword(userId);
      setTempPassword(r.temporary_password);
    } catch (e: any) {
      setError(e?.message || "Failed to reset password");
    } finally {
      setWorking(false);
    }
  }

  async function handleCopy() {
    if (!tempPassword) return;
    try {
      await navigator.clipboard.writeText(tempPassword);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {}
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !working && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Key className="h-4 w-4" aria-hidden /> Reset password
          </DialogTitle>
          <DialogDescription>
            Generate a temporary password for <strong>{username}</strong>.
          </DialogDescription>
        </DialogHeader>

        {tempPassword === null ? (
          <div className="space-y-3 text-sm">
            <p>
              The user's current password will be replaced immediately. They
              will be able to log in with the generated password and then change
              it from Settings.
            </p>
            {error && (
              <p role="alert" className="text-destructive text-xs font-mono">{error}</p>
            )}
          </div>
        ) : (
          <div className="space-y-3">
            <div className="border border-destructive/40 bg-destructive/10 p-3 flex items-start gap-2">
              <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0 text-destructive" aria-hidden />
              <div className="text-xs text-foreground">
                Share this with the user out-of-band (Slack, in person, etc.).
                It cannot be retrieved again.
              </div>
            </div>
            <div className="relative">
              <pre
                data-testid="temp-password"
                className="font-mono text-base bg-surface-muted border border-border p-3 select-all break-all"
              >
                {tempPassword}
              </pre>
              <button
                type="button"
                onClick={handleCopy}
                aria-label="Copy temporary password"
                className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-1 text-[11px] font-mono uppercase tracking-wider text-foreground-muted hover:text-accent border border-border bg-surface"
              >
                {copied ? <><Check className="h-3 w-3" aria-hidden /> COPIED</> : <><Copy className="h-3 w-3" aria-hidden /> COPY</>}
              </button>
            </div>
          </div>
        )}

        <DialogFooter>
          {tempPassword === null ? (
            <>
              <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={working}>
                Cancel
              </Button>
              <Button onClick={handleGenerate} disabled={working}>
                {working && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
                Generate
              </Button>
            </>
          ) : (
            <Button onClick={() => onOpenChange(false)}>Done</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 7: Write the dialog tests**

Create `frontend/src/components/__tests__/admin-reset-password-dialog.test.tsx`:

```typescript
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { AdminResetPasswordDialog } from "../admin-reset-password-dialog";
import * as api from "@/lib/api";

vi.mock("@/lib/api", () => ({ adminResetPassword: vi.fn() }));

afterEach(cleanup);

describe("AdminResetPasswordDialog", () => {
  beforeEach(() => vi.clearAllMocks());

  it("calls adminResetPassword on Generate and surfaces temp password", async () => {
    (api.adminResetPassword as any).mockResolvedValue({
      temporary_password: "Abcd-1234-EfGh",
      username: "alice",
    });
    render(
      <AdminResetPasswordDialog userId="u1" username="alice" open onOpenChange={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /generate/i }));
    const pre = await screen.findByTestId("temp-password");
    expect(pre.textContent).toBe("Abcd-1234-EfGh");
    expect(api.adminResetPassword).toHaveBeenCalledWith("u1");
  });

  it("renders error inline when adminResetPassword rejects", async () => {
    (api.adminResetPassword as any).mockRejectedValue(new Error("boom"));
    render(
      <AdminResetPasswordDialog userId="u1" username="alice" open onOpenChange={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /generate/i }));
    expect(await screen.findByText(/boom/i)).toBeInTheDocument();
  });

  it("Copy button writes temp password to clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText }, configurable: true, writable: true,
    });
    (api.adminResetPassword as any).mockResolvedValue({
      temporary_password: "Xyz1-Wow2-Yes3", username: "bob",
    });
    render(
      <AdminResetPasswordDialog userId="u2" username="bob" open onOpenChange={() => {}} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /generate/i }));
    await screen.findByTestId("temp-password");
    const btn = screen.getByRole("button", { name: /copy temporary password/i });
    btn.click();
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Xyz1-Wow2-Yes3"));
  });
});
```

- [ ] **Step 8: Run dialog tests**

```bash
cd frontend && pnpm vitest run components/__tests__/admin-reset-password-dialog
```
Expected: 3 passed.

- [ ] **Step 9: Wire dialog into settings admin tab**

In `frontend/src/pages/settings.tsx`, find the admin tab block (search for `value="admin"` `TabsContent`) and locate the user-row render where the existing delete button is. Import `AdminResetPasswordDialog` and add a new row action button (key icon) next to delete. Track open state with a `resetTarget` state on the page:

```tsx
import { Key } from "lucide-react";  // add to existing imports
import { AdminResetPasswordDialog } from "@/components/admin-reset-password-dialog";

// inside the component, near other useState:
const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);

// next to the existing delete button in the admin row:
<button
  type="button"
  onClick={() => setResetTarget(u)}
  title={`Reset password for ${u.username}`}
  aria-label={`Reset password for ${u.username}`}
  className="text-foreground-muted hover:text-accent transition-colors p-1 cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
>
  <Key className="h-3.5 w-3.5" aria-hidden />
</button>

// at the bottom of the page, alongside other mounted dialogs:
<AdminResetPasswordDialog
  userId={resetTarget?.id ?? ""}
  username={resetTarget?.username ?? ""}
  open={resetTarget !== null}
  onOpenChange={(o) => { if (!o) setResetTarget(null); }}
/>
```

(Adjust to match the actual JSX shape of the admin row — read 30 lines around the existing delete button to mirror styling and spacing.)

- [ ] **Step 10: Add change-password section to the profile tab**

In `frontend/src/pages/settings.tsx`, find the `"profile"` tab content. Add a section below the existing profile fields:

```tsx
import { changePassword } from "@/lib/api";  // add to existing imports

// inside the component:
const [pwCurrent, setPwCurrent] = useState("");
const [pwNew, setPwNew] = useState("");
const [pwConfirm, setPwConfirm] = useState("");
const [pwError, setPwError] = useState("");
const [pwBusy, setPwBusy] = useState(false);
const [pwOk, setPwOk] = useState(false);

async function handleChangePassword(e: React.FormEvent) {
  e.preventDefault();
  setPwError("");
  setPwOk(false);
  if (pwNew !== pwConfirm) { setPwError("New password and confirmation do not match"); return; }
  if (pwNew.length < 8) { setPwError("New password must be at least 8 characters"); return; }
  setPwBusy(true);
  try {
    await changePassword(pwCurrent, pwNew);
    setPwOk(true);
    setPwCurrent(""); setPwNew(""); setPwConfirm("");
  } catch (e: any) {
    setPwError(e?.message || "Failed to change password");
  } finally {
    setPwBusy(false);
  }
}

// JSX, inside <TabsContent value="profile"> after the existing profile fields:
<section className="space-y-3 pt-6 border-t border-border" aria-labelledby="change-pw-heading">
  <h2 id="change-pw-heading" className="coord-ink">CHANGE PASSWORD</h2>
  <form onSubmit={handleChangePassword} className="space-y-3 max-w-md">
    <div>
      <Label htmlFor="pw-current">Current password</Label>
      <Input id="pw-current" type="password" autoComplete="current-password"
        value={pwCurrent} onChange={(e) => setPwCurrent(e.target.value)} required />
    </div>
    <div>
      <Label htmlFor="pw-new">New password</Label>
      <Input id="pw-new" type="password" autoComplete="new-password"
        value={pwNew} onChange={(e) => setPwNew(e.target.value)} required />
    </div>
    <div>
      <Label htmlFor="pw-confirm">Confirm new password</Label>
      <Input id="pw-confirm" type="password" autoComplete="new-password"
        value={pwConfirm} onChange={(e) => setPwConfirm(e.target.value)} required />
    </div>
    {pwError && <p role="alert" className="text-destructive text-xs font-mono">{pwError}</p>}
    {pwOk && <p className="text-good text-xs font-mono">Password changed.</p>}
    <Button type="submit" disabled={pwBusy}>
      {pwBusy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
      Change password
    </Button>
  </form>
</section>
```

(`text-good` may not be a defined class — use whatever success color the codebase has, or fall back to `text-foreground-muted`. Inspect existing success states in the codebase.)

- [ ] **Step 11: Run the full suite + tsc**

```bash
cd frontend && pnpm vitest run && pnpm tsc --noEmit
```
Expected: all green.

- [ ] **Step 12: Manual smoke (optional, if Vite is up)**

Open `/auth` → "Forgot password?" link visible only in login tab → click → static page → back to login.

Open `/settings?tab=profile` → change password section visible → fill correct current + new → submit → success message.

Open `/settings?tab=admin` (as admin) → see key icon next to delete on user row → click → dialog → Generate → temp password shown → Copy works.

- [ ] **Step 13: Commit**

```bash
git add frontend/src/pages/auth-forgot.tsx \
        frontend/src/pages/auth.tsx \
        frontend/src/pages/settings.tsx \
        frontend/src/components/admin-reset-password-dialog.tsx \
        frontend/src/pages/__tests__/auth-forgot.test.tsx \
        frontend/src/components/__tests__/admin-reset-password-dialog.test.tsx \
        $(grep -rl "createBrowserRouter\|<Routes>" frontend/src/ | head -3)
git commit -m "feat(ui): password recovery — forgot link, change-password form, admin reset dialog"
```

---

## Task 9 — Integration verification + deploy

**Files:** (no source changes)

- [ ] **Step 1: Rebuild backend container so all Tasks 1–8 changes are picked up**

```bash
cd /Users/kwoo2/Desktop/storage/akb && docker compose up -d --build backend
until curl -sf http://localhost:8000/livez >/dev/null 2>&1; do sleep 2; done
```

- [ ] **Step 2: Backend e2e sweep**

```bash
AKB_URL=http://localhost:8000 bash backend/tests/test_auth_password_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_mcp_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_collection_lifecycle_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_vault_templates_e2e.sh
```
Expected: all four green.

- [ ] **Step 3: Backend unit sweep**

```bash
cd backend && AKB_TEST_DSN="postgresql://akb:akb@localhost:15432/akb" \
  uv run --extra dev pytest tests/test_password_service.py \
                              tests/test_auth_change_password.py \
                              tests/test_collection_repo.py \
                              tests/test_collection_service.py \
                              tests/test_template_registry.py \
                              tests/test_git_service.py -v 2>&1 | tail -15
```
Expected: all green.

- [ ] **Step 4: Frontend full sweep**

```bash
cd frontend && pnpm vitest run && pnpm tsc --noEmit
```
Expected: all green.

- [ ] **Step 5: Manual smoke against production (if deploying)**

After `deploy/k8s/internal/deploy-internal.sh`:
- Log into `https://akb.agent.seahorse.dnotitia.com` as a test user.
- Settings → Profile → change password to a new value → success.
- Log out, log back in with the new password.
- As admin, Settings → Admin → click key icon on a user → Generate → confirm temp password renders + copy works.
- CLI smoke against the k8s pod (image is the same pip-installed one; no `uv`):
  ```bash
  kubectl exec -n akb deploy/backend -- python -m app.cli reset-password <test-user>
  ```
- Use the printed temp password to log in as that user.

- [ ] **Step 6: Push + deploy**

```bash
git push origin main
bash deploy/k8s/internal/deploy-internal.sh
curl -sk https://akb.agent.seahorse.dnotitia.com/livez
```

---

## Notes for the executing engineer

- The CLI is invoked **inside the backend container** (`docker compose exec backend …` or `kubectl exec deploy/backend …`). It runs under the same `uv` environment as the FastAPI app; no separate install needed.
- `events.actor_id` is a nullable TEXT column (`migrations/015_events_outbox.py`). CLI inserts NULL there; the `auth.password_reset` payload's `method: "cli"` is the distinguishing signal.
- JWT revocation on reset is an **acknowledged gap** (see spec § Non-goals). A user whose password was reset can continue to use an existing JWT until natural expiry on whichever device still has it. The threat model accepts this because the admin reset is intentional and operator-mediated.
- The change-password form intentionally stays logged in after success — the page lives at Settings, and forcing logout would be a UX regression with no security gain (the user just authenticated with `current_password`).
- If you find that an existing `settings.tsx` profile tab already has its own form structure (heading, labeled fields), match its visual rhythm rather than the snippet here. The snippet shows the data flow; the styling should align with neighbors.
- `Key` from `lucide-react` is the icon for the admin row's reset action. It's universally available in lucide-react releases; verified by running `grep -rn "from \"lucide-react\"" frontend/src/ | head` to scan the existing icon usage.
