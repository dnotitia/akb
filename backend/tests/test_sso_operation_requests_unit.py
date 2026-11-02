"""Operation request contract pieces that need no database: the shared fixture,
the declared response shapes and the HPKE seal."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest

from app.api.routes.admin_sso_operation_requests import (
    OperationRequestList,
    OperationRequestView,
    RequestDocument,
    SealedCredentialResponse,
)
from app.sso import identity_operations as ops, operation_requests, sealing

_FIXTURE = Path(__file__).parent / "fixtures" / "admin_sso_operation_requests_v1.json"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _keypair() -> tuple[X25519PrivateKey, str]:
    private = X25519PrivateKey.generate()
    return private, _b64(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def test_shared_fixture_is_a_valid_list_whose_digests_name_their_documents():
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    OperationRequestList.model_validate(fixture)
    for item in fixture["requests"]:
        canonical = json.dumps(item["request"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert hashlib.sha256(canonical.encode()).hexdigest() == item["request_sha256"] == ops._digest(item["request"])
        # The declared model must hand the hashed object back unchanged, or a
        # reader could no longer verify the digest from the response.
        assert RequestDocument.model_validate(item["request"]).model_dump(mode="json") == item["request"]


def test_view_projection_matches_the_declared_model():
    now = datetime.now(timezone.utc)
    row = {"request_id": uuid.uuid4(), "request_sha256": "a" * 64, "request": "{}", "status": "pending",
           "created_at": now, "expires_at": now, "requester_username": "machine", "decided_by": None,
           "decider_username": None, "decided_at": None, "operation_expires_at": None, "closed_at": None}
    assert set(operation_requests.view(row)) == set(OperationRequestView.model_fields)
    # The only response that carries a credential carries it sealed, and keeps it out of reprs.
    assert SealedCredentialResponse.model_fields["sealed_credential"].repr is False
    assert not {"credential", "raw_credential"} & set(SealedCredentialResponse.model_fields)


def test_seal_opens_only_with_the_private_key_and_the_same_request_binding():
    private, public = _keypair()
    sealed = sealing.seal("akb_sso_operation_fixture", public, request_id="r", request_sha256="d")
    data = base64.urlsafe_b64decode(sealed + "=" * (-len(sealed) % 4))
    # The encapsulated X25519 key, then the ciphertext and its 16-byte tag.
    assert len(data) == 32 + len("akb_sso_operation_fixture") + 16
    assert sealing.SUITE.decrypt(data, private, info=sealing.info("r", "d")) == b"akb_sso_operation_fixture"
    for key, info in ((X25519PrivateKey.generate(), sealing.info("r", "d")),
                      (private, sealing.info("other", "d")), (private, sealing.info("r", "other"))):
        with pytest.raises(InvalidTag):
            sealing.SUITE.decrypt(data, key, info=info)
    # Fresh ephemeral key per seal: equal inputs never give equal outputs.
    assert sealing.seal("akb_sso_operation_fixture", public, request_id="r", request_sha256="d") != sealed


@pytest.mark.parametrize("value", [
    _b64(bytes(32)),  # low-order point: all-zero shared secret
    _b64(bytes([1]) + bytes(31)),  # low-order point
    _b64(b"\x09" * 31),  # 31 bytes
    _b64(b"\x09" * 33),  # 33 bytes
    _b64(b"\x09" * 32) + "=",  # padding
    base64.b64encode(b"\xfb" * 32).rstrip(b"=").decode(),  # standard alphabet
    _b64(b"\x09" * 32)[:-1] + "B",  # non-canonical trailing bits
    "é" * 43,
    "",
])
def test_unusable_recipient_keys_are_refused(value):
    with pytest.raises(sealing.SealingKeyError):
        sealing.public_key(value)


def test_configure_summary_keeps_every_field_but_the_secret():
    from app.api.routes.admin_sso import ConfigureProviderRequest

    spec = ConfigureProviderRequest(provider_type="keycloak-oidc", display_name="Company SSO",
                                    issuer="https://id.example.com", discovery_url="https://id.example.com/d",
                                    client_id="akb", client_secret="fixture-value")  # pragma: allowlist secret
    summary = ops.summarize_configure(spec)
    assert summary == {"provider_type": "keycloak-oidc", "display_name": "Company SSO",
                       "issuer": "https://id.example.com", "discovery_url": "https://id.example.com/d",
                       "client_id": "akb", "client_secret_supplied": True}
    assert set(summary) - {"client_secret_supplied"} | {"client_secret"} == set(ConfigureProviderRequest.model_fields)
    assert ops.summarize_configure(spec.model_copy(update={"client_secret": None}))["client_secret_supplied"] is False


def test_capability_is_advertised():
    assert "sso.operation-requests.v1" in ops.CAPABILITIES


def _reader_request(authorization):
    from starlette.requests import Request

    headers = [(b"authorization", authorization.encode())] if authorization else []
    return Request({"type": "http", "method": "GET", "path": "/api/v1/admin/sso/operation-requests", "headers": headers})


@pytest.mark.asyncio
async def test_a_local_human_administrator_bearer_reads_operation_requests(monkeypatch):
    """Local mode authenticates the human product admin with a bearer; the reader accepts it."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.api.routes import admin_sso_operation_requests as routes
    from app.config import settings

    human = SimpleNamespace(kind="human")
    service = AsyncMock(side_effect=AssertionError("a local human must not reach the service-key path"))
    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    monkeypatch.setattr(routes, "get_current_product_admin", AsyncMock(return_value=human))
    monkeypatch.setattr(routes, "get_requesting_service_key", service)

    assert await routes.get_request_reader(_reader_request("Bearer local"), authorization="Bearer local") is human
    service.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_local_bearer_that_is_not_a_human_administrator_falls_to_the_service_key(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.api.routes import admin_sso_operation_requests as routes
    from app.config import settings
    from app.exceptions import AuthenticationError

    service_actor = SimpleNamespace(kind="service")
    monkeypatch.setattr(settings, "auth_mode", "local", raising=False)
    monkeypatch.setattr(routes, "get_current_product_admin", AsyncMock(side_effect=AuthenticationError("no")))
    monkeypatch.setattr(routes, "get_requesting_service_key", AsyncMock(return_value=service_actor))

    assert await routes.get_request_reader(_reader_request("Bearer akb_key"), authorization="Bearer akb_key") is service_actor


@pytest.mark.asyncio
async def test_an_sso_mode_bearer_is_only_ever_a_service_key(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.api.routes import admin_sso_operation_requests as routes
    from app.config import settings

    service_actor = SimpleNamespace(kind="service")
    human = AsyncMock(side_effect=AssertionError("SSO mode reads the human from the session, not a bearer"))
    monkeypatch.setattr(settings, "auth_mode", "sso", raising=False)
    monkeypatch.setattr(routes, "get_current_product_admin", human)
    monkeypatch.setattr(routes, "get_requesting_service_key", AsyncMock(return_value=service_actor))

    assert await routes.get_request_reader(_reader_request("Bearer akb_key"), authorization="Bearer akb_key") is service_actor
    human.assert_not_awaited()
