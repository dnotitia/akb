"""Source-local contracts for app identity carrier separation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.config import settings
from app.exceptions import AKBError
from app.services import app_identity_service, auth_service


def _configure_secrets(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "jwt_secret",
        "user-session-signing-material-long-enough",
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "app_token_secret",
        "separate-app-signing-material-long-enough",
        raising=False,
    )
    monkeypatch.setattr(settings, "app_token_ttl_seconds", 300, raising=False)


def test_app_credential_generation_keeps_only_non_secret_metadata():
    raw, proof_hash, prefix = app_identity_service.generate_app_credential()

    assert raw.startswith("akb_app_")
    assert prefix == raw[:16]
    assert len(proof_hash) == 64
    assert raw not in proof_hash
    assert prefix != raw


def test_app_token_contains_identity_not_registry_authority(monkeypatch):
    _configure_secrets(monkeypatch)
    app_id = uuid.uuid4()
    credential_id = uuid.uuid4()

    raw, expires_at, token_id = app_identity_service._create_app_token(
        app_id=app_id,
        credential_id=credential_id,
        generation=7,
    )
    claims = app_identity_service.decode_app_token(raw)

    assert claims is not None
    assert claims["sub"] == str(app_id)
    assert claims["cid"] == str(credential_id)
    assert claims["gen"] == 7
    assert claims["jti"] == token_id
    assert expires_at > datetime.now(timezone.utc)
    assert {
        "vault",
        "vault_id",
        "installation_id",
        "capability",
        "capabilities",
        "grant_generation",
        "resource",
    }.isdisjoint(claims)


def test_matching_user_and_app_signing_secrets_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", "same-signing-material", raising=False)
    monkeypatch.setattr(settings, "app_token_secret", "same-signing-material", raising=False)

    with pytest.raises(AKBError) as exc:
        app_identity_service._create_app_token(
            app_id=uuid.uuid4(),
            credential_id=uuid.uuid4(),
            generation=1,
        )
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_explicit_app_token_type_never_reaches_user_lookup(monkeypatch):
    _configure_secrets(monkeypatch)
    now = datetime.now(timezone.utc)
    app_shaped = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        settings.jwt_secret,
        algorithm="HS256",
        headers={"typ": app_identity_service.APP_TOKEN_TYPE},
    )

    async def forbidden_pool():
        raise AssertionError("app carrier must be rejected before a user lookup")

    monkeypatch.setattr(auth_service, "get_pool", forbidden_pool)

    assert await auth_service.resolve_token(f"Bearer {app_shaped}") is None
    assert await auth_service.resolve_akb_session_authorization(
        f"Bearer {app_shaped}"
    ) is None


def test_supported_capabilities_are_control_plane_only():
    assert app_identity_service.SUPPORTED_APP_CAPABILITIES == {
        "installation:read",
        "inventory:read",
        "rollout:read",
        "rollout:request",
    }
    forbidden_fragments = {"document", "table", "sql", "imperson"}
    assert all(
        not any(fragment in capability for fragment in forbidden_fragments)
        for capability in app_identity_service.SUPPORTED_APP_CAPABILITIES
    )
