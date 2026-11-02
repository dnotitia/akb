"""Which realm accounts the residue excludes, read from a real PostgreSQL.

The exclusion is two lookups keyed by the exact ``(issuer, subject)`` pair: an
identity binding in ``external_identities`` and a recorded arrival in
``pending_admissions``. A mock would agree with whatever this file assumed
about issuers, retention, and the shape of those rows, so these run against the
real tables.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
import uuid

import asyncpg
import pytest

from app.config import settings
from app.sso import realm_residue
from app.sso.models import RealmAccount, RealmAccountListing


pytestmark = pytest.mark.asyncio
_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")
_ISSUER = "https://residue.example.test/realms/akb"
_OTHER_ISSUER = "https://upstream.example.test/realms/workforce"


@pytest.fixture
async def pool(monkeypatch):
    try:
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=3)
    except Exception:
        pytest.skip("Postgres unreachable at AKB_TEST_DSN")

    from app.db.postgres import _load_migration

    async with pool.acquire() as conn:
        for filename in (
            "043_workspace_account_governance.py",
            "082_pending_admissions.py",
        ):
            migration = _load_migration(filename)
            assert migration is not None
            await migration.migrate(conn=conn)

    async def _get_pool():
        return pool

    monkeypatch.setattr(realm_residue, "get_pool", _get_pool)
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(settings, "keycloak_enabled", True, raising=False)
    monkeypatch.setattr(settings, "keycloak_server_url", "https://residue.example.test", raising=False)
    monkeypatch.setattr(settings, "keycloak_realm", "akb", raising=False)
    monkeypatch.setattr(settings, "keycloak_pending_admission_retention_hours", 24, raising=False)
    assert settings.keycloak_issuer == _ISSUER

    yield pool

    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM pending_admissions WHERE issuer = ANY($1::text[])",
            [_ISSUER, _OTHER_ISSUER],
        )
        await conn.execute("DELETE FROM users WHERE username LIKE 'residue-%'")
    await pool.close()


async def _bind(pool, issuer: str, subject: str) -> None:
    user_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, email, password_hash, display_name,
                               auth_provider, is_admin, account_status, account_kind)
            VALUES ($1, $2, $3, '!sso', 'Residue Fixture', 'keycloak',
                    false, 'active', 'human')
            """,
            user_id,
            f"residue-{user_id.hex[:12]}",
            f"residue-{user_id.hex[:12]}@example.com",
        )
        await conn.execute(
            """
            INSERT INTO external_identities (user_id, issuer, subject, email_snapshot)
            VALUES ($1, $2, $3, NULL)
            """,
            user_id,
            issuer,
            subject,
        )


async def _arrive(pool, issuer: str, subject: str, *, hours_ago: int = 0) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO pending_admissions (issuer, subject, email, display_name,
                                            first_seen_at, last_seen_at)
            VALUES ($1, $2, NULL, NULL,
                    NOW() - make_interval(hours => $3),
                    NOW() - make_interval(hours => $3))
            RETURNING id
            """,
            issuer,
            subject,
            hours_ago,
        )


class _Control:
    control_mode = "direct"

    def __init__(self, subjects: list[str]) -> None:
        self.config = type("Config", (), {"broker_issuer": _ISSUER})()
        self._listing = RealmAccountListing(
            accounts=tuple(
                RealmAccount(
                    user_id=subject,
                    username=f"name-{index}",
                    created_at=datetime(2026, 9, index + 1, tzinfo=timezone.utc),
                )
                for index, subject in enumerate(subjects)
            ),
            truncated=False,
        )

    async def list_realm_accounts(self, *, limit: int) -> RealmAccountListing:
        return self._listing


async def test_bindings_and_recorded_arrivals_are_excluded_by_exact_issuer_and_subject(pool):
    tag = uuid.uuid4().hex[:8]
    bound = f"bound-{tag}"
    pending = f"pending-{tag}"
    stale = f"stale-{tag}"
    other_issuer = f"other-issuer-{tag}"
    untouched = f"untouched-{tag}"

    await _bind(pool, _ISSUER, bound)
    pending_id = await _arrive(pool, _ISSUER, pending, hours_ago=1)
    # Past retention: no longer listed to an administrator, about to be
    # evicted, and therefore not a recorded arrival anyone can act on.
    await _arrive(pool, _ISSUER, stale, hours_ago=48)
    # The same subject bound and recorded under ANOTHER issuer is a different
    # identity. Matching on the subject alone would hide this account.
    await _bind(pool, _OTHER_ISSUER, other_issuer)
    await _arrive(pool, _OTHER_ISSUER, other_issuer)

    payload = await realm_residue.realm_account_residue(
        _Control([bound, pending, stale, other_issuer, untouched])
    )

    assert payload["state"] == "available"
    assert payload["realm_account_count"] == 5
    assert payload["bound_count"] == 1
    assert payload["pending_arrival_count"] == 1
    assert payload["residue_count"] == 3
    listed = {item["realm_user_id"]: item["arrival"] for item in payload["accounts"]}
    assert bound not in listed
    assert listed[stale] is None
    assert listed[other_issuer] is None
    assert listed[untouched] is None
    assert listed[pending]["admission_id"] == str(pending_id)
    # Residue first: the pending arrival is the last item.
    assert payload["accounts"][-1]["realm_user_id"] == pending


async def test_the_read_writes_nothing(pool):
    tag = uuid.uuid4().hex[:8]
    bound = f"bound-{tag}"
    pending = f"pending-{tag}"
    await _bind(pool, _ISSUER, bound)
    await _arrive(pool, _ISSUER, pending, hours_ago=1)

    async def snapshot():
        async with pool.acquire() as conn:
            return (
                await conn.fetch(
                    "SELECT * FROM external_identities WHERE issuer = $1 ORDER BY subject",
                    _ISSUER,
                ),
                await conn.fetch(
                    "SELECT * FROM pending_admissions WHERE issuer = $1 ORDER BY subject",
                    _ISSUER,
                ),
            )

    before = await snapshot()
    await realm_residue.realm_account_residue(_Control([bound, pending, f"other-{tag}"]))
    assert await snapshot() == before
