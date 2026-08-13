"""Focused transport and read-back contracts for the Keycloak adapter."""

from __future__ import annotations

import base64
from dataclasses import replace
import json

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import httpx
import pytest

from app.services.standalone_sso_bootstrap import (
    StandaloneSSOBootstrapError,
    StandaloneSSOBootstrapSpec,
)
from app.services.standalone_sso_keycloak import KeycloakStandaloneSSOControl


pytestmark = pytest.mark.asyncio

_SECRETS = (
    "temporary-bootstrap-secret-must-not-leak",  # pragma: allowlist secret
    "permanent-management-secret-must-not-leak",  # pragma: allowlist secret
    "api-browser-secret-must-not-leak",  # pragma: allowlist secret
    "admin-browser-secret-must-not-leak",  # pragma: allowlist secret
    "one-time-product-admin-password",  # pragma: allowlist secret
)


def _spec() -> StandaloneSSOBootstrapSpec:
    return StandaloneSSOBootstrapSpec(
        keycloak_internal_url="http://keycloak:8080",
        keycloak_public_url="https://auth.akb.example.com",
        realm="akb",
        akb_public_url="https://akb.example.com",
        bootstrap_client_id="akb-bootstrap-temporary",
        bootstrap_client_secret=_SECRETS[0],
        management_client_id="akb-sso-manager",
        management_client_secret=_SECRETS[1],
        api_client_id="akb-web",
        api_client_secret=_SECRETS[2],
        admin_client_id="akb-admin",
        admin_client_secret=_SECRETS[3],
        product_admin_username="product-admin",
        product_admin_email="product-admin@example.com",
        product_admin_password=_SECRETS[4],
    )


def _control(
    handler: httpx.MockTransport,
) -> tuple[KeycloakStandaloneSSOControl, StandaloneSSOBootstrapSpec]:
    spec = _spec()
    control = KeycloakStandaloneSSOControl()
    control._clients[spec.keycloak_internal_url] = httpx.AsyncClient(  # noqa: SLF001
        base_url=spec.keycloak_internal_url,
        transport=handler,
    )
    return control, spec


async def test_admin_rest_failure_never_includes_response_or_request_secrets():
    response_body = " ".join(_SECRETS)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=response_body)

    control, spec = _control(httpx.MockTransport(handler))
    try:
        with pytest.raises(StandaloneSSOBootstrapError) as captured:
            await control.acquire_bootstrap(spec)
    finally:
        await control.aclose()

    assert captured.value.code == "keycloak_token_request_failed"
    rendered = f"{captured.value!s} {captured.value!r}"
    assert all(secret not in rendered for secret in _SECRETS)


async def test_removed_bootstrap_secret_skips_token_request():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an absent retired credential must not be transmitted")

    control, spec = _control(httpx.MockTransport(handler))
    spec = replace(spec, bootstrap_client_secret="")
    try:
        assert await control.acquire_bootstrap(spec) is None
    finally:
        await control.aclose()


async def test_keycloak_internal_base_path_is_preserved():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/realms/master/protocol/openid-connect/token"
        return httpx.Response(200, json={"access_token": "opaque-token"})

    spec = replace(_spec(), keycloak_internal_url="http://keycloak:8080/auth")
    control = KeycloakStandaloneSSOControl()
    control._clients[spec.keycloak_internal_url] = httpx.AsyncClient(  # noqa: SLF001
        base_url=f"{spec.keycloak_internal_url}/",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await control.acquire_bootstrap(spec) == "opaque-token"
    finally:
        await control.aclose()


async def test_duplicate_exact_client_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/realms/akb/clients"
        assert request.url.params["clientId"] == "akb-web"
        return httpx.Response(
            200,
            json=[
                {"id": "first", "clientId": "akb-web"},
                {"id": "second", "clientId": "akb-web"},
            ],
        )

    control, spec = _control(httpx.MockTransport(handler))
    try:
        with pytest.raises(StandaloneSSOBootstrapError) as captured:
            await control._exact_client(  # noqa: SLF001
                spec,
                spec.realm,
                spec.api_client_id,
                token="opaque-token",  # pragma: allowlist secret
            )
    finally:
        await control.aclose()

    assert captured.value.code == "keycloak_client_duplicate"


async def test_signing_key_readback_measures_active_key_and_rotation_window():
    active_public = base64.b64encode(
        rsa.generate_private_key(public_exponent=65537, key_size=3072)
        .public_key()
        .public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    ).decode("ascii").rstrip("=")
    passive_public = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/realms/akb/keys"
        return httpx.Response(
            200,
            json={
                "active": {"RS256": "active-kid"},
                "keys": [
                    {
                        "kid": "active-kid",
                        "algorithm": "RS256",
                        "use": "SIG",
                        "status": "ACTIVE",
                        "publicKey": active_public,
                    },
                    {
                        "kid": "passive-kid",
                        "algorithm": "RS256",
                        "use": "SIG",
                        "status": "PASSIVE",
                        "publicKey": passive_public,
                    },
                    {
                        "kid": "disabled-kid",
                        "algorithm": "RS256",
                        "use": "SIG",
                        "status": "DISABLED",
                        "publicKey": passive_public,
                    },
                ],
            },
        )

    control, spec = _control(httpx.MockTransport(handler))
    try:
        measured = await control._key_readback(  # noqa: SLF001
            spec,
            token="opaque-token",  # pragma: allowlist secret
        )
    finally:
        await control.aclose()

    assert measured == ("active-kid", 3072, 1)


async def test_bootstrap_retirement_deletes_only_exact_master_client_and_reauth_denies():
    deleted_paths: list[str] = []
    retired = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal retired
        if request.url.path == "/admin/realms/master/clients":
            assert request.url.params["clientId"] == "akb-bootstrap-temporary"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "bootstrap-client-uuid",
                        "clientId": "akb-bootstrap-temporary",
                    }
                ],
            )
        if request.url.path == "/admin/realms/master/clients/bootstrap-client-uuid":
            assert request.method == "DELETE"
            retired = True
            deleted_paths.append(request.url.path)
            return httpx.Response(204)
        if request.url.path == "/admin/realms/master":
            assert retired is True
            return httpx.Response(401)
        if request.url.path == "/realms/master/protocol/openid-connect/token":
            assert retired is True
            return httpx.Response(401)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    control, spec = _control(httpx.MockTransport(handler))
    try:
        await control.retire_bootstrap(
            spec,
            bootstrap_token="opaque-bootstrap-token",  # pragma: allowlist secret
        )
        await control.assert_bootstrap_retired(
            spec,
            bootstrap_token="opaque-bootstrap-token",  # pragma: allowlist secret
        )
    finally:
        await control.aclose()

    assert deleted_paths == [
        "/admin/realms/master/clients/bootstrap-client-uuid"
    ]


async def test_missing_one_time_password_cannot_create_product_admin():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        assert request.method == "GET"
        assert request.url.path == "/admin/realms/akb/users"
        return httpx.Response(200, json=[])

    control, spec = _control(httpx.MockTransport(handler))
    spec = replace(spec, product_admin_password="")
    try:
        with pytest.raises(StandaloneSSOBootstrapError) as captured:
            await control._reconcile_product_admin(  # noqa: SLF001
                spec,
                token="opaque-token",  # pragma: allowlist secret
            )
    finally:
        await control.aclose()

    assert captured.value.code == "keycloak_product_admin_password_unavailable"
    assert requests == ["GET /admin/realms/akb/users"]


async def test_existing_product_admin_password_is_reset_to_the_operator_input():
    requests: list[str] = []
    user_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal user_reads
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/admin/realms/akb/users":
            user_reads += 1
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "product-admin-uuid",
                        "username": "product-admin",
                        "email": "product-admin@example.com",
                        "enabled": True,
                        "emailVerified": True,
                        "requiredActions": (
                            ["UPDATE_PASSWORD"] if user_reads > 1 else []
                        ),
                    }
                ],
            )
        if request.url.path.endswith("/reset-password"):
            assert request.method == "PUT"
            assert json.loads(request.content) == {
                "type": "password",
                "value": _SECRETS[4],
                "temporary": True,
            }
            return httpx.Response(204)
        if request.url.path.endswith("/federated-identity"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/credentials"):
            return httpx.Response(200, json=[{"type": "password"}])
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    control, spec = _control(httpx.MockTransport(handler))
    try:
        user = await control._reconcile_product_admin(  # noqa: SLF001
            spec,
            token="opaque-token",  # pragma: allowlist secret
        )
    finally:
        await control.aclose()

    assert user["id"] == "product-admin-uuid"
    assert requests == [
        "GET /admin/realms/akb/users",
        "GET /admin/realms/akb/users/product-admin-uuid/federated-identity",
        "PUT /admin/realms/akb/users/product-admin-uuid/reset-password",
        "GET /admin/realms/akb/users",
        "GET /admin/realms/akb/users/product-admin-uuid/credentials",
    ]


async def test_product_admin_password_change_requirement_must_read_back():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/realms/akb/users":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "product-admin-uuid",
                        "username": "product-admin",
                        "email": "product-admin@example.com",
                        "enabled": True,
                        "emailVerified": True,
                        "requiredActions": [],
                    }
                ],
            )
        if request.url.path.endswith("/federated-identity"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/reset-password"):
            return httpx.Response(204)
        if request.url.path.endswith("/credentials"):
            return httpx.Response(200, json=[{"type": "password"}])
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    control, spec = _control(httpx.MockTransport(handler))
    try:
        with pytest.raises(StandaloneSSOBootstrapError) as captured:
            await control._reconcile_product_admin(  # noqa: SLF001
                spec,
                token="opaque-token",  # pragma: allowlist secret
            )
    finally:
        await control.aclose()

    assert captured.value.code == "keycloak_product_admin_update_password_missing"


async def test_product_admin_federation_race_after_password_reset_is_rejected():
    user_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal user_reads
        if request.url.path == "/admin/realms/akb/users":
            user_reads += 1
            user = {
                "id": "product-admin-uuid",
                "username": "product-admin",
                "email": "product-admin@example.com",
                "enabled": True,
                "emailVerified": True,
                "requiredActions": ["UPDATE_PASSWORD"],
            }
            if user_reads > 1:
                user["federationLink"] = "racing-storage-provider"
            return httpx.Response(200, json=[user])
        if request.url.path.endswith("/federated-identity"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/reset-password"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    control, spec = _control(httpx.MockTransport(handler))
    try:
        with pytest.raises(StandaloneSSOBootstrapError) as captured:
            await control._reconcile_product_admin(  # noqa: SLF001
                spec,
                token="opaque-token",  # pragma: allowlist secret
            )
    finally:
        await control.aclose()

    assert captured.value.code == "keycloak_admin_identity_is_federated"


async def test_existing_product_admin_cannot_be_adopted_without_one_time_password():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        assert request.url.path == "/admin/realms/akb/users"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "product-admin-uuid",
                    "username": "product-admin",
                    "email": "product-admin@example.com",
                    "enabled": True,
                    "emailVerified": True,
                }
            ],
        )

    control, spec = _control(httpx.MockTransport(handler))
    spec = replace(spec, product_admin_password="")
    try:
        with pytest.raises(StandaloneSSOBootstrapError) as captured:
            await control._reconcile_product_admin(  # noqa: SLF001
                spec,
                token="opaque-token",  # pragma: allowlist secret
            )
    finally:
        await control.aclose()

    assert captured.value.code == "keycloak_product_admin_password_unavailable"
    assert requests == ["GET /admin/realms/akb/users"]


async def test_existing_federated_identity_is_rejected_before_password_mutation():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path == "/admin/realms/akb/users":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "product-admin-uuid",
                        "username": "product-admin",
                        "email": "product-admin@example.com",
                        "enabled": True,
                        "emailVerified": True,
                    }
                ],
            )
        if request.url.path.endswith("/federated-identity"):
            return httpx.Response(
                200,
                json=[{"identityProvider": "upstream", "userId": "external"}],
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    control, spec = _control(httpx.MockTransport(handler))
    try:
        with pytest.raises(StandaloneSSOBootstrapError) as captured:
            await control._reconcile_product_admin(  # noqa: SLF001
                spec,
                token="opaque-token",  # pragma: allowlist secret
            )
    finally:
        await control.aclose()

    assert captured.value.code == "keycloak_admin_identity_is_federated"
    assert requests == [
        "GET /admin/realms/akb/users",
        "GET /admin/realms/akb/users/product-admin-uuid/federated-identity",
    ]


async def test_existing_user_storage_federation_is_rejected_before_password_mutation():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        assert request.url.path == "/admin/realms/akb/users"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "product-admin-uuid",
                    "username": "product-admin",
                    "email": "product-admin@example.com",
                    "enabled": True,
                    "emailVerified": True,
                    "federationLink": "ldap-storage-provider",
                }
            ],
        )

    control, spec = _control(httpx.MockTransport(handler))
    try:
        with pytest.raises(StandaloneSSOBootstrapError) as captured:
            await control._reconcile_product_admin(  # noqa: SLF001
                spec,
                token="opaque-token",  # pragma: allowlist secret
            )
    finally:
        await control.aclose()

    assert captured.value.code == "keycloak_admin_identity_is_federated"
    assert requests == ["GET /admin/realms/akb/users"]


async def test_realm_audits_operations_without_secret_bearing_representations():
    profile = KeycloakStandaloneSSOControl._realm_profile(_spec())  # noqa: SLF001

    assert profile["adminEventsEnabled"] is True
    assert profile["adminEventsDetailsEnabled"] is False


async def test_realm_sets_a_lean_product_admin_password_policy_from_creation():
    profile = KeycloakStandaloneSSOControl._realm_profile(_spec())  # noqa: SLF001

    assert profile["passwordPolicy"] == (  # pragma: allowlist secret
        "length(12) and notUsername and notEmail"
    )


async def test_client_profiles_separate_user_admin_and_management_authorities():
    spec = _spec()
    api = KeycloakStandaloneSSOControl._api_client(spec)  # noqa: SLF001
    admin = KeycloakStandaloneSSOControl._admin_client(spec)  # noqa: SLF001
    management = KeycloakStandaloneSSOControl._management_client(spec)  # noqa: SLF001

    assert {api["clientId"], admin["clientId"], management["clientId"]} == {
        "akb-web",
        "akb-admin",
        "akb-sso-manager",
    }
    assert api["redirectUris"] == [
        "https://akb.example.com/api/v1/auth/keycloak/callback"
    ]
    assert admin["redirectUris"] == [
        "https://akb.example.com/api/v1/admin/auth/keycloak/callback"
    ]
    assert api["attributes"]["pkce.code.challenge.method"] == "S256"
    assert admin["attributes"]["pkce.code.challenge.method"] == "S256"
    assert management["standardFlowEnabled"] is False
    assert management["serviceAccountsEnabled"] is True
    assert management["defaultClientScopes"] == ["service_account"]
    assert management["redirectUris"] == []
