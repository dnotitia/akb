"""Real RSA proofs and independently verified token-pair regression tests."""
import base64
import hashlib
import json
import time
import uuid
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import ValidationError

from app.config import AuthModeConfigurationError, CompanionLoginClient, settings
from app.exceptions import AuthenticationError
from app.services import companion_login as service
from app.services.keycloak_oidc import KeycloakOIDC


@pytest.fixture
def login(monkeypatch):
    bff = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kc = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = bff.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    for name, value in dict(auth_mode="sso", keycloak_enabled=True, public_base_url="https://akb.example.com",
                            keycloak_server_url="https://id.example.com", keycloak_realm="akb", keycloak_client_id="akb-web",
                            keycloak_companion_client_ids_by_origin={"https://app.example.com": "example-app"},
                            keycloak_companion_login_clients={"example-app": CompanionLoginClient(
                                public_keys={"login-v1": public}, provider_aliases=["workforce"])}).items():
        monkeypatch.setattr(settings, name, value)
    oidc = KeycloakOIDC()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(kc.public_key()))
    jwk.update(kid="oidc-v1", use="sig", alg="RS256")
    oidc._jwks = {"keys": [jwk]}
    monkeypatch.setattr(service, "get_keycloak_oidc", lambda: oidc)
    consume = AsyncMock()
    monkeypatch.setattr(service, "_consume_assertion", consume)
    req = service.CompanionLoginRequest(provider_alias="workforce", nonce="n" * 43)
    subject = str(uuid.uuid4())

    def tokens(access_changes=None, id_changes=None):
        now = int(time.time())
        common = dict(iss=settings.keycloak_issuer, sub=subject, sid="session-1", azp="example-app",
                      iat=now, exp=now + 300, identity_provider="workforce")
        claims = {**common, "aud": settings.api_oauth_audience_effective, "typ": "Bearer",
                  "jti": str(uuid.uuid4()), "scope": "openid profile email", "email": f"{subject}@corp.example",
                  "email_verified": True, "preferred_username": f"auth-{subject}", **(access_changes or {})}
        access = jwt.encode(claims, kc, algorithm="RS256", headers={"kid": "oidc-v1"})
        digest = base64.urlsafe_b64encode(hashlib.sha256(access.encode()).digest()[:16]).rstrip(b"=").decode()
        claims = {**common, "aud": "example-app", "nonce": req.nonce, "at_hash": digest, **(id_changes or {})}
        identity = jwt.encode(claims, kc, algorithm="RS256", headers={"kid": "oidc-v1"})
        return access, identity

    def sign(access, identity, changes=None, headers=None, key=None, request=None):
        now = int(time.time())
        claims = dict(iss="example-app", sub="example-app", aud=settings.public_base_url + service.ENDPOINT,
                      iat=now, exp=now + 60, jti=str(uuid.uuid4()), request_hash=service.request_hash(request or req, access, identity))
        claims.update(changes or {})
        return jwt.encode(claims, key or bff, algorithm="RS256", headers={"kid": "login-v1", **(headers or {})})
    return req, tokens, sign, consume, subject


def test_valid_assertion(login):
    req, tokens, sign, _, _ = login
    access, identity = tokens()
    assert service.verify_assertion(sign(access, identity), service.request_hash(req, access, identity), req.provider_alias)[0] == "example-app"


@pytest.mark.parametrize("changes", [
    {"iss": "other"}, {"sub": "other"}, {"aud": "https://evil.example"}, {"aud": ["https://akb.example.com" + service.ENDPOINT]},
    {"iat": True}, {"iat": int(time.time()) + 120}, {"exp": 1}, {"exp": int(time.time()) + 120},
    {"jti": "not-a-uuid"}, {"request_hash": "changed"},
])
def test_assertion_refusals(login, changes):
    req, tokens, sign, _, _ = login
    access, identity = tokens()
    with pytest.raises(AuthenticationError):
        service.verify_assertion(sign(access, identity, changes), service.request_hash(req, access, identity), req.provider_alias)


@pytest.mark.parametrize("headers", [{"kid": "missing"}, {"jku": "https://evil.example/jwks"}, {"typ": "at+jwt"}])
def test_header_refusals(login, headers):
    req, tokens, sign, _, _ = login
    access, identity = tokens()
    with pytest.raises(AuthenticationError):
        service.verify_assertion(sign(access, identity, headers=headers), service.request_hash(req, access, identity), req.provider_alias)


def test_forged_signature_and_provider(login):
    req, tokens, sign, _, _ = login
    access, identity = tokens()
    digest = service.request_hash(req, access, identity)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthenticationError):
        service.verify_assertion(sign(access, identity, key=key), digest, req.provider_alias)
    with pytest.raises(AuthenticationError):
        service.verify_assertion(sign(access, identity), digest, "other")


@pytest.mark.asyncio
@pytest.mark.parametrize("ac,ic", [
    ({"azp": "other"}, {}), ({"identity_provider": "other"}, {}), ({"aud": "wrong"}, {}),
    ({"typ": "ID"}, {}), ({"iss": "https://evil.example"}, {}), ({"exp": 1}, {}),
    ({}, {"sub": "other"}), ({}, {"sid": "other"}), ({}, {"nonce": "wrong"}), ({}, {"at_hash": "wrong"}), ({}, {"at_hash": "é"}),
    ({}, {"identity_provider": "other"}), ({}, {"azp": "other"}), ({}, {"aud": "akb-web"}), ({}, {"aud": ["example-app", "other"]}),
    ({}, {"exp": 1}), ({}, {"iss": "https://evil.example"}), ({}, {"client_id": "example-app"}),
])
async def test_pair_refused_before_db(login, ac, ic):
    req, tokens, sign, consume, _ = login
    access, identity = tokens(ac, ic)
    with pytest.raises(AuthenticationError):
        await service.complete_companion_login(req, access, identity, sign(access, identity))
    consume.assert_not_awaited()


@pytest.mark.asyncio
async def test_bearer_alone_cannot_complete(login):
    req, tokens, _, consume, _ = login
    access, identity = tokens()
    with pytest.raises(AuthenticationError):
        await service.complete_companion_login(req, access, identity, "")
    consume.assert_not_awaited()


def test_disabled_and_invalid_keys(login, monkeypatch):
    req, tokens, sign, _, _ = login
    access, identity = tokens()
    monkeypatch.setattr(settings, "keycloak_companion_login_clients", {})
    with pytest.raises(AuthenticationError):
        service.verify_assertion(sign(access, identity), service.request_hash(req, access, identity), req.provider_alias)
    with pytest.raises(ValidationError):
        CompanionLoginClient(public_keys={"v1": "invalid"}, provider_aliases=["workforce"])


def test_headers_redacted():
    import logging
    from app.logging_redaction import redact, SecretRedactingFilter
    for header in ("X-AKB-ID-Token", "X-AKB-Login-Assertion"):
        assert "opaque-secret" not in redact(f"{header}: opaque-secret")
        record = logging.LogRecord("test", logging.INFO, "", 1, f"{header}: opaque-secret", (), None)
        SecretRedactingFilter().filter(record)
        assert "opaque-secret" not in record.getMessage()


@pytest.mark.asyncio
async def test_route_success_is_cookie_free_and_account_denials_stable(login, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from app.api.routes import auth
    from app.exceptions import AccountSuspendedError, ExternalIdentityConflictError, MembershipRequiredError

    app = FastAPI()
    app.include_router(auth.router, prefix="/api/v1")
    req, tokens, sign, _, _ = login
    access, identity = tokens()
    headers = {"Authorization": "Bearer " + access, "X-AKB-ID-Token": identity,
               "X-AKB-Login-Assertion": sign(access, identity)}
    result = {"user": {"id": str(uuid.uuid4()), "username": "person", "email": "person@example.com",
                       "display_name": None, "is_admin": False}}
    complete = AsyncMock(return_value=result)
    monkeypatch.setattr(auth, "complete_companion_login", complete)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://akb.example.com") as client:
        response = await client.post(service.ENDPOINT, json=req.model_dump(), headers=headers)
        assert response.json() == result
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        for error in (AccountSuspendedError(), ExternalIdentityConflictError(), MembershipRequiredError()):
            complete.side_effect = error
            response = await client.post(service.ENDPOINT, json=req.model_dump(), headers=headers)
            assert response.status_code == error.status_code and response.json()["code"] == error.code
            assert response.headers["cache-control"] == "no-store"
            assert "set-cookie" not in response.headers
        complete.reset_mock()
        response = await client.post(service.ENDPOINT, json=req.model_dump(), headers={"Authorization": "Bearer " + access})
        assert response.status_code == 401 and response.headers["cache-control"] == "no-store"
        complete.assert_not_awaited()


def test_config_registration_is_explicit_and_validated(login):
    from app.config import Settings
    config = settings.model_dump()
    config.update(local_auth_enabled=False, keycloak_sso_only=True)
    assert Settings.model_validate(config).keycloak_companion_login_clients
    for changes in ({"public_base_url": "https://akb.example.com/prefix"},
                    {"public_base_url": "http://akb.example.com"}, {"auth_mode": "local"},
                    {"keycloak_companion_client_ids_by_origin": {}}):
        with pytest.raises((ValueError, AuthModeConfigurationError)):
            Settings.model_validate({**config, **changes})


def test_dotted_provider_alias_matches_existing_catalog(login):
    req, tokens, sign, _, _ = login
    client = settings.keycloak_companion_login_clients["example-app"]
    client.provider_aliases = ["workforce.ms"]
    # Validate both startup registration and exact request field using the
    # same provider vocabulary as AKB's existing provider catalog.
    assert CompanionLoginClient.model_validate(client.model_dump()).provider_aliases == ["workforce.ms"]
    request = service.CompanionLoginRequest.model_validate({**req.model_dump(), "provider_alias": "workforce.ms"})
    access, identity = tokens()
    assertion = sign(access, identity, request=request)
    assert service.verify_assertion(assertion, service.request_hash(request, access, identity), request.provider_alias)[0] == "example-app"
