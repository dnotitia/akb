"""Focused unit contracts for explicit recovery-admin provisioning."""

from __future__ import annotations

import pytest

from app.config import settings


pytestmark = pytest.mark.asyncio


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class _RegistrationConnection:
    async def fetchrow(self, query, *_args):
        assert "SELECT id FROM users" in query
        return None

    async def fetchval(self, query, *_args):
        # Model PostgreSQL's result for the legacy first-user-wins INSERT.
        return "NOT EXISTS (SELECT 1 FROM users)" in query


class _RoleSync:
    async def on_user_create(self, _user_id):
        return None


async def test_first_local_registration_is_never_implicitly_admin(monkeypatch):
    from app.services import auth_service

    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)

    async def _get_pool():
        return _Pool(_RegistrationConnection())

    async def _hash_password(_password):
        return "bcrypt-hash"

    monkeypatch.setattr(auth_service, "get_pool", _get_pool)
    monkeypatch.setattr(auth_service, "hash_password_async", _hash_password)
    monkeypatch.setattr(auth_service, "get_role_sync", lambda: _RoleSync())

    result = await auth_service.register(
        "ordinary-member",
        "ordinary-member@example.com",
        "operator-supplied-password",
    )

    assert result["is_admin"] is False
