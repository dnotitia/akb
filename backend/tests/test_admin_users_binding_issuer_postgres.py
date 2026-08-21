"""PostgreSQL contract: the account listing says WHICH issuer an account is bound under.

A control plane that has moved a workspace to its own realm needs to tell, for
each person, whether the binding they already have is the one this runtime will
accept. `has_external_identity` cannot answer that: it says a binding exists,
not that it is the right one, and after an issuer move every stale account still
answers true. That gap is what makes a migrated member show as ready while being
unable to sign in.

The runtime is the authority on what it presents, so it answers rather than
handing out parts for a caller to recombine — an issuer derived a second way
somewhere else would be a second answer to a question that must have one.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)
_PRESENTED = "https://auth-team1.example.invalid/realms/akb"
_LEFT_BEHIND = "https://shared.example.invalid/realms/akb-platform"


@pytest.fixture
async def listing(monkeypatch):
    try:
        admin = await asyncpg.connect(_DSN, timeout=2.0)
    except (OSError, asyncpg.PostgresError):
        if os.environ.get("REQUIRE_REAL_PG") == "1":
            pytest.fail("REQUIRE_REAL_PG=1 but Postgres is not reachable at AKB_TEST_DSN")
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    schema = "admin_users_binding_issuer_" + uuid.uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        _DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE users (
                    id UUID PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    is_admin BOOLEAN NOT NULL DEFAULT false,
                    auth_provider TEXT NOT NULL,
                    account_status TEXT NOT NULL,
                    account_kind TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE external_identities (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    issuer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    username_snapshot TEXT,
                    email_snapshot TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (issuer, subject)
                );
                CREATE TABLE vaults (
                    id UUID PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    owner_id UUID REFERENCES users(id)
                );
                """
            )

        from app.services import access_service

        async def _get_pool():
            return pool

        monkeypatch.setattr(access_service, "get_pool", _get_pool)
        yield pool, access_service
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _seed(pool) -> dict[str, uuid.UUID]:
    ids = {name: uuid.uuid4() for name in ("carried", "left_behind", "unbound", "service")}
    async with pool.acquire() as conn:
        for name, user_id in ids.items():
            await conn.execute(
                """
                INSERT INTO users (id, username, email, display_name, is_admin,
                                   auth_provider, account_status, account_kind)
                VALUES ($1, $2, $3, $2, false, $4, 'active', $5)
                """,
                user_id, name, f"{name}@example.invalid",
                "keycloak" if name != "service" else "service",
                "service" if name == "service" else "human",
            )
        await conn.execute(
            "INSERT INTO external_identities (user_id, issuer, subject) VALUES ($1,$2,'s1')",
            ids["carried"], _PRESENTED,
        )
        await conn.execute(
            "INSERT INTO external_identities (user_id, issuer, subject) VALUES ($1,$2,'s2')",
            ids["left_behind"], _LEFT_BEHIND,
        )
    return ids


def _by_username(users: list[dict]) -> dict[str, dict]:
    return {u["username"]: u for u in users}


async def test_listing_distinguishes_a_carried_binding_from_one_left_behind(
    listing, monkeypatch
):
    pool, access_service = listing
    await _seed(pool)
    monkeypatch.setattr(access_service, "presented_issuer_or_none", lambda: _PRESENTED)

    users = _by_username(await access_service.list_all_users_admin())

    assert users["carried"]["identity_issuers"] == [_PRESENTED]
    assert users["carried"]["bound_to_presented_issuer"] is True

    # The whole point: this account HAS a binding, and `has_external_identity`
    # would say so. It is bound to the issuer the workspace left.
    assert users["left_behind"]["identity_issuers"] == [_LEFT_BEHIND]
    assert users["left_behind"]["bound_to_presented_issuer"] is False

    assert users["unbound"]["identity_issuers"] == []
    assert users["unbound"]["bound_to_presented_issuer"] is False


async def test_a_runtime_that_presents_no_issuer_answers_unknown_not_false(
    listing, monkeypatch
):
    """Local mode has no issuer, so "is this binding stale" has no answer here.

    `False` would be a lie shaped exactly like the defect this field exists to
    remove — every account would read as needing to sign in again. The absence
    has to travel as absence.
    """
    pool, access_service = listing
    await _seed(pool)
    monkeypatch.setattr(access_service, "presented_issuer_or_none", lambda: None)

    users = _by_username(await access_service.list_all_users_admin())

    assert users["carried"]["bound_to_presented_issuer"] is None
    assert users["left_behind"]["bound_to_presented_issuer"] is None
    # The issuers themselves are still reported: they are facts about the rows,
    # not about what this runtime accepts.
    assert users["left_behind"]["identity_issuers"] == [_LEFT_BEHIND]


async def test_the_route_reports_the_issuer_this_runtime_presents(monkeypatch):
    """The caller must not have to derive the issuer to compare against."""
    from app.api.routes import access
    from app.services.auth_service import AuthenticatedUser

    monkeypatch.setattr(access, "presented_issuer_or_none", lambda: _PRESENTED)

    async def _users() -> list[dict]:
        return [{"username": "carried", "bound_to_presented_issuer": True}]

    monkeypatch.setattr(access, "list_all_users_admin", _users)
    result = await access.admin_list_users(
        AuthenticatedUser(
            user_id=str(uuid.uuid4()), username="platform-bot",
            email="platform-bot@workspace.local", display_name=None,
            is_admin=True, auth_method="pat", account_kind="service",
            key_class="service",
        )
    )

    assert result["presented_issuer"] == _PRESENTED
    assert result["users"][0]["bound_to_presented_issuer"] is True
