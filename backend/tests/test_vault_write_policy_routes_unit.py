"""Route-level contract for the vault write-policy admin API (P0 S3, Task 10).

Mirrors ``test_workspace_account_admin_routes.py``'s convention: no DB, no
real service call — each route handler is invoked directly with a
monkeypatched service function so route-level concerns (is_admin gating,
argument forwarding, path/body wiring) are isolated from the DB-backed
service-layer behaviour ``test_vault_write_policy_unit.py`` covers.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.services.auth_service import AuthenticatedUser


pytestmark = pytest.mark.asyncio


def _user(*, admin: bool) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="vwp-caller",
        email="vwp-caller@service.akb.invalid",
        display_name=None,
        is_admin=admin,
        auth_method="pat",
        key_class="service",
    )


# ── non-admin → 403 before the service ever runs ────────────────────────

async def test_non_admin_rejected_for_set_write_policy(monkeypatch):
    from app.api.routes import access

    async def _must_not_run(*_a, **_k):
        raise AssertionError("service must not run for a non-admin")

    monkeypatch.setattr(access, "set_vault_write_policy", _must_not_run)
    req = access.SetVaultWritePolicyRequest(managed_by="collector:acme")

    with pytest.raises(HTTPException) as exc_info:
        await access.admin_set_vault_write_policy("some-vault", req, _user(admin=False))
    assert exc_info.value.status_code == 403


async def test_non_admin_rejected_for_remove_write_policy(monkeypatch):
    from app.api.routes import access

    async def _must_not_run(*_a, **_k):
        raise AssertionError("service must not run for a non-admin")

    monkeypatch.setattr(access, "remove_vault_write_policy", _must_not_run)

    with pytest.raises(HTTPException) as exc_info:
        await access.admin_remove_vault_write_policy("some-vault", _user(admin=False))
    assert exc_info.value.status_code == 403


async def test_non_admin_rejected_for_add_grant(monkeypatch):
    from app.api.routes import access

    async def _must_not_run(*_a, **_k):
        raise AssertionError("service must not run for a non-admin")

    monkeypatch.setattr(access, "add_vault_write_grant", _must_not_run)

    with pytest.raises(HTTPException) as exc_info:
        await access.admin_add_vault_write_grant(
            "some-vault", str(uuid.uuid4()), _user(admin=False),
        )
    assert exc_info.value.status_code == 403


async def test_non_admin_rejected_for_atomic_bootstrap(monkeypatch):
    from app.api.routes import access

    async def _must_not_run(*_a, **_k):
        raise AssertionError("service must not run for a non-admin")

    monkeypatch.setattr(access, "bootstrap_vault_write_policy", _must_not_run)
    req = access.BootstrapVaultWritePolicyRequest(
        managed_by="akb-platform:workspace-a",
        grants=[
            access.BootstrapVaultWriteGrantRequest(token_id=str(uuid.uuid4()))
        ],
    )

    with pytest.raises(HTTPException) as exc_info:
        await access.admin_bootstrap_vault_write_policy(
            "some-vault", req, _user(admin=False),
        )
    assert exc_info.value.status_code == 403


async def test_non_admin_rejected_for_remove_grant(monkeypatch):
    from app.api.routes import access

    async def _must_not_run(*_a, **_k):
        raise AssertionError("service must not run for a non-admin")

    monkeypatch.setattr(access, "remove_vault_write_grant", _must_not_run)

    with pytest.raises(HTTPException) as exc_info:
        await access.admin_remove_vault_write_grant(
            "some-vault", str(uuid.uuid4()), _user(admin=False),
        )
    assert exc_info.value.status_code == 403


# ── admin → forwards to the service with the right arguments ───────────

async def test_admin_set_write_policy_forwards_args(monkeypatch):
    from app.api.routes import access

    observed = {}

    async def _set(actor_id, vault_name, managed_by, note=None):
        observed.update(
            actor_id=actor_id, vault_name=vault_name, managed_by=managed_by, note=note,
        )
        return {"vault": vault_name, "managed_by": managed_by, "note": note, "marked": True}

    monkeypatch.setattr(access, "set_vault_write_policy", _set)
    admin = _user(admin=True)
    req = access.SetVaultWritePolicyRequest(managed_by="collector:acme", note="pilot")

    result = await access.admin_set_vault_write_policy("v1", req, admin)

    assert result["marked"] is True
    assert observed == {
        "actor_id": admin.user_id,
        "vault_name": "v1",
        "managed_by": "collector:acme",
        "note": "pilot",
    }


async def test_admin_remove_write_policy_forwards_args(monkeypatch):
    from app.api.routes import access

    observed = {}

    async def _remove(actor_id, vault_name):
        observed.update(actor_id=actor_id, vault_name=vault_name)
        return {"vault": vault_name, "unmarked": True, "was_marked": True}

    monkeypatch.setattr(access, "remove_vault_write_policy", _remove)
    admin = _user(admin=True)

    result = await access.admin_remove_vault_write_policy("v1", admin)

    assert result["unmarked"] is True
    assert observed == {"actor_id": admin.user_id, "vault_name": "v1"}


async def test_admin_add_grant_forwards_args(monkeypatch):
    from app.api.routes import access

    observed = {}
    token_id = str(uuid.uuid4())

    async def _add(actor_id, vault_name, tid):
        observed.update(actor_id=actor_id, vault_name=vault_name, token_id=tid)
        return {"vault": vault_name, "token_id": tid, "granted": True}

    monkeypatch.setattr(access, "add_vault_write_grant", _add)
    admin = _user(admin=True)

    result = await access.admin_add_vault_write_grant("v1", token_id, admin)

    assert result["granted"] is True
    assert observed == {"actor_id": admin.user_id, "vault_name": "v1", "token_id": token_id}


async def test_admin_add_grant_forwards_action_limit(monkeypatch):
    from app.api.routes import access

    observed = {}
    token_id = str(uuid.uuid4())

    async def _add(actor_id, vault_name, tid, *, write_actions=None):
        observed.update(
            actor_id=actor_id,
            vault_name=vault_name,
            token_id=tid,
            write_actions=write_actions,
        )
        return {
            "vault": vault_name,
            "token_id": tid,
            "write_actions": write_actions,
            "granted": True,
        }

    monkeypatch.setattr(access, "add_vault_write_grant", _add)
    admin = _user(admin=True)
    req = access.AddVaultWriteGrantRequest(actions=["file_upload"])

    result = await access.admin_add_vault_write_grant(
        "v1", token_id, admin, req=req,
    )

    assert result["write_actions"] == ["file_upload"]
    assert observed == {
        "actor_id": admin.user_id,
        "vault_name": "v1",
        "token_id": token_id,
        "write_actions": ["file_upload"],
    }


async def test_admin_atomic_bootstrap_forwards_complete_grant_set(monkeypatch):
    from app.api.routes import access

    observed = {}
    wildcard_id = str(uuid.uuid4())
    upload_id = str(uuid.uuid4())

    async def _bootstrap(actor_id, vault_name, managed_by, grants, note=None):
        observed.update(
            actor_id=actor_id,
            vault_name=vault_name,
            managed_by=managed_by,
            grants=grants,
            note=note,
        )
        return {"vault": vault_name, "marked": True, "grants": grants}

    monkeypatch.setattr(access, "bootstrap_vault_write_policy", _bootstrap)
    admin = _user(admin=True)
    req = access.BootstrapVaultWritePolicyRequest(
        managed_by="akb-platform:workspace-a",
        note="initial managed cutover",
        grants=[
            access.BootstrapVaultWriteGrantRequest(token_id=wildcard_id),
            access.BootstrapVaultWriteGrantRequest(
                token_id=upload_id,
                actions=["file_upload"],
            ),
        ],
    )

    result = await access.admin_bootstrap_vault_write_policy("v1", req, admin)

    assert result["marked"] is True
    assert observed == {
        "actor_id": admin.user_id,
        "vault_name": "v1",
        "managed_by": "akb-platform:workspace-a",
        "grants": [
            {"token_id": wildcard_id, "write_actions": None},
            {"token_id": upload_id, "write_actions": ["file_upload"]},
        ],
        "note": "initial managed cutover",
    }


async def test_admin_remove_grant_forwards_args(monkeypatch):
    from app.api.routes import access

    observed = {}
    token_id = str(uuid.uuid4())

    async def _remove(actor_id, vault_name, tid):
        observed.update(actor_id=actor_id, vault_name=vault_name, token_id=tid)
        return {"vault": vault_name, "token_id": tid, "revoked": True}

    monkeypatch.setattr(access, "remove_vault_write_grant", _remove)
    admin = _user(admin=True)

    result = await access.admin_remove_vault_write_grant("v1", token_id, admin)

    assert result["revoked"] is True
    assert observed == {"actor_id": admin.user_id, "vault_name": "v1", "token_id": token_id}


async def test_write_policy_routes_are_explicit_in_openapi():
    from app.main import app

    paths = app.openapi()["paths"]
    expected = {
        "/api/v1/admin/vaults/{vault}/write-policy",
        "/api/v1/admin/vaults/{vault}/write-policy/bootstrap",
        "/api/v1/admin/vaults/{vault}/write-policy/grants/{token_id}",
    }
    assert expected <= set(paths)
