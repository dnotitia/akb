"""Keycloak Admin REST adapter for the standalone SSO bootstrap lifecycle."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Mapping
import re
from typing import Any
from urllib.parse import quote

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    load_der_public_key,
    load_pem_public_key,
)
import httpx

from app.services.standalone_sso_bootstrap import (
    MANAGEMENT_REALM_ROLES,
    StandaloneSSOBootstrapError,
    StandaloneSSOBootstrapSpec,
    StandaloneSSOReadback,
)


_KEY_PROVIDER_TYPE = "org.keycloak.keys.KeyProvider"
_ACTIVE_KEY_PROVIDER_NAME = "akb-rs256-3072-active"
_NATIVE_AMR_CONFIG_ALIAS = "akb-native-password-amr"
_API_AUDIENCE_MAPPER_NAME = "akb-api-audience"
_ADMIN_AMR_MAPPER_NAME = "akb-admin-native-amr"
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")


def _fail(code: str) -> StandaloneSSOBootstrapError:
    return StandaloneSSOBootstrapError(code)


def _object(value: object, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(code)
    return value


def _objects(value: object, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _fail(code)
    return value


def _required_string(value: Mapping[str, object], key: str, code: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise _fail(code)
    return result


def _exact(items: list[dict[str, Any]], key: str, value: str, code: str) -> dict[str, Any] | None:
    matches = [item for item in items if item.get(key) == value]
    if len(matches) > 1:
        raise _fail(code)
    return matches[0] if matches else None


def _path(value: str) -> str:
    return quote(value, safe="")


def _rsa_public_key_size(public_key: str) -> int:
    """Measure Keycloak's bounded PEM or unpadded-base64 DER public key."""
    if not public_key or len(public_key) > 16_384:
        raise _fail("keycloak_active_rs256_key_invalid")
    try:
        if public_key.startswith("-----BEGIN PUBLIC KEY-----"):
            parsed = load_pem_public_key(public_key.encode("ascii"))
        else:
            padding = "=" * (-len(public_key) % 4)
            der = base64.b64decode(public_key + padding, validate=True)
            parsed = load_der_public_key(der)
    except (UnicodeEncodeError, ValueError, TypeError, binascii.Error) as exc:
        raise _fail("keycloak_active_rs256_key_invalid") from exc
    if not isinstance(parsed, rsa.RSAPublicKey):
        raise _fail("keycloak_active_rs256_key_invalid")
    return parsed.key_size


class KeycloakStandaloneSSOControl:
    """Narrow, secret-redacting Admin REST implementation."""

    def __init__(self, *, verify_ssl: bool = True) -> None:
        self._verify_ssl = verify_ssl
        self._clients: dict[str, httpx.AsyncClient] = {}

    def _client(self, spec: StandaloneSSOBootstrapSpec) -> httpx.AsyncClient:
        base_url = spec.keycloak_internal_url.rstrip("/")
        client = self._clients.get(base_url)
        if client is None:
            client = httpx.AsyncClient(
                base_url=base_url,
                verify=self._verify_ssl,
                timeout=httpx.Timeout(20.0, connect=10.0),
            )
            self._clients[base_url] = client
        return client

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()

    async def _token(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        realm: str,
        client_id: str,
        client_secret: str,
    ) -> str | None:
        if not client_id or not client_secret:
            return None
        try:
            response = await self._client(spec).post(
                f"/realms/{_path(realm)}/protocol/openid-connect/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise _fail("keycloak_unreachable") from exc
        if response.status_code in {400, 401, 403, 404}:
            return None
        if response.status_code != 200:
            raise _fail("keycloak_token_request_failed")
        try:
            body = _object(response.json(), "keycloak_token_response_invalid")
        except (TypeError, ValueError) as exc:
            raise _fail("keycloak_token_response_invalid") from exc
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise _fail("keycloak_token_response_invalid")
        return token

    async def acquire_management(self, spec: StandaloneSSOBootstrapSpec) -> str | None:
        return await self._token(
            spec,
            realm=spec.realm,
            client_id=spec.management_client_id,
            client_secret=spec.management_client_secret,
        )

    async def acquire_bootstrap(self, spec: StandaloneSSOBootstrapSpec) -> str | None:
        return await self._token(
            spec,
            realm="master",
            client_id=spec.bootstrap_client_id,
            client_secret=spec.bootstrap_client_secret,
        )

    async def _request(
        self,
        spec: StandaloneSSOBootstrapSpec,
        method: str,
        path: str,
        *,
        token: str,
        json_body: object | None = None,
        params: Mapping[str, str | int | float | bool | None] | None = None,
        expected: frozenset[int] = frozenset({200}),
        code: str = "keycloak_admin_request_failed",
    ) -> httpx.Response:
        try:
            response = await self._client(spec).request(
                method,
                path,
                headers={"Authorization": f"Bearer {token}"},
                json=json_body,
                params=params,
            )
        except httpx.HTTPError as exc:
            raise _fail("keycloak_unreachable") from exc
        if response.status_code not in expected:
            raise _fail(code)
        return response

    async def _json(
        self,
        spec: StandaloneSSOBootstrapSpec,
        path: str,
        *,
        token: str,
        params: Mapping[str, str | int | float | bool | None] | None = None,
        code: str,
    ) -> object:
        response = await self._request(
            spec,
            "GET",
            path,
            token=token,
            params=params,
            code=code,
        )
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise _fail(code) from exc

    async def _realm(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> dict[str, Any] | None:
        response = await self._request(
            spec,
            "GET",
            f"/admin/realms/{_path(spec.realm)}",
            token=token,
            expected=frozenset({200, 404}),
            code="keycloak_realm_read_failed",
        )
        if response.status_code == 404:
            return None
        try:
            return _object(response.json(), "keycloak_realm_read_failed")
        except (TypeError, ValueError) as exc:
            raise _fail("keycloak_realm_read_failed") from exc

    async def _reconcile_realm(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> dict[str, Any]:
        existing = await self._realm(spec, token=token)
        desired = self._realm_profile(spec)
        if existing is None:
            await self._request(
                spec,
                "POST",
                "/admin/realms",
                token=token,
                json_body=desired,
                expected=frozenset({201}),
                code="keycloak_realm_create_failed",
            )
        else:
            updated = dict(existing)
            updated.update(desired)
            await self._request(
                spec,
                "PUT",
                f"/admin/realms/{_path(spec.realm)}",
                token=token,
                json_body=updated,
                expected=frozenset({204}),
                code="keycloak_realm_update_failed",
            )
        realm = await self._realm(spec, token=token)
        if realm is None or not self._realm_matches(spec, realm):
            raise _fail("keycloak_realm_readback_failed")
        _required_string(realm, "id", "keycloak_realm_readback_failed")
        return realm

    @staticmethod
    def _realm_profile(spec: StandaloneSSOBootstrapSpec) -> dict[str, Any]:
        return {
            "realm": spec.realm,
            "enabled": True,
            "displayName": "AKB",
            "registrationAllowed": False,
            "registrationEmailAsUsername": False,
            "editUsernameAllowed": False,
            "resetPasswordAllowed": False,
            "loginWithEmailAllowed": False,
            "duplicateEmailsAllowed": False,
            "verifyEmail": False,
            "bruteForceProtected": True,
            "passwordPolicy": (  # pragma: allowlist secret
                "length(12) and notUsername and notEmail"
            ),
            "defaultSignatureAlgorithm": "RS256",
            "adminEventsEnabled": True,
            # Keycloak's detailed admin events persist the JSON representation
            # sent to Admin REST. Bootstrap requests contain client secrets and
            # the one-time product-admin password, so retain operation audit
            # events but never store their request representations.
            "adminEventsDetailsEnabled": False,
        }

    @classmethod
    def _realm_matches(
        cls,
        spec: StandaloneSSOBootstrapSpec,
        realm: Mapping[str, object],
    ) -> bool:
        return all(
            realm.get(key) == value
            for key, value in cls._realm_profile(spec).items()
        )

    async def _list_clients(
        self,
        spec: StandaloneSSOBootstrapSpec,
        realm: str,
        client_id: str,
        *,
        token: str,
    ) -> list[dict[str, Any]]:
        value = await self._json(
            spec,
            f"/admin/realms/{_path(realm)}/clients",
            token=token,
            params={"clientId": client_id},
            code="keycloak_client_read_failed",
        )
        return _objects(value, "keycloak_client_read_failed")

    async def _exact_client(
        self,
        spec: StandaloneSSOBootstrapSpec,
        realm: str,
        client_id: str,
        *,
        token: str,
    ) -> dict[str, Any] | None:
        clients = await self._list_clients(spec, realm, client_id, token=token)
        return _exact(
            clients,
            "clientId",
            client_id,
            "keycloak_client_duplicate",
        )

    async def _reconcile_client(
        self,
        spec: StandaloneSSOBootstrapSpec,
        desired: dict[str, Any],
        *,
        token: str,
    ) -> dict[str, Any]:
        client_id = _required_string(desired, "clientId", "keycloak_client_invalid")
        existing = await self._exact_client(
            spec,
            spec.realm,
            client_id,
            token=token,
        )
        if existing is None:
            await self._request(
                spec,
                "POST",
                f"/admin/realms/{_path(spec.realm)}/clients",
                token=token,
                json_body=desired,
                expected=frozenset({201}),
                code="keycloak_client_create_failed",
            )
        else:
            client_uuid = _required_string(
                existing,
                "id",
                "keycloak_client_read_failed",
            )
            updated = dict(existing)
            updated.update(desired)
            await self._request(
                spec,
                "PUT",
                f"/admin/realms/{_path(spec.realm)}/clients/{_path(client_uuid)}",
                token=token,
                json_body=updated,
                expected=frozenset({204}),
                code="keycloak_client_update_failed",
            )
        readback = await self._exact_client(
            spec,
            spec.realm,
            client_id,
            token=token,
        )
        if readback is None:
            raise _fail("keycloak_client_readback_failed")
        return readback

    @staticmethod
    def _api_client(spec: StandaloneSSOBootstrapSpec) -> dict[str, Any]:
        return {
            "clientId": spec.api_client_id,
            "name": "AKB browser and API",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "secret": spec.api_client_secret,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "implicitFlowEnabled": False,
            "fullScopeAllowed": False,
            "redirectUris": [
                f"{spec.akb_public_url.rstrip('/')}/api/v1/auth/keycloak/callback"
            ],
            "webOrigins": [spec.akb_public_url.rstrip("/")],
            # Keycloak 26.x puts the access-token subject mapper in its
            # built-in `basic` client scope.  Omitting it yields a validly
            # signed browser access token with no `sub`, which AKB must and
            # does reject at the exact-identity boundary.
            "defaultClientScopes": ["basic", "profile", "email"],
            "optionalClientScopes": [],
            "attributes": {
                "pkce.code.challenge.method": "S256",
                "post.logout.redirect.uris": f"{spec.akb_public_url.rstrip('/')}/*",
            },
        }

    @staticmethod
    def _admin_client(spec: StandaloneSSOBootstrapSpec) -> dict[str, Any]:
        return {
            "clientId": spec.admin_client_id,
            "name": "AKB product administration",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "secret": spec.admin_client_secret,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": False,
            "implicitFlowEnabled": False,
            "fullScopeAllowed": False,
            "redirectUris": [spec.admin_redirect_uri],
            "webOrigins": [spec.akb_public_url.rstrip("/")],
            "defaultClientScopes": ["basic", "profile", "email"],
            "optionalClientScopes": [],
            "attributes": {
                "pkce.code.challenge.method": "S256",
                "post.logout.redirect.uris": spec.admin_post_logout_redirect_uri,
            },
        }

    @staticmethod
    def _management_client(spec: StandaloneSSOBootstrapSpec) -> dict[str, Any]:
        return {
            "clientId": spec.management_client_id,
            "name": "AKB SSO provider management",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "secret": spec.management_client_secret,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "serviceAccountsEnabled": True,
            "implicitFlowEnabled": False,
            "fullScopeAllowed": False,
            "redirectUris": [],
            "webOrigins": [],
            # Keycloak attaches this built-in scope to every service-account
            # client; declaring it makes read-back exact without broadening
            # realm-management role scope.
            "defaultClientScopes": ["service_account"],
            "optionalClientScopes": [],
        }

    async def _protocol_mappers(
        self,
        spec: StandaloneSSOBootstrapSpec,
        client_uuid: str,
        *,
        token: str,
    ) -> list[dict[str, Any]]:
        value = await self._json(
            spec,
            (
                f"/admin/realms/{_path(spec.realm)}/clients/{_path(client_uuid)}"
                "/protocol-mappers/models"
            ),
            token=token,
            code="keycloak_mapper_read_failed",
        )
        return _objects(value, "keycloak_mapper_read_failed")

    async def _reconcile_mapper(
        self,
        spec: StandaloneSSOBootstrapSpec,
        client_uuid: str,
        desired: dict[str, Any],
        *,
        token: str,
    ) -> dict[str, Any]:
        name = _required_string(desired, "name", "keycloak_mapper_invalid")
        existing = _exact(
            await self._protocol_mappers(spec, client_uuid, token=token),
            "name",
            name,
            "keycloak_mapper_duplicate",
        )
        base = (
            f"/admin/realms/{_path(spec.realm)}/clients/{_path(client_uuid)}"
            "/protocol-mappers/models"
        )
        if existing is None:
            await self._request(
                spec,
                "POST",
                base,
                token=token,
                json_body=desired,
                expected=frozenset({201}),
                code="keycloak_mapper_create_failed",
            )
        else:
            mapper_id = _required_string(existing, "id", "keycloak_mapper_read_failed")
            updated = dict(desired)
            updated["id"] = mapper_id
            await self._request(
                spec,
                "PUT",
                f"{base}/{_path(mapper_id)}",
                token=token,
                json_body=updated,
                expected=frozenset({204}),
                code="keycloak_mapper_update_failed",
            )
        readback = _exact(
            await self._protocol_mappers(spec, client_uuid, token=token),
            "name",
            name,
            "keycloak_mapper_duplicate",
        )
        if readback is None:
            raise _fail("keycloak_mapper_readback_failed")
        return readback

    async def _reconcile_native_amr(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> None:
        value = await self._json(
            spec,
            f"/admin/realms/{_path(spec.realm)}/authentication/flows/browser/executions",
            token=token,
            code="keycloak_auth_flow_read_failed",
        )
        executions = _objects(value, "keycloak_auth_flow_read_failed")
        matches = [
            item
            for item in executions
            if item.get("providerId") == "auth-username-password-form"
        ]
        if len(matches) != 1:
            raise _fail("keycloak_native_password_execution_ambiguous")
        execution = matches[0]
        execution_id = _required_string(
            execution,
            "id",
            "keycloak_auth_flow_read_failed",
        )
        config_id = execution.get("authenticationConfig")
        desired = {
            "alias": _NATIVE_AMR_CONFIG_ALIAS,
            "config": {
                "default.reference.value": "pwd",
                "default.reference.maxAge": "300",
            },
        }
        if config_id is None:
            await self._request(
                spec,
                "POST",
                (
                    f"/admin/realms/{_path(spec.realm)}/authentication/executions/"
                    f"{_path(execution_id)}/config"
                ),
                token=token,
                json_body=desired,
                expected=frozenset({201}),
                code="keycloak_native_amr_create_failed",
            )
        elif isinstance(config_id, str) and config_id:
            updated = dict(desired)
            updated["id"] = config_id
            await self._request(
                spec,
                "PUT",
                (
                    f"/admin/realms/{_path(spec.realm)}/authentication/config/"
                    f"{_path(config_id)}"
                ),
                token=token,
                json_body=updated,
                expected=frozenset({204}),
                code="keycloak_native_amr_update_failed",
            )
        else:
            raise _fail("keycloak_auth_flow_read_failed")

    async def _native_amr_readback(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> str:
        value = await self._json(
            spec,
            f"/admin/realms/{_path(spec.realm)}/authentication/flows/browser/executions",
            token=token,
            code="keycloak_auth_flow_read_failed",
        )
        matches = [
            item
            for item in _objects(value, "keycloak_auth_flow_read_failed")
            if item.get("providerId") == "auth-username-password-form"
        ]
        if len(matches) != 1:
            raise _fail("keycloak_native_password_execution_ambiguous")
        config_id = matches[0].get("authenticationConfig")
        if not isinstance(config_id, str) or not config_id:
            raise _fail("keycloak_native_amr_readback_failed")
        config = _object(
            await self._json(
                spec,
                (
                    f"/admin/realms/{_path(spec.realm)}/authentication/config/"
                    f"{_path(config_id)}"
                ),
                token=token,
                code="keycloak_native_amr_readback_failed",
            ),
            "keycloak_native_amr_readback_failed",
        )
        if config.get("alias") != _NATIVE_AMR_CONFIG_ALIAS:
            raise _fail("keycloak_native_amr_readback_failed")
        values = config.get("config")
        if (
            not isinstance(values, dict)
            or values.get("default.reference.value") != "pwd"
            or values.get("default.reference.maxAge") != "300"
        ):
            raise _fail("keycloak_native_amr_readback_failed")
        return "pwd"

    async def _reconcile_signing_key(
        self,
        spec: StandaloneSSOBootstrapSpec,
        realm_id: str,
        *,
        token: str,
    ) -> None:
        path = f"/admin/realms/{_path(spec.realm)}/components"
        value = await self._json(
            spec,
            path,
            token=token,
            params={"parent": realm_id, "type": _KEY_PROVIDER_TYPE},
            code="keycloak_key_provider_read_failed",
        )
        components = _objects(value, "keycloak_key_provider_read_failed")
        existing = _exact(
            components,
            "name",
            _ACTIVE_KEY_PROVIDER_NAME,
            "keycloak_key_provider_duplicate",
        )
        desired = {
            "name": _ACTIVE_KEY_PROVIDER_NAME,
            "providerId": "rsa-generated",
            "providerType": _KEY_PROVIDER_TYPE,
            "parentId": realm_id,
            "config": {
                "priority": ["200"],
                "enabled": ["true"],
                "active": ["true"],
                "algorithm": ["RS256"],
                "keySize": ["3072"],
            },
        }
        if existing is None:
            await self._request(
                spec,
                "POST",
                path,
                token=token,
                json_body=desired,
                expected=frozenset({201}),
                code="keycloak_key_provider_create_failed",
            )
        else:
            component_id = _required_string(
                existing,
                "id",
                "keycloak_key_provider_read_failed",
            )
            updated = dict(desired)
            updated["id"] = component_id
            await self._request(
                spec,
                "PUT",
                f"{path}/{_path(component_id)}",
                token=token,
                json_body=updated,
                expected=frozenset({204}),
                code="keycloak_key_provider_update_failed",
            )

    async def _reconcile_management_roles(
        self,
        spec: StandaloneSSOBootstrapSpec,
        management_uuid: str,
        *,
        token: str,
    ) -> None:
        realm_path = f"/admin/realms/{_path(spec.realm)}"
        service_user = _object(
            await self._json(
                spec,
                f"{realm_path}/clients/{_path(management_uuid)}/service-account-user",
                token=token,
                code="keycloak_management_service_account_read_failed",
            ),
            "keycloak_management_service_account_read_failed",
        )
        service_user_id = _required_string(
            service_user,
            "id",
            "keycloak_management_service_account_read_failed",
        )
        realm_management = await self._exact_client(
            spec,
            spec.realm,
            "realm-management",
            token=token,
        )
        if realm_management is None:
            raise _fail("keycloak_realm_management_client_missing")
        realm_management_uuid = _required_string(
            realm_management,
            "id",
            "keycloak_realm_management_client_missing",
        )
        role_base = f"{realm_path}/clients/{_path(realm_management_uuid)}/roles"
        desired_roles: list[dict[str, Any]] = []
        for role_name in MANAGEMENT_REALM_ROLES:
            desired_roles.append(
                _object(
                    await self._json(
                        spec,
                        f"{role_base}/{_path(role_name)}",
                        token=token,
                        code="keycloak_management_role_missing",
                    ),
                    "keycloak_management_role_missing",
                )
            )
        mapping_path = (
            f"{realm_path}/users/{_path(service_user_id)}/role-mappings/clients/"
            f"{_path(realm_management_uuid)}"
        )
        current = _objects(
            await self._json(
                spec,
                mapping_path,
                token=token,
                code="keycloak_management_role_read_failed",
            ),
            "keycloak_management_role_read_failed",
        )
        current_by_name = {
            _required_string(item, "name", "keycloak_management_role_read_failed"): item
            for item in current
        }
        desired_names = set(MANAGEMENT_REALM_ROLES)
        extras = [item for name, item in current_by_name.items() if name not in desired_names]
        missing = [item for item in desired_roles if item.get("name") not in current_by_name]
        if extras:
            await self._request(
                spec,
                "DELETE",
                mapping_path,
                token=token,
                json_body=extras,
                expected=frozenset({204}),
                code="keycloak_management_role_remove_failed",
            )
        if missing:
            await self._request(
                spec,
                "POST",
                mapping_path,
                token=token,
                json_body=missing,
                expected=frozenset({204}),
                code="keycloak_management_role_assign_failed",
            )

        # With fullScopeAllowed=false, user role mappings alone are not put in
        # this client's access token. Scope exactly the same six
        # realm-management roles so the permanent token is usable without
        # exposing every role the service account might acquire later.
        scope_path = (
            f"{realm_path}/clients/{_path(management_uuid)}/scope-mappings/clients/"
            f"{_path(realm_management_uuid)}"
        )
        current_scope = _objects(
            await self._json(
                spec,
                scope_path,
                token=token,
                code="keycloak_management_scope_read_failed",
            ),
            "keycloak_management_scope_read_failed",
        )
        current_scope_by_name = {
            _required_string(item, "name", "keycloak_management_scope_read_failed"): item
            for item in current_scope
        }
        scope_extras = [
            item
            for name, item in current_scope_by_name.items()
            if name not in desired_names
        ]
        scope_missing = [
            item for item in desired_roles if item.get("name") not in current_scope_by_name
        ]
        if scope_extras:
            await self._request(
                spec,
                "DELETE",
                scope_path,
                token=token,
                json_body=scope_extras,
                expected=frozenset({204}),
                code="keycloak_management_scope_remove_failed",
            )
        if scope_missing:
            await self._request(
                spec,
                "POST",
                scope_path,
                token=token,
                json_body=scope_missing,
                expected=frozenset({204}),
                code="keycloak_management_scope_assign_failed",
            )

    async def _management_roles(
        self,
        spec: StandaloneSSOBootstrapSpec,
        management_uuid: str,
        *,
        token: str,
    ) -> tuple[str, ...]:
        realm_path = f"/admin/realms/{_path(spec.realm)}"
        service_user = _object(
            await self._json(
                spec,
                f"{realm_path}/clients/{_path(management_uuid)}/service-account-user",
                token=token,
                code="keycloak_management_service_account_read_failed",
            ),
            "keycloak_management_service_account_read_failed",
        )
        service_user_id = _required_string(
            service_user,
            "id",
            "keycloak_management_service_account_read_failed",
        )
        realm_management = await self._exact_client(
            spec,
            spec.realm,
            "realm-management",
            token=token,
        )
        if realm_management is None:
            raise _fail("keycloak_realm_management_client_missing")
        realm_management_uuid = _required_string(
            realm_management,
            "id",
            "keycloak_realm_management_client_missing",
        )
        roles = _objects(
            await self._json(
                spec,
                (
                    f"{realm_path}/users/{_path(service_user_id)}/role-mappings/clients/"
                    f"{_path(realm_management_uuid)}"
                ),
                token=token,
                code="keycloak_management_role_read_failed",
            ),
            "keycloak_management_role_read_failed",
        )
        return tuple(
            sorted(
                _required_string(item, "name", "keycloak_management_role_read_failed")
                for item in roles
            )
        )

    async def _management_scope_roles(
        self,
        spec: StandaloneSSOBootstrapSpec,
        management_uuid: str,
        *,
        token: str,
    ) -> tuple[str, ...]:
        realm_management = await self._exact_client(
            spec,
            spec.realm,
            "realm-management",
            token=token,
        )
        if realm_management is None:
            raise _fail("keycloak_realm_management_client_missing")
        realm_management_uuid = _required_string(
            realm_management,
            "id",
            "keycloak_realm_management_client_missing",
        )
        roles = _objects(
            await self._json(
                spec,
                (
                    f"/admin/realms/{_path(spec.realm)}/clients/"
                    f"{_path(management_uuid)}/scope-mappings/clients/"
                    f"{_path(realm_management_uuid)}"
                ),
                token=token,
                code="keycloak_management_scope_read_failed",
            ),
            "keycloak_management_scope_read_failed",
        )
        return tuple(
            sorted(
                _required_string(item, "name", "keycloak_management_scope_read_failed")
                for item in roles
            )
        )

    async def _exact_user(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> dict[str, Any] | None:
        value = await self._json(
            spec,
            f"/admin/realms/{_path(spec.realm)}/users",
            token=token,
            params={"username": spec.product_admin_username, "exact": "true"},
            code="keycloak_product_admin_read_failed",
        )
        return _exact(
            _objects(value, "keycloak_product_admin_read_failed"),
            "username",
            spec.product_admin_username,
            "keycloak_product_admin_duplicate",
        )

    async def _reconcile_product_admin(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> dict[str, Any]:
        existing = await self._exact_user(spec, token=token)
        if not spec.product_admin_password:
            raise _fail("keycloak_product_admin_password_unavailable")
        if existing is None:
            desired = {
                "username": spec.product_admin_username,
                "email": spec.product_admin_email,
                "enabled": True,
                "emailVerified": True,
                "firstName": "AKB",
                "lastName": "Product Administrator",
                "requiredActions": ["UPDATE_PASSWORD"],
                "credentials": [
                    {
                        "type": "password",
                        "value": spec.product_admin_password,
                        "temporary": True,
                    }
                ],
            }
            await self._request(
                spec,
                "POST",
                f"/admin/realms/{_path(spec.realm)}/users",
                token=token,
                json_body=desired,
                expected=frozenset({201}),
                code="keycloak_product_admin_create_failed",
            )
            user = await self._exact_user(spec, token=token)
        else:
            user = existing
        if user is None:
            raise _fail("keycloak_product_admin_readback_failed")
        if not self._product_admin_is_native(user):
            raise _fail("keycloak_admin_identity_is_federated")
        if not self._product_admin_matches(spec, user):
            raise _fail("keycloak_product_admin_readback_failed")
        if existing is not None:
            user_id = _required_string(
                user,
                "id",
                "keycloak_product_admin_readback_failed",
            )
            if await self._federated_identity_count(
                spec,
                user_id,
                token=token,
            ):
                # Never add a local recovery credential to a brokered user.
                # The dedicated product admin must remain realm-native.
                raise _fail("keycloak_admin_identity_is_federated")
            await self._request(
                spec,
                "PUT",
                (
                    f"/admin/realms/{_path(spec.realm)}/users/{_path(user_id)}"
                    "/reset-password"
                ),
                token=token,
                json_body={
                    "type": "password",
                    "value": spec.product_admin_password,
                    "temporary": True,
                },
                expected=frozenset({204}),
                code="keycloak_product_admin_password_reset_failed",
            )
            user = await self._exact_user(spec, token=token)
            if user is None:
                raise _fail("keycloak_product_admin_readback_failed")
            if not self._product_admin_is_native(user):
                raise _fail("keycloak_admin_identity_is_federated")
            if not self._product_admin_matches(spec, user):
                raise _fail("keycloak_product_admin_readback_failed")
        if user.get("requiredActions") != ["UPDATE_PASSWORD"]:
            raise _fail("keycloak_product_admin_update_password_missing")
        await self._require_product_admin_password(spec, user, token=token)
        return user

    @staticmethod
    def _product_admin_is_native(user: Mapping[str, object]) -> bool:
        return user.get("federationLink") in {None, ""}

    @staticmethod
    def _product_admin_matches(
        spec: StandaloneSSOBootstrapSpec,
        user: Mapping[str, object],
    ) -> bool:
        return (
            user.get("email") == spec.product_admin_email
            and user.get("enabled") is True
            and user.get("emailVerified") is True
        )

    async def _require_product_admin_password(
        self,
        spec: StandaloneSSOBootstrapSpec,
        user: Mapping[str, object],
        *,
        token: str,
    ) -> None:
        user_id = _required_string(user, "id", "keycloak_product_admin_readback_failed")
        credentials = _objects(
            await self._json(
                spec,
                f"/admin/realms/{_path(spec.realm)}/users/{_path(user_id)}/credentials",
                token=token,
                code="keycloak_product_admin_credential_read_failed",
            ),
            "keycloak_product_admin_credential_read_failed",
        )
        if not any(item.get("type") == "password" for item in credentials):
            raise _fail("keycloak_product_admin_password_missing")

    async def _federated_identity_count(
        self,
        spec: StandaloneSSOBootstrapSpec,
        user_id: str,
        *,
        token: str,
    ) -> int:
        value = await self._json(
            spec,
            (
                f"/admin/realms/{_path(spec.realm)}/users/{_path(user_id)}"
                "/federated-identity"
            ),
            token=token,
            code="keycloak_product_admin_federation_read_failed",
        )
        return len(_objects(value, "keycloak_product_admin_federation_read_failed"))

    async def _key_readback(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> tuple[str, int, int]:
        body = _object(
            await self._json(
                spec,
                f"/admin/realms/{_path(spec.realm)}/keys",
                token=token,
                code="keycloak_keys_read_failed",
            ),
            "keycloak_keys_read_failed",
        )
        active = body.get("active")
        if not isinstance(active, dict):
            raise _fail("keycloak_keys_read_failed")
        active_kid = active.get("RS256")
        if not isinstance(active_kid, str) or not active_kid:
            raise _fail("keycloak_active_rs256_key_missing")
        keys = _objects(body.get("keys"), "keycloak_keys_read_failed")
        matching = [
            item
            for item in keys
            if item.get("kid") == active_kid
            and item.get("algorithm") == "RS256"
            and str(item.get("use", "")).upper() == "SIG"
        ]
        if len(matching) != 1:
            raise _fail("keycloak_active_rs256_key_ambiguous")
        public_key = matching[0].get("publicKey")
        if not isinstance(public_key, str) or not public_key:
            raise _fail("keycloak_active_rs256_key_invalid")
        active_bits = _rsa_public_key_size(public_key)
        passive = sum(
            1
            for item in keys
            if item.get("algorithm") == "RS256"
            and str(item.get("use", "")).upper() == "SIG"
            and item.get("kid") != active_kid
            and str(item.get("status", "")).upper() != "DISABLED"
        )
        return active_kid, active_bits, passive

    @staticmethod
    def _selected_client_matches(
        actual: Mapping[str, object],
        expected: Mapping[str, object],
    ) -> bool:
        fields = (
            "clientId",
            "enabled",
            "protocol",
            "publicClient",
            "standardFlowEnabled",
            "directAccessGrantsEnabled",
            "serviceAccountsEnabled",
            "implicitFlowEnabled",
            "fullScopeAllowed",
            "redirectUris",
            "webOrigins",
        )
        if not all(actual.get(field) == expected.get(field) for field in fields):
            return False
        for field in ("defaultClientScopes", "optionalClientScopes"):
            actual_values = actual.get(field)
            expected_values = expected.get(field)
            if not isinstance(actual_values, list) or not isinstance(
                expected_values,
                list,
            ):
                return False
            if set(actual_values) != set(expected_values):
                return False
        actual_attributes = actual.get("attributes")
        expected_attributes = expected.get("attributes", {})
        if not isinstance(actual_attributes, dict) or not isinstance(
            expected_attributes,
            dict,
        ):
            return False
        return all(
            actual_attributes.get(key) == value
            for key, value in expected_attributes.items()
        )

    async def reconcile(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        bootstrap_token: str,
    ) -> StandaloneSSOReadback:
        realm = await self._reconcile_realm(spec, token=bootstrap_token)
        realm_id = _required_string(realm, "id", "keycloak_realm_readback_failed")
        await self._reconcile_signing_key(
            spec,
            realm_id,
            token=bootstrap_token,
        )
        await self._reconcile_native_amr(spec, token=bootstrap_token)

        api = await self._reconcile_client(
            spec,
            self._api_client(spec),
            token=bootstrap_token,
        )
        admin = await self._reconcile_client(
            spec,
            self._admin_client(spec),
            token=bootstrap_token,
        )
        management = await self._reconcile_client(
            spec,
            self._management_client(spec),
            token=bootstrap_token,
        )
        api_uuid = _required_string(api, "id", "keycloak_client_readback_failed")
        admin_uuid = _required_string(admin, "id", "keycloak_client_readback_failed")
        management_uuid = _required_string(
            management,
            "id",
            "keycloak_client_readback_failed",
        )
        await self._reconcile_mapper(
            spec,
            api_uuid,
            {
                "name": _API_AUDIENCE_MAPPER_NAME,
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "consentRequired": False,
                "config": {
                    "included.custom.audience": (
                        f"{spec.akb_public_url.rstrip('/')}/api"
                    ),
                    "id.token.claim": "false",
                    "access.token.claim": "true",
                    "lightweight.claim": "false",
                },
            },
            token=bootstrap_token,
        )
        await self._reconcile_mapper(
            spec,
            admin_uuid,
            {
                "name": _ADMIN_AMR_MAPPER_NAME,
                "protocol": "openid-connect",
                "protocolMapper": "oidc-amr-mapper",
                "consentRequired": False,
                "config": {
                    "id.token.claim": "true",
                    "access.token.claim": "false",
                    "lightweight.claim": "false",
                },
            },
            token=bootstrap_token,
        )
        await self._reconcile_management_roles(
            spec,
            management_uuid,
            token=bootstrap_token,
        )
        await self._reconcile_product_admin(spec, token=bootstrap_token)
        return await self.readback(spec, management_token=bootstrap_token)

    async def readback(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        management_token: str,
    ) -> StandaloneSSOReadback:
        realm = await self._realm(spec, token=management_token)
        if realm is None or not self._realm_matches(spec, realm):
            raise _fail("keycloak_realm_readback_failed")
        realm_id = _required_string(realm, "id", "keycloak_realm_readback_failed")

        expected_clients = (
            (self._api_client(spec), "api"),
            (self._admin_client(spec), "admin"),
            (self._management_client(spec), "management"),
        )
        clients: dict[str, dict[str, Any]] = {}
        for expected, role in expected_clients:
            client_id = _required_string(expected, "clientId", "keycloak_client_invalid")
            actual = await self._exact_client(
                spec,
                spec.realm,
                client_id,
                token=management_token,
            )
            if actual is None or not self._selected_client_matches(actual, expected):
                raise _fail("keycloak_client_readback_failed")
            clients[role] = actual
        api_uuid = _required_string(clients["api"], "id", "keycloak_client_readback_failed")
        admin_uuid = _required_string(
            clients["admin"],
            "id",
            "keycloak_client_readback_failed",
        )
        management_uuid = _required_string(
            clients["management"],
            "id",
            "keycloak_client_readback_failed",
        )

        api_mapper = _exact(
            await self._protocol_mappers(spec, api_uuid, token=management_token),
            "name",
            _API_AUDIENCE_MAPPER_NAME,
            "keycloak_mapper_duplicate",
        )
        if (
            api_mapper is None
            or api_mapper.get("protocolMapper") != "oidc-audience-mapper"
            or not isinstance(api_mapper.get("config"), dict)
            or api_mapper["config"].get("included.custom.audience")
            != f"{spec.akb_public_url.rstrip('/')}/api"
            or api_mapper["config"].get("id.token.claim") != "false"
            or api_mapper["config"].get("access.token.claim") != "true"
        ):
            raise _fail("keycloak_api_audience_readback_failed")
        admin_mapper = _exact(
            await self._protocol_mappers(spec, admin_uuid, token=management_token),
            "name",
            _ADMIN_AMR_MAPPER_NAME,
            "keycloak_mapper_duplicate",
        )
        if (
            admin_mapper is None
            or admin_mapper.get("protocolMapper") != "oidc-amr-mapper"
            or not isinstance(admin_mapper.get("config"), dict)
            or admin_mapper["config"].get("id.token.claim") != "true"
            or admin_mapper["config"].get("access.token.claim") != "false"
        ):
            raise _fail("keycloak_admin_amr_mapper_readback_failed")

        product_admin = await self._exact_user(spec, token=management_token)
        if product_admin is None:
            raise _fail("keycloak_product_admin_readback_failed")
        if not self._product_admin_is_native(product_admin):
            raise _fail("keycloak_admin_identity_is_federated")
        if not self._product_admin_matches(
            spec,
            product_admin,
        ):
            raise _fail("keycloak_product_admin_readback_failed")
        await self._require_product_admin_password(
            spec,
            product_admin,
            token=management_token,
        )
        product_admin_id = _required_string(
            product_admin,
            "id",
            "keycloak_product_admin_readback_failed",
        )
        federation_count = await self._federated_identity_count(
            spec,
            product_admin_id,
            token=management_token,
        )
        active_kid, active_bits, passive = await self._key_readback(
            spec,
            token=management_token,
        )
        native_amr = await self._native_amr_readback(
            spec,
            token=management_token,
        )
        roles = await self._management_roles(
            spec,
            management_uuid,
            token=management_token,
        )
        scope_roles = await self._management_scope_roles(
            spec,
            management_uuid,
            token=management_token,
        )
        return StandaloneSSOReadback(
            realm_id=realm_id,
            product_admin_subject=product_admin_id,
            admin_client_uuid=admin_uuid,
            management_client_uuid=management_uuid,
            api_client_uuid=api_uuid,
            active_signing_kid=active_kid,
            active_signing_bits=active_bits,
            passive_rs256_keys=passive,
            management_roles=roles,
            management_scope_roles=scope_roles,
            admin_native_amr=native_amr,
            product_admin_federated_identities=federation_count,
        )

    async def retire_bootstrap(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        bootstrap_token: str,
    ) -> None:
        if _CLIENT_ID_RE.fullmatch(spec.bootstrap_client_id) is None:
            raise _fail("keycloak_bootstrap_client_id_invalid")
        client = await self._exact_client(
            spec,
            "master",
            spec.bootstrap_client_id,
            token=bootstrap_token,
        )
        if client is None:
            raise _fail("keycloak_bootstrap_client_missing")
        client_uuid = _required_string(
            client,
            "id",
            "keycloak_bootstrap_client_invalid",
        )
        await self._request(
            spec,
            "DELETE",
            f"/admin/realms/master/clients/{_path(client_uuid)}",
            token=bootstrap_token,
            expected=frozenset({204}),
            code="keycloak_bootstrap_client_retire_failed",
        )

    async def assert_bootstrap_retired(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        bootstrap_token: str,
    ) -> None:
        for attempt in range(5):
            prior = await self._request(
                spec,
                "GET",
                "/admin/realms/master",
                token=bootstrap_token,
                expected=frozenset({200, 401, 403}),
                code="keycloak_bootstrap_retirement_check_failed",
            )
            prior_token_denied = prior.status_code in {401, 403}
            new_token_denied = await self.acquire_bootstrap(spec) is None
            if prior_token_denied and new_token_denied:
                return
            if attempt < 4:
                await asyncio.sleep(0.5)
        raise _fail("keycloak_bootstrap_client_still_active")
