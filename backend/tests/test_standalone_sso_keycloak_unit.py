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
    "one-time-upgrade-secret-must-not-leak",  # pragma: allowlist secret
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


async def test_upgrade_authority_authenticates_only_against_master_realm():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/realms/master/protocol/openid-connect/token"
        form = dict(item.split("=", 1) for item in request.content.decode().split("&"))
        assert form["client_id"] == "akb-bootstrap-upgrade-v2"
        return httpx.Response(200, json={"access_token": "opaque-upgrade-token"})

    control, spec = _control(httpx.MockTransport(handler))
    spec = replace(spec, upgrade_client_secret=_SECRETS[5])
    try:
        assert await control.acquire_upgrade(spec) == "opaque-upgrade-token"
    finally:
        await control.aclose()


async def test_legacy_upgrade_updates_api_client_and_signed_provider_mapper(monkeypatch):
    control = KeycloakStandaloneSSOControl()
    spec = replace(
        _spec(),
        backchannel_logout_uri=("http://backend:8000/api/v1/auth/keycloak/backchannel-logout"),
        upgrade_client_secret=_SECRETS[5],
    )
    events: list[tuple[str, object]] = []
    expected_readback = object()

    async def _exact_client(_spec, realm, client_id, *, token):
        events.append(("exact-client", (realm, client_id, token)))
        return {**control._api_client(spec), "id": "api-client-uuid"}  # noqa: SLF001

    async def _reconcile_mapper(_spec, client_uuid, desired, *, token):
        events.append(("reconcile-mapper", (client_uuid, desired, token)))
        return desired

    async def _reconcile_client(_spec, desired, *, token):
        events.append(("reconcile-client", (desired, token)))
        return {**desired, "id": "api-client-uuid"}

    async def _readback(_spec, *, management_token):
        events.append(("readback", management_token))
        return expected_readback

    monkeypatch.setattr(control, "_exact_client", _exact_client)
    monkeypatch.setattr(control, "_reconcile_client", _reconcile_client)
    monkeypatch.setattr(control, "_reconcile_mapper", _reconcile_mapper)
    monkeypatch.setattr(control, "readback", _readback)

    result = await control.upgrade_legacy_to_current(
        spec,
        upgrade_token="opaque-upgrade-token",  # pragma: allowlist secret
    )

    assert result is expected_readback
    assert [name for name, _value in events] == [
        "exact-client",
        "reconcile-client",
        "reconcile-mapper",
        "readback",
    ]
    desired_client, client_token = events[1][1]
    assert desired_client["attributes"]["backchannel.logout.url"] == (
        "http://backend:8000/api/v1/auth/keycloak/backchannel-logout"
    )
    assert client_token == "opaque-upgrade-token"  # pragma: allowlist secret
    client_uuid, desired, token = events[2][1]
    assert client_uuid == "api-client-uuid"
    assert desired == control._api_identity_provider_mapper()  # noqa: SLF001
    assert token == "opaque-upgrade-token"  # pragma: allowlist secret


@pytest.mark.parametrize(
    "actual_callback",
    [
        "https://akb.example.com/api/v1/auth/keycloak/backchannel-logout",
        "http://backend:8000/api/v1/auth/keycloak/backchannel-logout",
    ],
)
async def test_callback_upgrade_accepts_only_exact_receipt_source_or_target(
    monkeypatch,
    actual_callback: str,
):
    control = KeycloakStandaloneSSOControl()
    source_callback = "https://akb.example.com/api/v1/auth/keycloak/backchannel-logout"
    spec = replace(
        _spec(),
        backchannel_logout_uri=("http://backend:8000/api/v1/auth/keycloak/backchannel-logout"),
        upgrade_client_secret=_SECRETS[5],
    )
    reconciled: list[dict[str, object]] = []
    expected_readback = object()

    async def _exact_client(_spec, _realm, _client_id, *, token):
        assert token == "opaque-upgrade-token"  # pragma: allowlist secret
        return {
            **control._api_client(  # noqa: SLF001
                spec,
                backchannel_logout_uri=actual_callback,
            ),
            "id": "api-client-uuid",
        }

    async def _reconcile_client(_spec, desired, *, token):
        assert token == "opaque-upgrade-token"  # pragma: allowlist secret
        reconciled.append(desired)
        return {**desired, "id": "api-client-uuid"}

    async def _readback(_spec, *, management_token):
        assert management_token == "opaque-upgrade-token"  # pragma: allowlist secret
        return expected_readback

    monkeypatch.setattr(control, "_exact_client", _exact_client)
    monkeypatch.setattr(control, "_reconcile_client", _reconcile_client)
    monkeypatch.setattr(control, "readback", _readback)

    result = await control.upgrade_callback_to_current(
        spec,
        source_backchannel_logout_uri=source_callback,
        upgrade_token="opaque-upgrade-token",  # pragma: allowlist secret
    )

    assert result is expected_readback
    assert len(reconciled) == 1
    assert reconciled[0]["attributes"]["backchannel.logout.url"] == (spec.backchannel_logout_uri_effective)


async def test_callback_upgrade_rejects_callback_outside_receipt_transition(monkeypatch):
    control = KeycloakStandaloneSSOControl()
    spec = replace(
        _spec(),
        backchannel_logout_uri=("http://backend:8000/api/v1/auth/keycloak/backchannel-logout"),
        upgrade_client_secret=_SECRETS[5],
    )

    async def _exact_client(_spec, _realm, _client_id, *, token):
        assert token == "opaque-upgrade-token"  # pragma: allowlist secret
        return {
            **control._api_client(  # noqa: SLF001
                spec,
                backchannel_logout_uri=("https://other.example.com/api/v1/auth/keycloak/backchannel-logout"),
            ),
            "id": "api-client-uuid",
        }

    async def _should_not_reconcile(*_args, **_kwargs):
        raise AssertionError("callback drift must not be reconciled")

    monkeypatch.setattr(control, "_exact_client", _exact_client)
    monkeypatch.setattr(control, "_reconcile_client", _should_not_reconcile)

    with pytest.raises(StandaloneSSOBootstrapError) as captured:
        await control.upgrade_callback_to_current(
            spec,
            source_backchannel_logout_uri=("https://akb.example.com/api/v1/auth/keycloak/backchannel-logout"),
            upgrade_token="opaque-upgrade-token",  # pragma: allowlist secret
        )

    assert captured.value.code == "keycloak_client_readback_failed"


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
    active_public = (
        base64.b64encode(
            rsa.generate_private_key(public_exponent=65537, key_size=3072)
            .public_key()
            .public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        )
        .decode("ascii")
        .rstrip("=")
    )
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

    assert deleted_paths == ["/admin/realms/master/clients/bootstrap-client-uuid"]


async def test_upgrade_retirement_deletes_only_exact_master_client_and_reauth_denies():
    deleted_paths: list[str] = []
    retired = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal retired
        if request.url.path == "/admin/realms/master/clients":
            assert request.url.params["clientId"] == "akb-bootstrap-upgrade-v2"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "upgrade-client-uuid",
                        "clientId": "akb-bootstrap-upgrade-v2",
                    }
                ],
            )
        if request.url.path == "/admin/realms/master/clients/upgrade-client-uuid":
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
    spec = replace(spec, upgrade_client_secret=_SECRETS[5])
    try:
        await control.retire_upgrade(
            spec,
            upgrade_token="opaque-upgrade-token",  # pragma: allowlist secret
        )
        await control.assert_upgrade_retired(
            spec,
            upgrade_token="opaque-upgrade-token",  # pragma: allowlist secret
        )
    finally:
        await control.aclose()

    assert deleted_paths == ["/admin/realms/master/clients/upgrade-client-uuid"]


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
                        "requiredActions": (["UPDATE_PASSWORD"] if user_reads > 1 else []),
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


async def test_readback_does_not_inspect_the_product_admin_credential_list(monkeypatch):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/federated-identity"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    control, spec = _control(httpx.MockTransport(handler))
    clients = {
        "akb-web": ("api-client-uuid", control._api_client(spec)),  # noqa: SLF001
        "akb-admin": ("admin-client-uuid", control._admin_client(spec)),  # noqa: SLF001
        "akb-sso-manager": (
            "management-client-uuid",
            control._management_client(spec),  # noqa: SLF001
        ),
    }
    mappers = {
        "api-client-uuid": [
            {
                "name": "akb-api-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "consentRequired": False,
                "config": {
                    "included.custom.audience": "https://akb.example.com/api",
                    "id.token.claim": "false",
                    "access.token.claim": "true",
                },
            },
            control._api_identity_provider_mapper(),  # noqa: SLF001
        ],
        "admin-client-uuid": [
            {
                "name": "akb-admin-native-amr",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-amr-mapper",
                "consentRequired": False,
                "config": {
                    "id.token.claim": "true",
                    "access.token.claim": "false",
                },
            }
        ],
    }
    roles = (
        "manage-identity-providers",
        "query-clients",
        "query-users",
        "view-clients",
        "view-realm",
        "view-users",
    )

    async def _realm(_spec, *, token):
        return {**control._realm_profile(spec), "id": "akb-realm-id"}  # noqa: SLF001

    async def _exact_client(_spec, _realm, client_id, *, token):
        uuid_value, profile = clients[client_id]
        # Keycloak always renders an attributes object, even when empty.
        return {**profile, "id": uuid_value, "attributes": profile.get("attributes", {})}

    async def _protocol_mappers(_spec, client_uuid, *, token):
        return mappers[client_uuid]

    async def _exact_user(_spec, *, token):
        # An installed realm whose administrator has since completed the
        # forced password change, or had the credential reset.
        return {
            "id": "product-admin-uuid",
            "username": "product-admin",
            "email": "product-admin@example.com",
            "enabled": True,
            "emailVerified": True,
            "requiredActions": ["UPDATE_PASSWORD"],
        }

    async def _key_readback(_spec, *, token):
        return ("rsa-3072-active-kid", 3072, 1)

    async def _native_amr_readback(_spec, *, token):
        return "pwd"

    async def _management_roles(_spec, _uuid, *, token):
        return roles

    async def _management_scope_roles(_spec, _uuid, *, token):
        return roles

    monkeypatch.setattr(control, "_realm", _realm)
    monkeypatch.setattr(control, "_exact_client", _exact_client)
    monkeypatch.setattr(control, "_protocol_mappers", _protocol_mappers)
    monkeypatch.setattr(control, "_exact_user", _exact_user)
    monkeypatch.setattr(control, "_key_readback", _key_readback)
    monkeypatch.setattr(control, "_native_amr_readback", _native_amr_readback)
    monkeypatch.setattr(control, "_management_roles", _management_roles)
    monkeypatch.setattr(control, "_management_scope_roles", _management_scope_roles)

    try:
        readback = await control.readback(
            spec,
            management_token="opaque-token",  # pragma: allowlist secret
        )
    finally:
        await control.aclose()

    assert readback.product_admin_subject == "product-admin-uuid"
    assert readback.product_admin_federated_identities == 0
    # Steady-state readback proves the account is realm-native. Whether a
    # credential currently exists is not its business, and asking would refuse
    # a converged realm on the next redeploy.
    assert requests == [
        "GET /admin/realms/akb/users/product-admin-uuid/federated-identity",
    ]


async def test_created_product_admin_must_read_back_holding_a_credential():
    """The install must prove its own write, not assume it.

    Nothing else covers this: with the read-back removed, an install that
    silently failed to attach the credential still reports success, and the
    account is left unreachable.
    """
    user_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal user_reads
        if request.url.path == "/admin/realms/akb/users" and request.method == "GET":
            user_reads += 1
            if user_reads == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "product-admin-uuid",
                        "username": "product-admin",
                        "email": "product-admin@example.com",
                        "enabled": True,
                        "emailVerified": True,
                        "requiredActions": ["UPDATE_PASSWORD"],
                    }
                ],
            )
        if request.url.path == "/admin/realms/akb/users" and request.method == "POST":
            return httpx.Response(201)
        if request.url.path.endswith("/credentials"):
            # The realm accepted the create but holds no password for it.
            return httpx.Response(200, json=[])
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

    assert captured.value.code == "keycloak_product_admin_password_missing"


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
    spec = replace(
        _spec(),
        backchannel_logout_uri=("http://backend:8000/api/v1/auth/keycloak/backchannel-logout"),
    )
    api = KeycloakStandaloneSSOControl._api_client(spec)  # noqa: SLF001
    admin = KeycloakStandaloneSSOControl._admin_client(spec)  # noqa: SLF001
    management = KeycloakStandaloneSSOControl._management_client(spec)  # noqa: SLF001

    assert {api["clientId"], admin["clientId"], management["clientId"]} == {
        "akb-web",
        "akb-admin",
        "akb-sso-manager",
    }
    assert api["redirectUris"] == ["https://akb.example.com/api/v1/auth/keycloak/callback"]
    assert admin["redirectUris"] == ["https://akb.example.com/api/v1/admin/auth/keycloak/callback"]
    assert api["attributes"]["pkce.code.challenge.method"] == "S256"
    assert api["frontchannelLogout"] is False
    assert api["attributes"]["backchannel.logout.url"] == (
        "http://backend:8000/api/v1/auth/keycloak/backchannel-logout"
    )
    assert api["attributes"]["backchannel.logout.session.required"] == "true"
    assert admin["attributes"]["pkce.code.challenge.method"] == "S256"
    assert api["defaultClientScopes"] == ["basic", "profile", "email"]
    assert admin["defaultClientScopes"] == ["basic", "profile", "email"]
    assert management["standardFlowEnabled"] is False
    assert management["serviceAccountsEnabled"] is True
    assert management["defaultClientScopes"] == ["service_account"]
    assert management["redirectUris"] == []


async def test_api_client_defaults_backchannel_logout_to_the_public_origin():
    api = KeycloakStandaloneSSOControl._api_client(_spec())  # noqa: SLF001

    assert api["attributes"]["backchannel.logout.url"] == (
        "https://akb.example.com/api/v1/auth/keycloak/backchannel-logout"
    )


async def test_api_client_maps_signed_broker_provenance_into_both_token_profiles():
    mapper = KeycloakStandaloneSSOControl._api_identity_provider_mapper()  # noqa: SLF001

    assert mapper == {
        "name": "akb-browser-identity-provider",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usersessionmodel-note-mapper",
        "consentRequired": False,
        "config": {
            "user.session.note": "identity_provider",
            "claim.name": "identity_provider",
            "jsonType.label": "String",
            "id.token.claim": "true",
            "access.token.claim": "true",
            "lightweight.claim": "false",
            "userinfo.token.claim": "false",
            "introspection.token.claim": "true",
            "access.tokenResponse.claim": "false",
        },
    }

    changed = dict(mapper)
    changed["config"] = dict(mapper["config"], **{"access.token.claim": "false"})
    assert KeycloakStandaloneSSOControl._mapper_matches(mapper, mapper) is True  # noqa: SLF001
    assert KeycloakStandaloneSSOControl._mapper_matches(changed, mapper) is False  # noqa: SLF001
