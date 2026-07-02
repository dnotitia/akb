"""Unit coverage for token key_class and coarse scope enforcement."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.exceptions import ValidationError
from app.services.auth_service import (
    AuthenticatedUser,
    generate_pat,
    normalize_token_scopes,
    scopes_from_db,
    token_has_scope,
)


def test_service_key_generation_uses_secret_prefix():
    raw, token_hash, token_prefix = generate_pat("service")

    assert raw.startswith("akb_secret_")
    assert token_prefix == raw[:12]
    assert len(token_hash) == 64


def test_pat_generation_keeps_existing_prefix():
    raw, _token_hash, token_prefix = generate_pat()

    assert raw.startswith("akb_")
    assert not raw.startswith("akb_secret_")
    assert token_prefix == raw[:12]


def test_normalize_token_scopes_defaults_to_read_write():
    assert normalize_token_scopes(None) == ["read", "write"]


def test_normalize_token_scopes_dedupes_without_reordering_first_seen():
    assert normalize_token_scopes(["write", "read", "write"]) == ["write", "read"]


@pytest.mark.parametrize("bad", [[], ["delete"], ["read", ""], ["read", 1]])
def test_normalize_token_scopes_rejects_bad_input(bad):
    with pytest.raises(ValidationError):
        normalize_token_scopes(bad)  # type: ignore[arg-type]


def test_scopes_from_db_none_preserves_legacy_read_write():
    assert scopes_from_db(None) == frozenset({"read", "write"})


def test_token_has_scope_treats_admin_as_super_scope():
    assert token_has_scope(frozenset({"admin"}), "read")
    assert token_has_scope(frozenset({"admin"}), "write")
    assert not token_has_scope(frozenset({"read"}), "write")


def test_rest_dependency_allows_read_scope_on_get(monkeypatch):
    async def fake_resolve(_authorization: str):
        return AuthenticatedUser(
            user_id="11111111-1111-1111-1111-111111111111",
            username="alice",
            email="alice@example.com",
            display_name=None,
            is_admin=False,
            auth_method="pat",
            key_class="pat",
            token_scopes=frozenset({"read"}),
        )

    monkeypatch.setattr(deps, "resolve_token", fake_resolve)

    app = FastAPI()

    @app.get("/read")
    async def read_route(_user: AuthenticatedUser = Depends(deps.get_current_user)):
        return {"ok": True}

    response = TestClient(app).get("/read", headers={"Authorization": "Bearer akb_fixture"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_rest_dependency_rejects_read_scope_on_post(monkeypatch):
    async def fake_resolve(_authorization: str):
        return AuthenticatedUser(
            user_id="11111111-1111-1111-1111-111111111111",
            username="alice",
            email="alice@example.com",
            display_name=None,
            is_admin=False,
            auth_method="pat",
            key_class="pat",
            token_scopes=frozenset({"read"}),
        )

    monkeypatch.setattr(deps, "resolve_token", fake_resolve)

    app = FastAPI()

    @app.post("/write")
    async def write_route(_user: AuthenticatedUser = Depends(deps.get_current_user)):
        return {"ok": True}

    response = TestClient(app).post("/write", headers={"Authorization": "Bearer akb_fixture"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "insufficient_scope"
    assert response.json()["detail"]["required_scope"] == "write"
