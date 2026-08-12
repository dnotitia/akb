"""Focused contract tests for PAT authority self-verification."""

from __future__ import annotations

import uuid

import pytest

from app.api.routes import auth
from app.exceptions import AuthenticationError, ForbiddenError
from app.models.vault_scope import VaultScope
from app.services.auth_service import AuthenticatedUser


TOKEN_ID = "11111111-1111-1111-1111-111111111111"
EXPECTED_SCOPE = {
    "prefixes": ["automation-"],
    "extra_vaults": ["source-knowledge"],
}


def _user(
    *,
    auth_method: str = "pat",
    token_id: str | None = TOKEN_ID,
    token_scopes: frozenset[str] | None = frozenset({"write"}),
    vault_scope: VaultScope | None = VaultScope.parse_input(EXPECTED_SCOPE),
) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=str(uuid.uuid4()),
        username="managed-client",
        email="managed-client@example.test",
        display_name=None,
        is_admin=False,
        auth_method=auth_method,
        vault_scope=vault_scope,
        token_id=token_id,
        key_class="service" if auth_method == "pat" else None,
        token_scopes=token_scopes,
    )


def _request(
    *,
    scope: dict[str, list[str]] | None = None,
    writer_authorities: list[dict[str, str]] | None = None,
) -> auth.VerifyAuthorityRequest:
    return auth.VerifyAuthorityRequest(
        vault_scope=scope or EXPECTED_SCOPE,
        writer_authorities=writer_authorities or [{"vault": "source-knowledge", "action": "*"}],
    )


@pytest.mark.asyncio
async def test_verify_authority_returns_only_canonical_exact_authority(monkeypatch):
    calls: list[tuple[str, str, str, str | None]] = []

    async def fake_check(
        user_id: str,
        vault: str,
        required_role: str = "reader",
        *,
        write_action: str | None = None,
    ):
        calls.append((user_id, vault, required_role, write_action))
        return {"vault_id": uuid.uuid4(), "role": "owner", "role_source": "member"}

    monkeypatch.setattr(auth, "check_vault_access", fake_check)
    user = _user()

    result = await auth.verify_authority(_request(), user)

    assert result.model_dump(mode="json") == {
        "token_id": TOKEN_ID,
        "vault_scope": EXPECTED_SCOPE,
        "authorities": [{"vault": "source-knowledge", "role": "writer", "action": "*"}],
    }
    assert calls == [(user.user_id, "source-knowledge", "writer", "*")]


@pytest.mark.asyncio
async def test_verify_authority_passes_action_limited_authority(monkeypatch):
    observed: list[str | None] = []

    async def fake_check(*_args, write_action=None, **_kwargs):
        observed.append(write_action)
        return {"vault_id": uuid.uuid4(), "role": "writer", "role_source": "member"}

    monkeypatch.setattr(auth, "check_vault_access", fake_check)

    result = await auth.verify_authority(
        _request(writer_authorities=[{"vault": "source-knowledge", "action": "file_upload"}]),
        _user(),
    )

    assert observed == ["file_upload"]
    assert result.authorities[0].action == "file_upload"


def test_verify_authority_rejects_unknown_write_action():
    with pytest.raises(ValueError, match="unknown write action"):
        _request(writer_authorities=[{"vault": "source-knowledge", "action": "document_write"}])


@pytest.mark.asyncio
async def test_verify_authority_rejects_non_pat_before_access_check(monkeypatch):
    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("access check must not run")

    monkeypatch.setattr(auth, "check_vault_access", must_not_run)
    with pytest.raises(AuthenticationError):
        await auth.verify_authority(
            _request(),
            _user(auth_method="jwt", token_id=None, vault_scope=None),
        )


def test_verify_authority_caps_database_backed_authority_checks():
    with pytest.raises(ValueError, match="at most 32"):
        _request(writer_authorities=[{"vault": f"source-{index}", "action": "*"} for index in range(33)])


@pytest.mark.asyncio
async def test_verify_authority_rejects_missing_coarse_write_scope(monkeypatch):
    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("access check must not run")

    monkeypatch.setattr(auth, "check_vault_access", must_not_run)
    with pytest.raises(ForbiddenError, match="coarse write scope"):
        await auth.verify_authority(
            _request(),
            _user(token_scopes=frozenset({"read"})),
        )


@pytest.mark.asyncio
async def test_verify_authority_requires_exact_stored_scope(monkeypatch):
    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("access check must not run")

    monkeypatch.setattr(auth, "check_vault_access", must_not_run)
    with pytest.raises(ForbiddenError, match="does not match"):
        await auth.verify_authority(
            _request(scope={"prefixes": ["automation-"], "extra_vaults": []}),
            _user(),
        )


@pytest.mark.asyncio
async def test_verify_authority_rejects_writer_outside_expected_scope(monkeypatch):
    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("access check must not run")

    monkeypatch.setattr(auth, "check_vault_access", must_not_run)
    with pytest.raises(ForbiddenError, match="exceeds"):
        await auth.verify_authority(
            _request(writer_authorities=[{"vault": "unrelated-vault", "action": "*"}]),
            _user(),
        )


@pytest.mark.asyncio
async def test_verify_authority_sanitizes_effective_writer_denial(monkeypatch):
    async def denied(*_args, **_kwargs):
        raise ForbiddenError("sensitive policy provenance")

    monkeypatch.setattr(auth, "check_vault_access", denied)
    with pytest.raises(ForbiddenError) as exc_info:
        await auth.verify_authority(_request(), _user())

    assert str(exc_info.value) == "Authenticated token lacks required writer authority"
    assert "sensitive" not in str(exc_info.value)


def test_authority_verify_is_published_in_openapi(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.main import app

    schema = app.openapi()
    operation = schema["paths"]["/api/v1/auth/authority/verify"]["post"]
    assert operation["summary"] == "Verify the current PAT's exact write authority"
    assert operation["security"] == [{"bearerAuth": []}]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert request_schema == {"$ref": "#/components/schemas/VerifyAuthorityRequest"}
    assert response_schema == {"$ref": "#/components/schemas/VerifyAuthorityResponse"}

    components = schema["components"]["schemas"]
    assert components["VerifyAuthorityRequest"]["additionalProperties"] is False
    assert components["VerifyAuthorityRequest"]["properties"]["writer_authorities"]["maxItems"] == 32
    assert components["WriterAuthorityRequest"]["additionalProperties"] is False
    assert components["VerifyAuthorityResponse"]["additionalProperties"] is False
    assert components["VerifiedWriterAuthority"]["additionalProperties"] is False
