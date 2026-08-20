"""Admin-bypass ownership semantics: transfer + vault_info role truthfulness.

Trigger case (2026-07-16, seahorse-pipeline console-e2e vault repair):

- ``akb_transfer_ownership`` initiated by a system admin who is not the
  literal owner always failed with the misleading conflict ``owner_id moved
  during transfer`` — ``check_vault_access(required_role="owner")`` passes
  via the admin bypass, but the row-lock staleness re-check assumed the
  caller IS the owner.
- ``get_vault_info`` reported ``role: "owner"`` to admins on vaults they do
  not own (the bypass labelled itself ``role_source: "member"``), while
  ``list_accessible_vaults`` reports ``"admin"`` for the same caller/vault —
  consumers gating on ``role == "owner"`` (ownership-transfer UIs, the
  pipeline's Pattern 35 target-vault gate) were lied to.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import access_service

OWNER_ID = uuid.UUID("00000000-0000-0000-0000-00000000aaaa")
ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-00000000bbbb")
NEW_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-00000000cccc")
VAULT_ID = uuid.UUID("00000000-0000-0000-0000-00000000dddd")


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Answers the exact queries transfer_ownership runs, records writes."""

    def __init__(self, *, vault_owner: uuid.UUID):
        self.vault_owner = vault_owner
        self.executed: list[tuple[str, tuple]] = []

    def transaction(self):
        return _FakeTransaction()

    async def fetchrow(self, query: str, *args):
        if "FROM vaults" in query:
            return {"id": VAULT_ID, "owner_id": self.vault_owner}
        if "FROM users" in query:
            return {"id": NEW_OWNER_ID, "username": args[0]}
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def execute(self, query: str, *args):
        self.executed.append((" ".join(query.split()), args))


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


class _RoleSyncRecorder:
    def __init__(self):
        self.grants: list[tuple[uuid.UUID, uuid.UUID, str]] = []

    async def on_grant(self, vault_id, user_id, role):
        self.grants.append((vault_id, user_id, role))


@pytest.fixture
def transfer_env(monkeypatch):
    conn = _FakeConn(vault_owner=OWNER_ID)
    role_sync = _RoleSyncRecorder()
    events: list[str] = []

    async def _get_pool():
        return _FakePool(conn)

    async def _emit_event(_conn, event_type, **_kw):
        events.append(event_type)

    monkeypatch.setattr(access_service, "get_pool", _get_pool)
    monkeypatch.setattr(access_service, "emit_event", _emit_event)
    monkeypatch.setattr(access_service, "get_role_sync", lambda: role_sync)
    return conn, role_sync, events


def _grant_access(monkeypatch, *, role_source: str):
    async def _check(_user_id, _vault_name, required_role="reader", **_kw):
        assert required_role == "owner"
        return {
            "vault_id": VAULT_ID,
            "role": "owner",
            "status": "active",
            "role_source": role_source,
        }

    monkeypatch.setattr(access_service, "check_vault_access", _check)


async def test_admin_initiated_transfer_succeeds_for_unowned_vault(
    monkeypatch, transfer_env
):
    """The exact live failure: admin caller, vault owned by someone else."""
    conn, role_sync, events = transfer_env
    _grant_access(monkeypatch, role_source="system_admin")

    result = await access_service.transfer_ownership(
        str(ADMIN_ID), "fixture-vault", "new-owner"
    )

    assert result["transferred"] is True
    update = next(q for q, _ in conn.executed if q.startswith("UPDATE vaults"))
    assert "SET owner_id" in update
    # The previous literal owner (not the admin caller) is demoted to an
    # admin grant, and role-sync mirrors both memberships.
    assert (VAULT_ID, OWNER_ID, "admin") in role_sync.grants
    assert (VAULT_ID, NEW_OWNER_ID, "admin") in role_sync.grants
    assert events == ["access.transfer_ownership"]


async def test_owner_initiated_transfer_still_detects_lost_race(
    monkeypatch, transfer_env
):
    """A non-admin caller whose ownership moved mid-flight must still conflict."""
    _grant_access(monkeypatch, role_source="member")

    with pytest.raises(access_service.ConflictError, match="owner_id moved"):
        await access_service.transfer_ownership(
            str(ADMIN_ID),  # != vault owner in the locked row
            "fixture-vault",
            "new-owner",
        )


async def test_owner_initiated_transfer_succeeds_when_row_matches(
    monkeypatch, transfer_env
):
    _grant_access(monkeypatch, role_source="member")

    result = await access_service.transfer_ownership(
        str(OWNER_ID), "fixture-vault", "new-owner"
    )

    assert result["transferred"] is True


class _VaultInfoConn(_FakeConn):
    """Extends the transfer fake with the vault_info fan-out queries."""

    def __init__(self, *, vault_owner: uuid.UUID):
        super().__init__(vault_owner=vault_owner)
        self.fetchval_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        if "SELECT * FROM vaults" in query:
            return {
                "id": VAULT_ID,
                "name": args[0],
                "description": "",
                "status": "active",
                "public_access": "none",
                "owner_id": self.vault_owner,
                "created_at": __import__("datetime").datetime(2026, 5, 28),
            }
        if "FROM users" in query:
            return {"username": "real-owner", "display_name": "Real Owner"}
        if "FROM documents" in query:
            return None
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetchval(self, query: str, *args):
        self.fetchval_calls.append((" ".join(query.split()), args))
        return 0


@pytest.fixture
def vault_info_env(monkeypatch):
    conn = _VaultInfoConn(vault_owner=OWNER_ID)

    async def _get_pool():
        return _FakePool(conn)

    async def _get_policy(_vault_id, conn=None):
        return None

    monkeypatch.setattr(access_service, "get_pool", _get_pool)
    monkeypatch.setattr(access_service.write_policy_repo, "get_policy", _get_policy)
    return conn


def _grant_reader_access(monkeypatch, *, role: str, role_source: str):
    async def _check(_user_id, _vault_name, required_role="reader", **_kw):
        return {
            "vault_id": VAULT_ID,
            "role": role,
            "status": "active",
            "role_source": role_source,
        }

    monkeypatch.setattr(access_service, "check_vault_access", _check)


async def test_vault_info_reports_admin_not_owner_for_unowned_vault(
    monkeypatch, vault_info_env
):
    """Admin bypass on someone else's vault must read as access, not ownership."""
    _grant_reader_access(monkeypatch, role="owner", role_source="system_admin")

    info = await access_service.get_vault_info(str(ADMIN_ID), "fixture-vault")

    assert info["role"] == "admin"
    assert info["owner"] == "real-owner"


async def test_vault_info_keeps_owner_role_for_the_literal_owner(
    monkeypatch, vault_info_env
):
    """An admin who literally owns the vault still reads as owner."""
    _grant_reader_access(monkeypatch, role="owner", role_source="system_admin")

    info = await access_service.get_vault_info(str(OWNER_ID), "fixture-vault")

    assert info["role"] == "owner"


async def test_vault_info_edge_count_uses_the_reader_visible_boundary(
    monkeypatch, vault_info_env
):
    _grant_reader_access(monkeypatch, role="reader", role_source="member")

    await access_service.get_vault_info(str(OWNER_ID), "fixture-vault")

    query, args = next(
        (query, args)
        for query, args in vault_info_env.fetchval_calls
        if "FROM edges" in query
    )
    assert "starts_with(source_uri, $2)" in query
    assert "starts_with(target_uri, $2)" in query
    assert args == (VAULT_ID, "akb://fixture-vault/")
