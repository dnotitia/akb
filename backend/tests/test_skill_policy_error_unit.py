"""Structured policy errors for the vault-skill reserved namespace."""

import json

import pytest

from app.exceptions import (
    RESERVED_SYSTEM_PATH_CODE,
    ForbiddenError,
    ReservedSystemPathError,
)
from app.util.errors import exception_envelope
from app.services.skill_policy import (
    VAULT_SKILL_PATH,
    check_collection_create,
    check_collection_delete,
    check_delete,
    check_move,
    check_put,
    check_resource_collection,
    check_update,
)


def assert_reserved(callable_):
    with pytest.raises(ReservedSystemPathError) as raised:
        callable_()
    error = raised.value
    assert error.status_code == 403
    assert error.code == RESERVED_SYSTEM_PATH_CODE


def test_reserved_document_writes_use_stable_code():
    assert_reserved(lambda: check_put("overview/runbooks", "reference"))
    assert_reserved(lambda: check_put("notes", "skill"))
    assert_reserved(lambda: check_update(VAULT_SKILL_PATH, "reference"))
    assert_reserved(lambda: check_update("notes/custom.md", "skill"))
    assert_reserved(lambda: check_move("notes/a.md", "overview/a.md"))
    assert_reserved(lambda: check_delete(VAULT_SKILL_PATH))


def test_reserved_collection_and_resource_writes_use_stable_code():
    assert_reserved(lambda: check_collection_create("overview/new"))
    assert_reserved(lambda: check_collection_delete("overview"))
    assert_reserved(lambda: check_resource_collection("overview"))


def test_canonical_skill_owner_denial_remains_acl_for_callers():
    with pytest.raises(ForbiddenError) as raised:
        check_update(VAULT_SKILL_PATH, None)
    assert not isinstance(raised.value, ReservedSystemPathError)
    assert raised.value.code is None


def test_mcp_error_envelope_preserves_reserved_code():
    error = ReservedSystemPathError("reserved")

    assert exception_envelope(error) == {
        "error": "reserved",
        "code": RESERVED_SYSTEM_PATH_CODE,
    }


@pytest.mark.asyncio
async def test_http_error_envelope_preserves_reserved_code(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.main import akb_error_handler

    response = await akb_error_handler(None, ReservedSystemPathError("reserved"))
    payload = json.loads(response.body)

    assert response.status_code == 403
    assert payload["code"] == RESERVED_SYSTEM_PATH_CODE
    assert payload["detail"]["code"] == RESERVED_SYSTEM_PATH_CODE
