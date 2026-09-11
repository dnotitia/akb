"""Authority-domain adopt/provision on the browser path (dnotitia/akb#529).

Runs against a real PostgreSQL: the object under test is an exact-binding
write racing a unique constraint, four guard refusals, and an event row —
none of which a mock would honestly exercise.

The authority map is set through the validated ``Settings`` field (not a
raw monkeypatched dict) so a malformed map fails the same way it does at
config load. ``provider_alias`` is passed explicitly, exactly as the
browser callback does after verifying it against both tokens.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

from app.config import settings
from app.exceptions import (
    AccountSuspendedError,
    AuthenticationError,
    ExternalIdentityConflictError,
    MembershipRequiredError,
)


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_ISSUER = "https://id.example.com/realms/akb"
_ALIAS = "workforce"
_DOMAIN = "corp.example"


@pytest.fixture
async def pool(monkeypatch):
    try:
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3)
    except Exception:
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    from app.db.postgres import _load_migration
    from app.services import auth_service

    for filename in (
        "043_workspace_account_governance.py",
        "071_recovery_admin.py",
        "082_pending_admissions.py",
    ):
        migration = _load_migration(filename)
        assert migration is not None
        async with pool.acquire() as conn:
            await migration.migrate(conn=conn)

    async def _get_pool():
        return pool

    monkeypatch.setattr(auth_service, "get_pool", _get_pool)
    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "invite_only", raising=False)
    monkeypatch.setattr(settings, "keycloak_require_verified_email", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_link_by_email", False, raising=False)
    monkeypatch.setattr(
        settings,
        "keycloak_authoritative_email_domains_by_provider",
        {_ALIAS: [_DOMAIN]},
        raising=False,
    )
    yield pool

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE username LIKE 'auth-%'")
        await conn.execute("DELETE FROM events WHERE actor_id LIKE 'auth-%'")
    await pool.close()


def _claims(subject: str, email: str | None = None, *, verified: bool = True) -> dict:
    claims: dict[str, object] = {
        "iss": _ISSUER,
        "sub": subject,
        "preferred_username": f"auth-{subject}",
    }
    if email is not None:
        claims.update({"email": email, "email_verified": verified})
    return claims


async def _insert_unbound_user(
    pool,
    email: str,
    *,
    status: str = "active",
    kind: str = "human",
    provider: str = "local",
    recovery: bool = False,
    admin: bool = False,
) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await _insert_unbound_user_on(
            conn, email, status=status, kind=kind, provider=provider, recovery=recovery, admin=admin
        )


async def _insert_unbound_user_on(
    conn,
    email: str,
    *,
    status: str = "active",
    kind: str = "human",
    provider: str = "local",
    recovery: bool = False,
    admin: bool = False,
) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO users (
            id, username, email, password_hash, auth_provider,
            account_status, account_kind, is_admin, is_recovery_admin
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        """,
        user_id,
        f"auth-{uuid.uuid4().hex[:12]}",
        email,
        "!keycloak-sso:no-local-login!",
        provider,
        status,
        kind,
        admin,
        recovery,
    )
    return user_id


async def _binding(pool, subject: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT user_id, issuer, subject, email_snapshot FROM external_identities"
            " WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            subject,
        )


async def _events(pool, actor: str, kind: str):
    import json

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload FROM events WHERE actor_id = $1 AND kind = $2",
            actor,
            kind,
        )
    return [json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"] for r in rows]


# ── the happy paths ───────────────────────────────────────────────────


async def test_invite_only_adopts_on_declared_domain(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-adopt-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    user_id = await _insert_unbound_user(pool, email)

    resolved = await _resolve_or_provision_keycloak_user(
        _claims(f"adopt-{uuid.uuid4().hex}", email), provider_alias=_ALIAS
    )

    assert resolved["user_id"] == user_id
    assert resolved["newly_provisioned"] is False
    async with pool.acquire() as conn:
        bound = await conn.fetchrow(
            "SELECT issuer, subject FROM external_identities WHERE user_id = $1",
            user_id,
        )
        account = await conn.fetchrow(
            "SELECT auth_provider FROM users WHERE id = $1", user_id
        )
    assert bound is not None and bound["issuer"] == _ISSUER
    assert account["auth_provider"] == "keycloak"
    adopted = await _events(pool, str(user_id), "auth.user_adopted")
    assert len(adopted) == 1
    payload = adopted[0]
    assert payload["domain"] == _DOMAIN
    assert payload["is_admin"] is False
    assert payload["prior_auth_provider"] == "local"
    assert payload["email"] == email
    assert payload["issuer"] == _ISSUER
    assert payload["subject"]
    # The note invite_only took on an earlier refusal is answered by the
    # binding, like every other exact-binding writer (covered directly by
    # test_adopt_clears_a_prior_pending_admission below).


async def test_adopt_clears_a_prior_pending_admission(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-note-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    user_id = await _insert_unbound_user(pool, email)
    subject = f"note-{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pending_admissions (issuer, subject, email) VALUES ($1, $2, $3)",
            _ISSUER,
            subject,
            email,
        )

    resolved = await _resolve_or_provision_keycloak_user(
        _claims(subject, email), provider_alias=_ALIAS
    )

    assert resolved["user_id"] == user_id
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM pending_admissions WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            subject,
        ) == 0


async def test_invite_only_provisions_unknown_address_on_declared_domain(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-new-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    subject = f"provision-{uuid.uuid4().hex}"

    resolved = await _resolve_or_provision_keycloak_user(
        _claims(subject, email), provider_alias=_ALIAS
    )

    assert resolved["newly_provisioned"] is True
    row = await _binding(pool, subject)
    assert row is not None and row["email_snapshot"] == email
    provisioned = await _events(pool, str(resolved["user_id"]), "auth.user_provisioned")
    assert len(provisioned) == 1
    assert (await _events(pool, str(resolved["user_id"]), "auth.user_adopted")) == []


async def test_failed_adopt_records_the_arrival(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-guard-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    await _insert_unbound_user(pool, email, status="suspended")
    subject = f"guard-{uuid.uuid4().hex}"

    with pytest.raises(AccountSuspendedError):
        await _resolve_or_provision_keycloak_user(
            _claims(subject, email), provider_alias=_ALIAS
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM pending_admissions WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            subject,
        ) == 1


async def test_undeclared_domain_still_refuses_and_records(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    subject = f"refused-{uuid.uuid4().hex}"
    email = f"auth-{uuid.uuid4().hex[:8]}@other.example"

    with pytest.raises(MembershipRequiredError):
        await _resolve_or_provision_keycloak_user(
            _claims(subject, email), provider_alias=_ALIAS
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM pending_admissions WHERE issuer = $1 AND subject = $2",
            _ISSUER,
            subject,
        ) == 1


async def test_wrong_alias_does_not_inherit_authority(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    await _insert_unbound_user(pool, email)

    with pytest.raises(MembershipRequiredError):
        await _resolve_or_provision_keycloak_user(
            _claims(f"no-inherit-{uuid.uuid4().hex}", email), provider_alias="other"
        )


async def test_bearer_path_without_alias_never_adopts(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    await _insert_unbound_user(pool, email)

    with pytest.raises(MembershipRequiredError):
        await _resolve_or_provision_keycloak_user(
            _claims(f"bearer-{uuid.uuid4().hex}", email)
        )


# ── the four guards ───────────────────────────────────────────────────


async def test_unverified_email_is_refused(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    await _insert_unbound_user(pool, email)

    with pytest.raises(AuthenticationError):
        await _resolve_or_provision_keycloak_user(
            _claims(f"unverified-{uuid.uuid4().hex}", email, verified=False),
            provider_alias=_ALIAS,
        )


async def test_suspended_account_is_not_revived(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    await _insert_unbound_user(pool, email, status="suspended")

    with pytest.raises(AccountSuspendedError):
        await _resolve_or_provision_keycloak_user(
            _claims(f"suspended-{uuid.uuid4().hex}", email), provider_alias=_ALIAS
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT account_status FROM users WHERE email = $1", email
        ) == "suspended"


async def test_target_already_bound_for_this_issuer_is_refused(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    user_id = await _insert_unbound_user(pool, email)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO external_identities (user_id, issuer, subject, email_snapshot)"
            " VALUES ($1, $2, $3, $4)",
            user_id,
            _ISSUER,
            f"already-bound-{uuid.uuid4().hex}",
            email,
        )

    with pytest.raises(ExternalIdentityConflictError):
        await _resolve_or_provision_keycloak_user(
            _claims(f"second-{uuid.uuid4().hex}", email), provider_alias=_ALIAS
        )


async def test_recovery_admin_is_never_claimable(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    await _insert_unbound_user(pool, email, recovery=True, admin=True)

    with pytest.raises(ExternalIdentityConflictError):
        await _resolve_or_provision_keycloak_user(
            _claims(f"breakglass-{uuid.uuid4().hex}", email), provider_alias=_ALIAS
        )


async def test_non_human_target_is_refused(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    await _insert_unbound_user(pool, email, kind="service", provider="service")

    with pytest.raises(ExternalIdentityConflictError):
        await _resolve_or_provision_keycloak_user(
            _claims(f"service-{uuid.uuid4().hex}", email), provider_alias=_ALIAS
        )


async def test_admin_adopt_is_legible_in_the_event(pool):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-admin-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    user_id = await _insert_unbound_user(pool, email, admin=True)

    resolved = await _resolve_or_provision_keycloak_user(
        _claims(f"admin-{uuid.uuid4().hex}", email), provider_alias=_ALIAS
    )

    assert resolved["user_id"] == user_id
    adopted = await _events(pool, str(user_id), "auth.user_adopted")
    assert len(adopted) == 1
    assert adopted[0]["is_admin"] is True


async def test_pat_resolves_unchanged_across_adoption(pool):
    from app.services import auth_service
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    email = f"auth-pat-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    user_id = await _insert_unbound_user(pool, email)
    issued = await auth_service.create_pat(str(user_id), "pre-adopt-pat")
    raw_token = issued["token"]

    resolved = await _resolve_or_provision_keycloak_user(
        _claims(f"pat-{uuid.uuid4().hex}", email), provider_alias=_ALIAS
    )
    assert resolved["user_id"] == user_id

    authed = await auth_service._resolve_pat(raw_token)
    assert authed is not None
    assert authed.user_id == str(user_id)
    assert authed.auth_method == "pat"


# ── open-mode collision adopts on authority ───────────────────────────


async def test_open_mode_collision_adopts_on_declared_domain(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "open", raising=False)
    email = f"auth-collide-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    user_id = await _insert_unbound_user(pool, email)

    resolved = await _resolve_or_provision_keycloak_user(
        _claims(f"collide-{uuid.uuid4().hex}", email), provider_alias=_ALIAS
    )

    assert resolved["user_id"] == user_id
    assert resolved["newly_provisioned"] is False


async def test_open_mode_collision_off_domain_still_conflicts(pool, monkeypatch):
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "open", raising=False)
    email = f"auth-collide-{uuid.uuid4().hex[:8]}@other.example"
    await _insert_unbound_user(pool, email)

    with pytest.raises(ExternalIdentityConflictError):
        await _resolve_or_provision_keycloak_user(
            _claims(f"collide-{uuid.uuid4().hex}", email), provider_alias=_ALIAS
        )


# ── concurrency ───────────────────────────────────────────────────────


async def test_concurrent_adopts_of_one_address_converge(pool):
    from app.services import auth_service

    email = f"auth-race-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    user_id = await _insert_unbound_user(pool, email)

    first_subject = f"race-a-{uuid.uuid4().hex}"
    second_subject = f"race-b-{uuid.uuid4().hex}"

    # One winner binds; the loser sees the issuer already bound to the
    # target and is refused rather than double-binding. gather without
    # return_exceptions would hide the loser's refusal, so expect it.
    outcomes = await asyncio.gather(
        auth_service._resolve_or_provision_keycloak_user(
            _claims(first_subject, email), provider_alias=_ALIAS
        ),
        auth_service._resolve_or_provision_keycloak_user(
            _claims(second_subject, email), provider_alias=_ALIAS
        ),
        return_exceptions=True,
    )
    winners = [o for o in outcomes if not isinstance(o, BaseException)]
    losers = [o for o in outcomes if isinstance(o, ExternalIdentityConflictError)]
    assert len(winners) == 1 and len(losers) == 1
    assert winners[0]["user_id"] == user_id
    async with pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT COUNT(*) FROM external_identities WHERE user_id = $1", user_id
        ) == 1


async def test_collision_after_provision_insert_adopts_on_authority(pool, monkeypatch):
    """The UniqueViolationError branch re-checks authority before refusing.

    The address is pre-claimed by a winner and the pre-check adopt is forced
    to miss, so the provision INSERT fails on the email constraint while the
    subject is still unbound. The handler must bind the winner's account
    through the authority adopt instead of refusing.
    """
    from app.services import auth_service
    from app.services.auth_service import _resolve_or_provision_keycloak_user

    monkeypatch.setattr(settings, "keycloak_enrollment_mode", "open", raising=False)
    email = f"auth-late-{uuid.uuid4().hex[:8]}@{_DOMAIN}"
    subject = f"late-{uuid.uuid4().hex}"
    winner_id = await _insert_unbound_user(pool, email)

    original_adopt = auth_service._adopt_authoritative_user
    calls = 0

    async def adopt_miss_then_hit(conn, issuer, sub, claims, addr, domain):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None  # pre-check misses; the INSERT then collides
        return await original_adopt(conn, issuer, sub, claims, addr, domain)

    monkeypatch.setattr(auth_service, "_adopt_authoritative_user", adopt_miss_then_hit)

    resolved = await _resolve_or_provision_keycloak_user(
        _claims(subject, email), provider_alias=_ALIAS
    )

    assert calls == 2
    assert resolved["user_id"] == winner_id
    assert resolved["newly_provisioned"] is False
    row = await _binding(pool, subject)
    assert row is not None and row["user_id"] == winner_id
