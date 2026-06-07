"""Authentication service — users, JWT, PAT.

Handles:
- User registration and login (bcrypt + JWT)
- PAT creation, validation, and revocation
- Token-based identity resolution for requests
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass

import asyncpg
import bcrypt
import jwt

from app.config import settings
from app.db.postgres import get_pool
from app.exceptions import AuthenticationError, ConflictError, NotFoundError, ValidationError
from app.repositories.events_repo import emit_event
from app.services.role_sync import get_role_sync


@dataclass
class AuthenticatedUser:
    user_id: str
    username: str
    email: str
    display_name: str | None
    is_admin: bool
    auth_method: str  # "jwt" or "pat"


# ── Password hashing ────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    # Defensive: an SSO-provisioned account stores a non-bcrypt sentinel
    # hash (see provision_keycloak_user). bcrypt.checkpw raises ValueError
    # on a malformed hash; treat that as "no match" so a stray local-login
    # attempt against an SSO account returns 401, not 500.
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# ── JWT ──────────────────────────────────────────────────────

def create_jwt(
    user_id: str,
    username: str,
    *,
    not_before: datetime | None = None,
) -> str:
    """Encode a JWT for ``user_id``.

    ``not_before`` lets a caller pin the ``iat`` claim past a known
    revocation cutoff so a token issued in the same second as a
    revoke is born already valid (otherwise the iat-second comparison
    in :func:`resolve_token` would reject it for up to 1s).
    """
    now = datetime.now(timezone.utc)
    iat = now if not_before is None or not_before <= now else not_before
    payload = {
        "sub": user_id,
        "username": username,
        "exp": iat + timedelta(hours=settings.jwt_expire_hours),
        "iat": iat,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_jwt(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


# ── PAT ──────────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_pat() -> tuple[str, str, str]:
    """Generate a PAT. Returns (raw_token, token_hash, token_prefix)."""
    raw = "akb_" + secrets.token_urlsafe(32)
    return raw, _hash_token(raw), raw[:12]


# ── User operations ─────────────────────────────────────────

async def register(username: str, email: str, password: str, display_name: str | None = None) -> dict:
    pool = await get_pool()
    pw_hash = hash_password(password)
    user_id = uuid.uuid4()

    async with pool.acquire() as conn:
        # Check duplicates
        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE username = $1 OR email = $2",
            username, email,
        )
        if existing:
            raise ConflictError("Username or email already exists")

        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, display_name)
            VALUES ($1, $2, $3, $4, $5)
            """,
            user_id, username, email, pw_hash, display_name,
        )

    # PG-native RBAC: emit the per-user PG role so akb_sql works.
    # Best-effort — reconciler at next startup catches any failure here.
    await get_role_sync().on_user_create(user_id)

    return {"user_id": str(user_id), "username": username, "email": email}


async def login(username: str, password: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, username, email, password_hash, display_name, is_admin,
                   tokens_revoked_before, auth_provider
              FROM users WHERE username = $1 OR email = $1
            """,
            username,
        )
        # SSO-provisioned accounts have no usable local password. Reject
        # the local-login path explicitly with the same generic message
        # so we don't leak which accounts are SSO-backed.
        if row and row["auth_provider"] != "local":
            raise AuthenticationError("Invalid credentials")
        if not row or not verify_password(password, row["password_hash"]):
            raise AuthenticationError("Invalid credentials")

        # Push iat past the revocation cutoff so a login in the same
        # whole second as a revoke (admin reset, change_password) still
        # yields a usable token. resolve_token compares against
        # CEIL(epoch) so the safe boundary is cutoff + 1s rounded up.
        not_before = row["tokens_revoked_before"] + timedelta(seconds=1)
        token = create_jwt(
            str(row["id"]), row["username"], not_before=not_before,
        )
        return {
            "token": token,
            "user": {
                "id": str(row["id"]),
                "username": row["username"],
                "email": row["email"],
                "display_name": row["display_name"],
                "is_admin": row["is_admin"],
            },
        }


# ── Keycloak SSO (optional external IdP) ─────────────────────────────
#
# bcrypt hashes start with "$2"; this sentinel is deliberately NOT a
# valid bcrypt hash, so an SSO account can never be authenticated through
# /auth/login (verify_password returns False on the malformed hash, and
# login() refuses non-'local' providers up front anyway).
_SSO_SENTINEL_HASH = "!keycloak-sso:no-local-login!"


async def _unique_username(conn, base: str | None) -> str:
    """Derive a unique username from a Keycloak claim, suffixing on collision."""
    base = (base or "").strip() or "user"
    candidate = base
    for _ in range(8):
        taken = await conn.fetchval("SELECT 1 FROM users WHERE username = $1", candidate)
        if not taken:
            return candidate
        candidate = f"{base}-{secrets.token_hex(2)}"
    return f"{base}-{secrets.token_hex(4)}"


async def login_with_keycloak_claims(claims: dict) -> dict:
    """Map a verified Keycloak ID token to an AKB session.

    First login JIT-provisions an AKB user (keyed by email) and its
    per-user PG role; subsequent logins reuse the row. Returns the same
    ``{token, user}`` shape as :func:`login` so the SSO callback path is
    indistinguishable downstream.

    Keycloak is authentication only — ``realm_access.roles`` are NOT
    mapped to AKB ``is_admin`` or vault grants here (see the design doc).
    """
    email = (claims.get("email") or "").strip().lower()
    if not email:
        # AKB keys identity on email; without it we cannot map to a user.
        raise AuthenticationError("Keycloak account has no email claim")
    # Identity is keyed on email — refuse unverified emails so a realm that
    # permits self-asserted addresses can't be used to provision/adopt an
    # AKB account under someone else's email. Gated so a trusted realm can
    # opt out (keycloak_require_verified_email=false).
    if settings.keycloak_require_verified_email and claims.get("email_verified") is not True:
        raise AuthenticationError("Identity provider has not verified this email address")
    display_name = claims.get("name") or claims.get("preferred_username")
    preferred_username = claims.get("preferred_username") or email.split("@")[0]

    pool = await get_pool()
    new_user_id: uuid.UUID | None = None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, username, email, display_name, is_admin,
                   tokens_revoked_before, auth_provider
              FROM users WHERE email = $1
            """,
            email,
        )
        if row is not None:
            if row["auth_provider"] != "keycloak":
                # A local account already owns this email. Auto-linking is
                # deliberately out of scope (design doc) — fail loudly
                # rather than silently merging identities.
                raise ConflictError(
                    "An account with this email already exists with password "
                    "login; SSO linking is not enabled. Contact an admin."
                )
            user_id = row["id"]
            uname = row["username"]
            not_before = row["tokens_revoked_before"] + timedelta(seconds=1)
            user_payload = {
                "id": str(row["id"]),
                "username": row["username"],
                "email": row["email"],
                "display_name": row["display_name"],
                "is_admin": row["is_admin"],
            }
        else:
            user_id = uuid.uuid4()
            uname = await _unique_username(conn, preferred_username)
            try:
                # INSERT + outbox event in one tx so a rollback drops both.
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO users (id, username, email, password_hash,
                                           display_name, auth_provider)
                        VALUES ($1, $2, $3, $4, $5, 'keycloak')
                        """,
                        user_id, uname, email, _SSO_SENTINEL_HASH, display_name,
                    )
                    await emit_event(
                        conn,
                        "auth.user_provisioned",
                        actor_id=str(user_id),
                        payload={"auth_provider": "keycloak", "email": email},
                    )
                new_user_id = user_id
                not_before = None
                user_payload = {
                    "id": str(user_id),
                    "username": uname,
                    "email": email,
                    "display_name": display_name,
                    "is_admin": False,
                }
            except asyncpg.UniqueViolationError:
                # Concurrent first-login for the same email won the race.
                # Re-fetch and treat as the existing user (idempotent JIT)
                # instead of bubbling a raw 500.
                row = await conn.fetchrow(
                    """
                    SELECT id, username, email, display_name, is_admin,
                           tokens_revoked_before, auth_provider
                      FROM users WHERE email = $1
                    """,
                    email,
                )
                if row is None or row["auth_provider"] != "keycloak":
                    raise ConflictError(
                        "An account with this email already exists; "
                        "SSO linking is not enabled. Contact an admin."
                    )
                user_id = row["id"]
                uname = row["username"]
                not_before = row["tokens_revoked_before"] + timedelta(seconds=1)
                user_payload = {
                    "id": str(row["id"]),
                    "username": row["username"],
                    "email": row["email"],
                    "display_name": row["display_name"],
                    "is_admin": row["is_admin"],
                }

    # PG-native RBAC: create the per-user role outside the insert's
    # connection, mirroring register(). Best-effort — the reconciler at
    # next startup rebuilds any role this misses.
    if new_user_id is not None:
        await get_role_sync().on_user_create(new_user_id)

    token = create_jwt(str(user_id), uname, not_before=not_before)
    return {"token": token, "user": user_payload}


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
            # Otherwise a thief holding an old JWT would keep access
            # for up to jwt_expire_hours after the password change.
            await _revoke_sessions_in_conn(
                conn,
                uuid.UUID(user_id),
                actor_id=user_id,
                reason=REVOKE_REASON_PASSWORD_CHANGE,
            )
            # Users are not URI-addressable resources (the URI scheme
            # covers in-vault resources only). Subscribers identify the
            # user via `actor_id` + payload.user_id.
            await emit_event(
                conn,
                "auth.password_changed",
                resource_uri=None,
                actor_id=user_id,
                payload={"user_id": user_id},
            )


async def update_profile(
    user_id: str,
    *,
    display_name: str | None = None,
    email: str | None = None,
) -> dict:
    """Update own display_name and/or email. At least one field required.

    Returns the post-update row (username + display_name + email) for the
    caller to refresh local state. Username is immutable here — change
    it requires a separate admin flow.
    """
    sets: list[str] = []
    params: list = []  # mixed: str display_name/email + UUID at the end
    idx = 1
    if display_name is not None:
        sets.append(f"display_name = ${idx}")
        params.append(display_name)
        idx += 1
    if email is not None:
        sets.append(f"email = ${idx}")
        params.append(email)
        idx += 1
    if not sets:
        raise ValidationError("Nothing to update — pass display_name or email")

    sets.append("updated_at = NOW()")
    params.append(uuid.UUID(user_id))

    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                f"UPDATE users SET {', '.join(sets)} WHERE id = ${idx} "
                f"RETURNING username, display_name, email",
                *params,
            )
        except asyncpg.UniqueViolationError:
            raise ConflictError("Email already in use") from None
        if row is None:
            raise NotFoundError("User", user_id)
        return {
            "updated": True,
            "username": row["username"],
            "display_name": row["display_name"],
            "email": row["email"],
        }


# ── PAT operations ──────────────────────────────────────────

async def create_pat(user_id: str, name: str, *, expires_days: int | None = None) -> dict:
    """Issue a Personal Access Token.

    Scopes are NOT a caller-tunable knob: the backend doesn't enforce
    them anywhere, so accepting a `scopes` argument would falsely
    imply that a "read-only" PAT exists. Tokens always store the full
    `[read, write]` default; the response surfaces it so listings stay
    consistent. When scope enforcement is wired into the request
    handlers, re-introduce the argument with the matching check.
    """
    pool = await get_pool()
    raw_token, token_hash, token_prefix = generate_pat()

    expires_at = None
    if expires_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

    default_scopes = ["read", "write"]

    async with pool.acquire() as conn:
        token_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO tokens (id, user_id, name, token_hash, token_prefix, scopes, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            token_id,
            uuid.UUID(user_id),
            name,
            token_hash,
            token_prefix,
            default_scopes,
            expires_at,
        )

    return {
        "token": raw_token,
        "token_id": str(token_id),
        "name": name,
        "prefix": token_prefix,
        "scopes": default_scopes,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "note": "Save this token — it won't be shown again.",
    }


async def list_pats(user_id: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, token_prefix, scopes, expires_at, last_used_at, created_at
            FROM tokens WHERE user_id = $1 ORDER BY created_at DESC
            """,
            uuid.UUID(user_id),
        )
        return [
            {
                "token_id": str(r["id"]),
                "name": r["name"],
                "prefix": r["token_prefix"],
                "scopes": list(r["scopes"]) if r["scopes"] else [],
                "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
                "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
                "created_at": r["created_at"].isoformat(),
            }
            for r in rows
        ]


async def revoke_pat(user_id: str, token_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM tokens WHERE id = $1 AND user_id = $2",
            uuid.UUID(token_id), uuid.UUID(user_id),
        )
        return "DELETE 1" in result


# ── JWT revocation ──────────────────────────────────────────

# Canonical reasons recorded on the auth.sessions_revoked event. Keep
# stable — SIEM/audit subscribers filter on these.
REVOKE_REASON_SELF = "self"
REVOKE_REASON_ADMIN = "admin"
REVOKE_REASON_PASSWORD_CHANGE = "password_change"  # pragma: allowlist secret
REVOKE_REASON_PASSWORD_RESET = "password_reset"  # pragma: allowlist secret


async def _revoke_sessions_in_conn(
    conn,
    user_id: uuid.UUID,
    *,
    actor_id: str,
    reason: str,
) -> datetime:
    """Bump tokens_revoked_before and emit auth.sessions_revoked.

    Caller MUST be inside ``async with conn.transaction()`` so the cutoff
    and the audit event commit atomically with whatever wrapping write
    triggered the revoke (password change, reset, explicit revoke).
    """
    row = await conn.fetchrow(
        """
        UPDATE users
           SET tokens_revoked_before = NOW(),
               updated_at = NOW()
         WHERE id = $1
     RETURNING tokens_revoked_before
        """,
        user_id,
    )
    if row is None:
        raise NotFoundError("User", str(user_id))
    await emit_event(
        conn,
        "auth.sessions_revoked",
        resource_uri=None,
        actor_id=actor_id,
        payload={
            "user_id": str(user_id),
            "reason": reason,
        },
    )
    return row["tokens_revoked_before"]


async def revoke_all_sessions(
    user_id: str,
    *,
    actor_id: str | None = None,
    reason: str = REVOKE_REASON_SELF,
) -> datetime:
    """Invalidate every JWT issued to ``user_id`` before the call.

    Returns the cutoff timestamp. Any JWT with ``iat`` strictly less than
    this is rejected by :func:`resolve_token`. The caller's own JWT is
    invalidated too — the response is the last action that token can take.

    PATs are intentionally NOT touched. Mixing them would surprise
    pipelines that store a PAT and never expect "I changed my password"
    to break the integration.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await _revoke_sessions_in_conn(
                conn,
                uuid.UUID(user_id),
                actor_id=actor_id or user_id,
                reason=reason,
            )


# ── Token resolution (JWT or PAT) ───────────────────────────

async def resolve_token(authorization: str) -> AuthenticatedUser | None:
    """Resolve an Authorization header to an authenticated user.

    Supports:
    - Bearer <jwt>
    - Bearer akb_<pat>
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None

    token = authorization[7:]

    # PAT (starts with akb_)
    if token.startswith("akb_"):
        return await _resolve_pat(token)

    # JWT
    payload = decode_jwt(token)
    if not payload:
        return None
    iat = payload.get("iat")
    if iat is None:
        # Required for the revocation cutoff comparison below.
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, username, email, display_name, is_admin,
                   CEIL(EXTRACT(EPOCH FROM tokens_revoked_before))::bigint
                       AS revoked_epoch_ceil
              FROM users WHERE id = $1
            """,
            uuid.UUID(payload["sub"]),
        )
        if not row:
            return None

        # Server-side JWT revocation. The user can void every token
        # they have by setting tokens_revoked_before = NOW(); any JWT
        # whose iat predates that cutoff fails here even though the
        # signature is valid and exp has not passed. This is the only
        # mechanism to invalidate a leaked or stale JWT short of
        # rotating the global jwt_secret (which would log every user
        # out, not just one).
        #
        # JWT iat is whole-second (RFC 7519). tokens_revoked_before is
        # sub-second TIMESTAMPTZ. To make same-second writes safe we
        # compare against CEIL(epoch) — a revoke at 100.5s yields
        # revoked_epoch_ceil=101, so any iat≤100 fails (rejected) and
        # iat≥101 passes (post-sleep re-login). Without CEIL, a JWT
        # issued in the same second as revoke would survive.
        if int(iat) < int(row["revoked_epoch_ceil"]):
            return None

        return AuthenticatedUser(
            user_id=str(row["id"]),
            username=row["username"],
            email=row["email"],
            display_name=row["display_name"],
            is_admin=row["is_admin"],
            auth_method="jwt",
        )


async def _resolve_pat(raw_token: str) -> AuthenticatedUser | None:
    token_hash = _hash_token(raw_token)
    pool = await get_pool()

    async with pool.acquire() as conn:
        # Atomic fetch+update: a concurrent revoke (DELETE) between
        # SELECT and UPDATE would have allowed the in-flight request
        # to authenticate successfully against a row that no longer
        # exists. Collapse to one UPDATE...RETURNING so the row is
        # either still valid (RETURNING produces a row) or already
        # gone/expired (0 rows → auth fails).
        row = await conn.fetchrow(
            """
            UPDATE tokens t
               SET last_used_at = NOW()
              FROM users u
             WHERE t.user_id = u.id
               AND t.token_hash = $1
               AND (t.expires_at IS NULL OR t.expires_at > NOW())
            RETURNING t.id AS token_id, t.user_id, t.scopes, t.expires_at,
                      u.username, u.email, u.display_name, u.is_admin
            """,
            token_hash,
        )
        if not row:
            return None

        return AuthenticatedUser(
            user_id=str(row["user_id"]),
            username=row["username"],
            email=row["email"],
            display_name=row["display_name"],
            is_admin=row["is_admin"],
            auth_method="pat",
        )
