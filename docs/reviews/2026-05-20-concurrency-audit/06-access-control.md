# Access Control & Auth — Findings

## Summary

Nine distinct defects were found spanning TOCTOU on ownership transfer, stale `is_admin` elevation, unguarded session creation, `recent` activity leaking public-vault docs without access checks, `grep` passing a raw string UUID against UUID columns, PAT `last_used_at` written outside a transaction after the auth decision is already made, no audit trail for any access-control mutation, and an orphan-window on vault delete that allows in-flight writers to commit after the permission check but before cascade.

---

## Findings

### F1: `transfer_ownership` Executes Three Mutations Without a Transaction — Severity: HIGH

**Surface**: `POST /vaults/{vault}/transfer` → `transfer_ownership()`
**File**: `backend/app/services/access_service.py:429-454`

**Scenario**: Function acquires a connection but never opens a transaction. Three writes execute as separate auto-commits:
1. `UPDATE vaults SET owner_id`
2. `INSERT/UPDATE vault_access` (giving old owner admin)
3. `DELETE FROM vault_access` (removing new owner's explicit row)

If process crashes or event loop is interrupted between any pair, the vault is left partially transferred.

**Observable bad state**: Old owner locked out, or two users both believing themselves to be owner.

**Fix direction**: Wrap all three writes in `async with conn.transaction()`.

**Evidence**:
```python
# access_service.py:429-454 — no conn.transaction() anywhere
async with pool.acquire() as conn:
    vault = await conn.fetchrow(...)
    await conn.execute("UPDATE vaults ...")
    await conn.execute("INSERT INTO vault_access ...")
    await conn.execute("DELETE FROM vault_access ...")
```

---

### F2: TOCTOU Between `check_vault_access` and Subsequent Mutations — Severity: HIGH

**Surface**: All mutating endpoints (`grant_access`, `revoke_access`, `delete_vault`, `archive_vault`, `PUT /documents`, etc.)

**Files**:
- `backend/app/services/access_service.py:124-153` (`grant_access`)
- `backend/app/services/access_service.py:156-177` (`revoke_access`)
- `backend/app/services/access_service.py:586-652` (`delete_vault`)

**Scenario**: `check_vault_access` opens and closes its own connection, then the mutation opens a second connection. Between the two, another request can revoke the caller's role. The revoke commits but the mutation still proceeds.

Example: Alice has `admin`. Bob (owner) revokes Alice concurrently with Alice's `grant_access(..., Charlie, writer)`. Alice's check passes (still admin at read time), Bob's revoke commits, Alice's INSERT commits — Charlie now has access granted by a revoked admin.

**Observable bad state**: Revoked admin can grant/revoke/delete for a brief window after revocation.

**Fix direction**: Acquire `SELECT ... FOR UPDATE` on the `vaults` (or `vault_access`) row inside the same connection and transaction that performs the mutation.

---

### F3: No JWT Revocation Path After Admin Demotion — Severity: HIGH

**Surface**: JWT auth path `resolve_token`
**File**: `backend/app/services/auth_service.py:318-333`

**Scenario**: `is_admin` is re-read from DB on each request, so demotion is reflected. BUT there is no server-side mechanism to invalidate already-issued JWTs. A stolen or voluntarily-shared admin JWT remains valid for 24 h with no revoke path short of rotating `jwt_secret` (which invalidates ALL tokens).

**Observable bad state**: Stolen admin JWT usable for up to 24 hours.

**Fix direction**: Add `revoked_before` timestamp or token-generation counter to `users` table; check in `resolve_token`. Expose `POST /auth/revoke-all-sessions`.

**Evidence**:
```python
# auth_service.py:49-56 — JWT payload has no jti / revocation field
payload = {
    "sub": user_id,
    "username": username,
    "exp": ..., "iat": ...,
}
# config.py:83
jwt_expire_hours: int = 24
```

---

### F4: `POST /sessions/start` Does Not Check Vault Membership — Severity: MED

**Surface**: `POST /sessions/start`
**File**: `backend/app/api/routes/sessions.py:20-22`

**Scenario**: Requires valid JWT/PAT but passes `vault` name directly to `SessionService.start_session` without `check_vault_access`. The only DB check is `SELECT id FROM vaults WHERE name = $1` — verifies existence, not membership.

**Observable bad state**: Any authenticated user can open sessions on any private vault, polluting session history and memory store via `auto_summarize_session`.

**Fix direction**: Add `check_vault_access(user.user_id, vault, required_role="reader")` in the route or service.

---

### F5: `GET /recent` Excludes Public Vaults From Unfiltered Query — Severity: MED

**Surface**: `GET /recent` (no `vault` param)
**File**: `backend/app/api/routes/sessions.py:79-93`

**Scenario**: When `vault` omitted, query filters by `v.owner_id = $1 OR va.user_id = $1`. Public vaults (`public_access != 'none'`) are NOT included. Inconsistent with `list_accessible_vaults` and `search_service.search`.

**Observable bad state**: User sees documents from accessible public vaults in `/search` but not in `/recent`.

**Fix direction**: Add `OR v.public_access IN ('reader', 'writer')` mirroring the search ACL predicate.

**Evidence**: Compare `sessions.py:86-92` vs `search_service.py:105-109`.

---

### F6: `grep` Passes Raw String `user_id` Against UUID Columns; `None` Bypasses ACL — Severity: MED

**Surface**: `GET /grep` (no `vault` param) → `SearchService.grep`
**File**: `backend/app/services/search_service.py:487-496`

**Scenario**: When `vault=None`, the `elif user_id:` branch applies the ACL predicate. If `user_id` is also `None` (the signature allows `str | None`), the branch is skipped and the query runs with NO ACL — returning chunks from every vault. The REST route always passes `user_id`, but any internal/MCP caller invoking `grep(pattern=..., vault=None, user_id=None)` gets an unfiltered cross-vault dump.

Additionally, `user_id` is `str` passed against `UUID`-typed columns; type errors on malformed values propagate as 500.

**Observable bad state**: `user_id=None` + `vault=None` → cross-vault dump.

**Fix direction**: Validate at least one of `vault`/`user_id` is non-None. Cast `user_id` to `uuid.UUID`.

---

### F7: PAT `last_used_at` Update Races With Concurrent Revocation — Severity: MED

**Surface**: PAT auth path `_resolve_pat`
**File**: `backend/app/services/auth_service.py:340-370`

**Scenario**: `_resolve_pat` fetches the token row, then in a separate statement updates `last_used_at`. Between them, `revoke_pat` can DELETE the row. The UPDATE silently affects 0 rows; current request continues authenticated with the now-deleted token.

**Observable bad state**: Revoked PAT successfully completes the in-flight request.

**Fix direction**: Combine into a single `UPDATE tokens SET last_used_at = NOW() WHERE token_hash = $1 AND (expires_at IS NULL OR expires_at > NOW()) RETURNING ...`. If 0 rows returned, fail auth.

---

### F8: `view_count` Increment Is a Non-Atomic Read-Then-Write — Severity: LOW

**Surface**: `GET /public/{slug}` → `resolve_publication`
**File**: `backend/app/services/publication_service.py:588-604`

(Same as Report 04 F7.) Concurrent reads see same `view_count`, all pass `max_views` check, all increment. `max_views=1` publication served multiple times.

**Fix direction**: `UPDATE publications SET view_count = view_count + 1 WHERE id = $1 AND (max_views IS NULL OR view_count < max_views) RETURNING ...`.

---

### F9: No Audit Events for Any Access-Control Mutation — Severity: LOW

**Surface**: `grant_access`, `revoke_access`, `transfer_ownership`, `archive_vault`, `delete_vault`, `delete_user_account`
**File**: `backend/app/services/access_service.py` — entire file

**Scenario**: `auth_service.change_password` and `password_service.reset_password` emit_event inside the mutation TX. `access_service` never calls `emit_event`. No audit trail for privilege escalation or ownership transfer.

**Observable bad state**: A compromised admin can manipulate ACLs without any audit record.

**Fix direction**: Call `emit_event(conn, "access.grant" / "access.revoke" / ...)` inside the mutation TX, mirroring `password_service.py:61-71`.

**Evidence**: `grep -n emit_event backend/app/services/access_service.py` → no matches.

---

## Essential Files

- `backend/app/services/access_service.py` — all ACL checks, grant/revoke/transfer/archive/delete
- `backend/app/services/auth_service.py` — JWT, PAT, `_resolve_pat`, `change_password`
- `backend/app/services/password_service.py` — audit pattern reference
- `backend/app/api/deps.py` — `get_current_user`, `get_optional_user`
- `backend/app/api/routes/access.py`, `routes/sessions.py`
- `backend/app/services/search_service.py:408-496` — grep ACL
- `backend/app/services/publication_service.py:548-610` — view_count
- `backend/app/db/init.sql:60-72` — `vault_access` schema
- `backend/app/config.py` — `jwt_expire_hours`
