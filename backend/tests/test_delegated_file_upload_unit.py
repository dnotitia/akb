"""Focused unit contract for action-limited delegated file uploads."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import file_write_context
from app.services.auth_service import AuthenticatedUser


pytestmark = pytest.mark.asyncio


def _request(delegated_authorization: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if delegated_authorization is not None:
        headers.append(
            (b"x-akb-delegated-authorization", delegated_authorization.encode())
        )
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def _user(
    *,
    key_class: str | None,
    auth_method: str = "pat",
    token_id: str | None = None,
    username: str = "naut-upload",
) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username=username,
        email=f"{username}@example.test",
        display_name=None,
        is_admin=False,
        auth_method=auth_method,
        token_id=token_id,
        key_class=key_class,
        token_scopes=frozenset({"write"}) if key_class else None,
    )


async def test_write_action_normalization_is_small_deterministic_and_fail_closed():
    from app.services.access_service import normalize_write_actions

    assert normalize_write_actions(None) == ("*",)
    assert normalize_write_actions(["file_upload", "file_upload"]) == ("file_upload",)
    with pytest.raises(ValueError):
        normalize_write_actions([])
    with pytest.raises(ValueError):
        normalize_write_actions(["*", "file_upload"])
    with pytest.raises(ValueError):
        normalize_write_actions(["document_write"])


async def test_secondary_resolver_accepts_only_akb_session_jwt_without_mutating_primary_context(
    monkeypatch,
):
    from app.models.vault_scope import current_key_class, current_token_id
    from app.services import auth_service

    delegated = _user(key_class=None, auth_method="jwt", username="alice")
    observed: list[str] = []

    async def _resolve_session(raw: str):
        observed.append(raw)
        return delegated

    monkeypatch.setattr(auth_service, "_resolve_akb_session_jwt", _resolve_session)
    raw = jwt.encode(
        {"sub": delegated.user_id},
        "unit-secret-at-least-32-bytes-long",
        algorithm="HS256",
    )
    token_marker = current_token_id.set("primary-service-token-id")
    class_marker = current_key_class.set("service")
    try:
        resolved = await auth_service.resolve_akb_session_authorization(f"Bearer {raw}")
        assert resolved is delegated
        assert observed == [raw]
        assert current_token_id.get() == "primary-service-token-id"
        assert current_key_class.get() == "service"
        assert await auth_service.resolve_akb_session_authorization("Bearer akb_pat") is None
    finally:
        current_key_class.reset(class_marker)
        current_token_id.reset(token_marker)


async def test_delegated_actor_requires_service_key_and_active_session(monkeypatch):
    from app.api import deps

    service = _user(key_class="service", token_id=str(uuid.uuid4()))
    delegated = _user(key_class=None, auth_method="jwt", username="alice")

    async def _resolve(_authorization: str):
        return delegated

    monkeypatch.setattr(deps, "resolve_akb_session_authorization", _resolve)
    actor = await deps.require_delegated_actor(
        _request("Bearer user.jwt"), service,
    )
    assert actor.user is delegated
    assert actor.service_user_id == service.user_id
    assert actor.service_token_id == service.token_id

    with pytest.raises(HTTPException) as missing:
        await deps.require_delegated_actor(_request(), service)
    assert missing.value.detail["code"] == "delegated_authorization_required"

    with pytest.raises(HTTPException) as wrong_primary:
        await deps.require_delegated_actor(
            _request("Bearer user.jwt"),
            _user(key_class="pat", token_id=str(uuid.uuid4())),
        )
    assert wrong_primary.value.detail["code"] == "delegation_requires_service_key"

    async def _reject(_authorization: str):
        return None

    monkeypatch.setattr(deps, "resolve_akb_session_authorization", _reject)
    with pytest.raises(HTTPException) as invalid:
        await deps.require_delegated_actor(_request("Bearer stale.jwt"), service)
    assert invalid.value.detail["code"] == "invalid_delegated_authorization"

    async def _resolve_pat(_authorization: str):
        return _user(
            key_class="pat",
            auth_method="pat",
            token_id=str(uuid.uuid4()),
            username="not-a-session",
        )

    monkeypatch.setattr(deps, "resolve_akb_session_authorization", _resolve_pat)
    with pytest.raises(HTTPException) as delegated_pat:
        await deps.require_delegated_actor(_request("Bearer akb_pat"), service)
    assert delegated_pat.value.detail["code"] == "invalid_delegated_authorization"


async def test_action_limited_upload_uses_human_actor_and_service_audit_metadata(
    monkeypatch,
):
    from app.api.routes import files

    service = _user(key_class="service", token_id=str(uuid.uuid4()))
    human = _user(key_class=None, auth_method="jwt", username="alice")
    observed: dict = {}

    async def _check_service(user_id, vault, required_role, *, write_action=None):
        observed["service_check"] = (user_id, vault, required_role, write_action)
        return {
            "vault_id": uuid.uuid4(),
            "role": "writer",
            "role_source": "write_policy_grant",
            "write_grant_actions": ["file_upload"],
        }

    async def _delegated_actor(_request, primary):
        assert primary is service
        return SimpleNamespace(
            user=human,
            service_user_id=service.user_id,
            service_token_id=service.token_id,
        )

    async def _check_human(user_id, vault):
        observed["human_check"] = (user_id, vault)
        return {"role": "writer"}

    async def _initiate(**kwargs):
        observed["initiate"] = kwargs
        return {"uri": "akb://team/file/f-1"}

    monkeypatch.setattr(file_write_context, "check_vault_access", _check_service)
    monkeypatch.setattr(file_write_context, "require_delegated_actor", _delegated_actor)
    monkeypatch.setattr(file_write_context, "check_delegated_vault_writer", _check_human)
    monkeypatch.setattr(files.file_service, "initiate_upload", _initiate)

    result = await files.upload_file(
        request=_request("Bearer user.jwt"),
        vault="team",
        filename="diagram.png",
        collection="notes",
        description="diagram",
        mime_type="image/png",
        user=service,
    )

    assert result["uri"] == "akb://team/file/f-1"
    assert observed["service_check"] == (
        service.user_id,
        "team",
        "writer",
        "file_upload",
    )
    assert observed["human_check"] == (human.user_id, "team")
    assert observed["initiate"]["actor_id"] == human.username


async def test_action_limited_confirm_emits_both_actor_ids(monkeypatch):
    from app.api.routes import files

    service = _user(key_class="service", token_id=str(uuid.uuid4()))
    human = _user(key_class=None, auth_method="jwt", username="alice")
    observed: dict = {}

    async def _check_service(*_args, **_kwargs):
        return {
            "vault_id": uuid.uuid4(),
            "role": "writer",
            "role_source": "write_policy_grant",
            "write_grant_actions": ["file_upload"],
        }

    async def _delegated_actor(*_args):
        return SimpleNamespace(
            user=human,
            service_user_id=service.user_id,
            service_token_id=service.token_id,
        )

    async def _check_human(*_args):
        return {"role": "writer"}

    async def _confirm(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return {"uri": "akb://team/file/f-1"}

    monkeypatch.setattr(file_write_context, "check_vault_access", _check_service)
    monkeypatch.setattr(file_write_context, "require_delegated_actor", _delegated_actor)
    monkeypatch.setattr(file_write_context, "check_delegated_vault_writer", _check_human)
    monkeypatch.setattr(files.file_service, "confirm_upload", _confirm)

    await files.confirm_upload(
        request=_request("Bearer user.jwt"),
        vault="team",
        file_id="f-1",
        content_hash=None,
        hash_algorithm="sha256",
        user=service,
    )

    assert observed["kwargs"]["actor_id"] == human.username
    assert observed["kwargs"]["delegated_actor"] == {
        "delegated_user_id": human.user_id,
        "service_user_id": service.user_id,
        "service_token_id": service.token_id,
    }


async def test_file_event_flattens_only_bounded_delegation_ids():
    from app.services.file_service import _delegated_actor_event_fields

    fields = _delegated_actor_event_fields(
        {
            "delegated_user_id": "human-id",
            "service_user_id": "service-id",
            "service_token_id": "token-id",
            "authorization": "Bearer must-not-escape",
        }
    )

    assert fields == {
        "delegated_user_id": "human-id",
        "service_user_id": "service-id",
        "service_token_id": "token-id",
    }
    assert "delegated_actor" not in fields
    assert "authorization" not in fields


async def test_wildcard_grant_keeps_existing_non_delegated_file_behavior(monkeypatch):
    from app.api.routes import files

    service = _user(key_class="service", token_id=str(uuid.uuid4()))
    observed: dict = {}

    async def _check_service(*_args, **_kwargs):
        return {
            "vault_id": uuid.uuid4(),
            "role": "writer",
            "role_source": "write_policy_grant",
            "write_grant_actions": ["*"],
        }

    async def _must_not_delegate(*_args):
        raise AssertionError("wildcard compatibility path must not require delegation")

    async def _initiate(**kwargs):
        observed.update(kwargs)
        return {"uri": "akb://team/file/f-2"}

    monkeypatch.setattr(file_write_context, "check_vault_access", _check_service)
    monkeypatch.setattr(file_write_context, "require_delegated_actor", _must_not_delegate)
    monkeypatch.setattr(files.file_service, "initiate_upload", _initiate)

    await files.upload_file(
        request=_request(),
        vault="team",
        filename="collector.bin",
        collection="",
        description="",
        mime_type="application/octet-stream",
        user=service,
    )
    assert observed["actor_id"] == service.username
