"""Case-variant email pre-registration is refused (#551).

The authoritative-adoption lookup matches on `lower(email)`, so a case
variant registered through local signup makes that lookup find two rows and
refuse with `identity_conflict` — blocking the victim's sign-in until an
admin approves them. The fix is two layers: `register()` compares emails
case-insensitively (friendly 409), and migration 109's UNIQUE index on
`lower(email)` is the race-proof backstop.

No live DB — the pool is patched, so what is asserted is the duplicate-check
statement (which is where a variant slips through) plus the migration's
registration in the runtime applier list.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.exceptions import ConflictError
from app.services import auth_service


def _patch_pool(monkeypatch, *, existing=None):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=existing)
    conn.fetchval = AsyncMock(return_value=False)
    conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    monkeypatch.setattr(
        "app.services.auth_service.get_pool", AsyncMock(return_value=pool)
    )
    monkeypatch.setattr(
        "app.services.auth_service.hash_password_async", AsyncMock(return_value="x")
    )
    return conn


async def test_register_refuses_case_variant_email(monkeypatch):
    # Victim holds Victim@corp.example; the attacker registers the lower-case
    # variant. Exact-match check would miss it; lower() must not.
    conn = _patch_pool(monkeypatch, existing={"id": "victim-id"})

    with pytest.raises(ConflictError):
        await auth_service.register("attacker", "victim@corp.example", "pw12345678", None)

    statement = " ".join(conn.fetchrow.await_args_list[-1].args[0].split())
    assert "lower(email) = lower($2)" in statement


async def test_register_still_accepts_unrelated_email(monkeypatch):
    conn = _patch_pool(monkeypatch, existing=None)
    monkeypatch.setattr(
        "app.services.auth_service.get_role_sync",
        lambda: MagicMock(on_user_create=AsyncMock()),
    )

    out = await auth_service.register("newbie", "newbie@corp.example", "pw12345678", None)

    assert out["email"] == "newbie@corp.example"
    statement = " ".join(conn.fetchrow.await_args_list[-1].args[0].split())
    assert "lower(email) = lower($2)" in statement


def test_migration_109_is_registered_in_the_runtime_applier():
    # Guard pattern from test_migrations_registered_unit: a migration file on
    # disk that is not in _apply_migrations() silently never runs.
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    assert (backend / "app" / "db" / "migrations" / "109_users_email_lower_uniq.py").exists()
    src = (backend / "app" / "db" / "postgres.py").read_text(encoding="utf-8")
    assert '"109_users_email_lower_uniq.py"' in src


def test_migration_109_creates_lower_unique_index():
    src = (
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "app" / "db" / "migrations" / "109_users_email_lower_uniq.py"
    ).read_text(encoding="utf-8")
    assert "lower(email)" in src
    assert "CREATE UNIQUE INDEX" in src
