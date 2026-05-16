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
    return bcrypt.checkpw(password.encode(), password_hash.encode())


# ── JWT ──────────────────────────────────────────────────────

def create_jwt(user_id: str, username: str) -> str:
    payload = {
        "sub": user_id,
        "username": username,
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours),
        "iat": datetime.now(timezone.utc),
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

    return {"user_id": str(user_id), "username": username, "email": email}


async def login(username: str, password: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, email, password_hash, display_name, is_admin FROM users WHERE username = $1 OR email = $1",
            username,
        )
        if not row or not verify_password(password, row["password_hash"]):
            raise AuthenticationError("Invalid credentials")

        token = create_jwt(str(row["id"]), row["username"])
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

async def create_pat(user_id: str, name: str, scopes: list[str] | None = None, expires_days: int | None = None) -> dict:
    pool = await get_pool()
    raw_token, token_hash, token_prefix = generate_pat()

    expires_at = None
    if expires_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

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
            scopes or ["read", "write"],
            expires_at,
        )

    return {
        "token": raw_token,
        "token_id": str(token_id),
        "name": name,
        "prefix": token_prefix,
        "scopes": scopes or ["read", "write"],
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

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, email, display_name, is_admin FROM users WHERE id = $1",
            uuid.UUID(payload["sub"]),
        )
        if not row:
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
        row = await conn.fetchrow(
            """
            SELECT t.id as token_id, t.user_id, t.scopes, t.expires_at,
                   u.username, u.email, u.display_name, u.is_admin
            FROM tokens t
            JOIN users u ON t.user_id = u.id
            WHERE t.token_hash = $1
            """,
            token_hash,
        )
        if not row:
            return None

        # Check expiry
        if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
            return None

        # Update last_used_at
        await conn.execute(
            "UPDATE tokens SET last_used_at = $1 WHERE id = $2",
            datetime.now(timezone.utc), row["token_id"],
        )

        return AuthenticatedUser(
            user_id=str(row["user_id"]),
            username=row["username"],
            email=row["email"],
            display_name=row["display_name"],
            is_admin=row["is_admin"],
            auth_method="pat",
        )
