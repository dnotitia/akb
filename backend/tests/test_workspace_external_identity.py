"""Stable OIDC identity resolution and managed admission tests."""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

from app.config import settings
from app.exceptions import (
    AccountSuspendedError,
    ExternalIdentityConflictError,
    MembershipRequiredError,
)


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_ISSUER = "https://id.example.com/realms/akb"


@pytest.fixture
async def pool(monkeypatch):
    try:
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
    except Exception:
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    from app.db.postgres import _load_migration
    from app.services import auth_service

    migration = _load_migration("043_workspace_account_governance.py")
    assert migration is not None
    async with pool.acquire() as conn:
        await migration.migrate(conn=conn)

    async def _get_pool():
        return pool

    monkeypatch.setattr(auth_service, "get_pool", _get_pool)
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "open", raising=False)
    monkeypatch.setattr(settings, "keycloak_require_verified_email", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_link_by_email", False, raising=False)
    yield pool

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username LIKE 'wsg-%'")
        await conn.execute("DELETE FROM events WHERE actor_id LIKE 'wsg-%'")
    await pool.close()


def _claims(subject: str, email: str | None = None) -> dict:
    claims = {
        "iss": _ISSUER,
        "sub": subject,
        "preferred_username": f"wsg-{subject}",
    }
    if email is not None:
        claims.update({"email": email, "email_verified": True})
    return claims


async def _insert_bound_user(
    pool,
    *,
    subject: str,
    email: str,
    status: str = "active",
) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (
                id, username, email, password_hash, auth_provider,
                account_status, account_kind
            ) VALUES ($1, $2, $3, $4, 'keycloak', $5, 'human')
            """,
            user_id,
            f"wsg-{uuid.uuid4().hex[:12]}",
            email,
            "!keycloak-sso:no-local-login!",
            status,
        )
        await conn.execute(
            """
            INSERT INTO external_identities (user_id, issuer, subject, email_snapshot)
            VALUES ($1, $2, $3, $4)
            """,
            user_id,
            _ISSUER,
            subject,
            email,
        )
    return user_id


async def test_enrollment_mode_defaults_to_open():
    assert type(settings).model_fields["keycloak_enrollment_mode"].default == "open"


async def test_invite_only_rejects_unbound_verified_realm_user(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)
    with pytest.raises(MembershipRequiredError) as exc_info:
        await _resolve_or_provision_keycloak_user(
            _claims("not-invited", "wsg-not-invited@example.com")
        )
    assert exc_info.value.code == "membership_required"


async def test_exact_subject_binding_preserves_user_id_across_email_change(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    old_email = f"wsg-old-{uuid.uuid4().hex[:8]}@example.com"
    new_email = f"wsg-new-{uuid.uuid4().hex[:8]}@example.com"
    user_id = await _insert_bound_user(
        pool,
        subject="email-change",
        email=old_email,
    )
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)

    resolved = await _resolve_or_provision_keycloak_user(
        _claims("email-change", new_email)
    )

    assert resolved["user_id"] == user_id
    assert resolved["newly_provisioned"] is False
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.email, e.email_snapshot
              FROM users u
              JOIN external_identities e ON e.user_id = u.id
             WHERE u.id = $1 AND e.issuer = $2 AND e.subject = $3
            """,
            user_id,
            _ISSUER,
            "email-change",
        )
    assert row["email"] == new_email
    assert row["email_snapshot"] == new_email


async def test_exact_binding_does_not_require_email_claim(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    user_id = await _insert_bound_user(
        pool,
        subject="no-email-needed",
        email=f"wsg-no-email-{uuid.uuid4().hex[:8]}@example.com",
    )
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)

    resolved = await _resolve_or_provision_keycloak_user(_claims("no-email-needed"))
    assert resolved["user_id"] == user_id


async def test_open_mode_backfills_subject_for_existing_keycloak_user(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    user_id = uuid.uuid4()
    email = f"wsg-backfill-{uuid.uuid4().hex[:8]}@example.com"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, auth_provider)
            VALUES ($1, $2, $3, $4, 'keycloak')
            """,
            user_id,
            f"wsg-{uuid.uuid4().hex[:12]}",
            email,
            "!keycloak-sso:no-local-login!",
        )

    resolved = await _resolve_or_provision_keycloak_user(
        _claims("backfill-existing", email)
    )
    assert resolved["user_id"] == user_id
    async with pool.acquire() as conn:
        bound_user_id = await conn.fetchval(
            "SELECT user_id FROM external_identities WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            "backfill-existing",
        )
    assert bound_user_id == user_id


async def test_concurrent_open_mode_backfill_of_same_subject_is_idempotent(
    pool,
    monkeypatch,
):
    from app.services import auth_service

    user_id = uuid.uuid4()
    email = f"wsg-concurrent-backfill-{uuid.uuid4().hex[:8]}@example.com"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, auth_provider)
            VALUES ($1, $2, $3, $4, 'keycloak')
            """,
            user_id,
            f"wsg-{uuid.uuid4().hex[:12]}",
            email,
            "!keycloak-sso:no-local-login!",
        )

    original_lookup = auth_service._bound_external_user
    initial_lookups = 0
    both_initial_lookups_finished = asyncio.Event()

    async def synchronized_initial_lookup(conn, issuer, subject):
        nonlocal initial_lookups
        result = await original_lookup(conn, issuer, subject)
        if initial_lookups < 2:
            initial_lookups += 1
            if initial_lookups == 2:
                both_initial_lookups_finished.set()
            await both_initial_lookups_finished.wait()
        return result

    monkeypatch.setattr(
        auth_service,
        "_bound_external_user",
        synchronized_initial_lookup,
    )
    claims = _claims("concurrent-backfill", email)

    results = await asyncio.gather(
        auth_service._resolve_or_provision_keycloak_user(claims),
        auth_service._resolve_or_provision_keycloak_user(claims),
    )

    assert {result["user_id"] for result in results} == {user_id}
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM external_identities WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            "concurrent-backfill",
        ) == 1


async def test_open_mode_jit_creates_user_and_subject_binding(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"wsg-jit-{uuid.uuid4().hex[:8]}@example.com"
    resolved = await _resolve_or_provision_keycloak_user(_claims("jit-new", email))

    assert resolved["newly_provisioned"] is True
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.account_status, u.account_kind, e.issuer, e.subject
              FROM users u
              JOIN external_identities e ON e.user_id = u.id
             WHERE u.id = $1
            """,
            resolved["user_id"],
        )
    assert row["account_status"] == "active"
    assert row["account_kind"] == "human"
    assert row["issuer"] == _ISSUER
    assert row["subject"] == "jit-new"


async def test_second_subject_cannot_jit_bind_to_an_already_bound_email(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"wsg-collision-{uuid.uuid4().hex[:8]}@example.com"
    await _insert_bound_user(pool, subject="subject-one", email=email)

    with pytest.raises(ExternalIdentityConflictError):
        await _resolve_or_provision_keycloak_user(_claims("subject-two", email))


async def test_suspended_bound_user_is_denied(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    await _insert_bound_user(
        pool,
        subject="suspended",
        email=f"wsg-suspended-{uuid.uuid4().hex[:8]}@example.com",
        status="suspended",
    )
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)

    with pytest.raises(AccountSuspendedError) as exc_info:
        await _resolve_or_provision_keycloak_user(_claims("suspended"))
    assert exc_info.value.code == "account_suspended"


async def test_disabled_mode_rejects_even_a_bound_external_identity(pool, monkeypatch):
    from app.exceptions import ExternalAuthDisabledError
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    await _insert_bound_user(
        pool,
        subject="disabled-mode",
        email=f"wsg-disabled-{uuid.uuid4().hex[:8]}@example.com",
    )
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "disabled", raising=False)

    with pytest.raises(ExternalAuthDisabledError):
        await _resolve_or_provision_keycloak_user(_claims("disabled-mode"))


async def test_exact_identity_resolution_waits_for_suspension_and_denies(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    user_id = await _insert_bound_user(
        pool,
        subject="suspension-race",
        email=f"wsg-suspension-race-{uuid.uuid4().hex[:8]}@example.com",
    )
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow(
                "SELECT id FROM users WHERE id = $1 FOR UPDATE",
                user_id,
            )
            await conn.execute(
                "UPDATE users SET account_status = 'suspended' WHERE id = $1",
                user_id,
            )
            resolve_task = asyncio.create_task(
                _resolve_or_provision_keycloak_user(_claims("suspension-race"))
            )
            await asyncio.sleep(0.05)
            assert not resolve_task.done()

    with pytest.raises(AccountSuspendedError):
        await resolve_task


async def test_external_identity_profile_snapshot_is_not_locally_editable(pool):
    from app.exceptions import ExternalProfileReadOnlyError
    from app.services.auth_service import update_profile

    user_id = await _insert_bound_user(
        pool,
        subject="profile-read-only",
        email=f"wsg-profile-{uuid.uuid4().hex[:8]}@example.com",
    )

    with pytest.raises(ExternalProfileReadOnlyError):
        await update_profile(str(user_id), email="self-asserted@example.com")
    with pytest.raises(ExternalProfileReadOnlyError):
        await update_profile(str(user_id), display_name="Self asserted")
