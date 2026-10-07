"""Keycloak Admin REST adapter for the standalone SSO bootstrap lifecycle."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
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
    REALM_EVENT_TYPES,
    REALM_EVENTS_EXPIRATION_SECONDS,
    StandaloneSSOBootstrapError,
    StandaloneSSOBootstrapSpec,
    StandaloneSSOReadback,
)
from app.sso.brokered_account_guard import (
    BROKERED_ACCOUNT_MAPPER_NAME,
    BROKERED_ACCOUNT_ROLE,
    BROKERED_ACCOUNT_ROLE_DESCRIPTION,
    identity_provider_mapper,
    identity_provider_mapper_matches,
)


_KEY_PROVIDER_TYPE = "org.keycloak.keys.KeyProvider"
_ACTIVE_KEY_PROVIDER_NAME = "akb-rs256-3072-active"
_NATIVE_AMR_CONFIG_ALIAS = "akb-native-password-amr"
_API_AUDIENCE_MAPPER_NAME = "akb-api-audience"
_API_IDENTITY_PROVIDER_MAPPER_NAME = "akb-browser-identity-provider"
_ADMIN_AMR_MAPPER_NAME = "akb-admin-native-amr"
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_NATIVE_AMR_VALUES = {
    "default.reference.value": "pwd",
    "default.reference.maxAge": "300",
}
# The guarded browser flow is a copy, so the native-password reference the
# admin client's amr mapper reads has to live on the copy's password form too.
_GUARDED_NATIVE_AMR_CONFIG_ALIAS = "akb-browser-native-password-amr"
_BROKERED_ACCOUNT_CONDITION = "conditional-user-role"
_DENY_ACCESS = "deny-access-authenticator"
_BROKERED_ACCOUNT_DENY_MESSAGE = "This account signs in through its identity provider, not with a password."
_IDENTITY_PROVIDER_LIMIT = 100
_LINKED_ACCOUNT_PAGE = 100


@dataclass(frozen=True, slots=True)
class _GuardedFlow:
    """One password-accepting flow and the AKB-owned copy bound in its place.

    Keycloak refuses to add anything to a built-in flow, so the guard lives in
    a copy and the realm binds the copy instead.
    """

    base: str
    alias: str
    binding: str
    credential_step: str
    guard: str
    condition_config: str
    deny_config: str
    native_amr: bool


_GUARDED_FLOWS = (
    _GuardedFlow(
        base="browser",
        alias="akb browser",
        binding="browserFlow",
        credential_step="auth-username-password-form",
        guard="akb browser brokered-account guard",
        condition_config="akb-browser-brokered-account-condition",
        deny_config="akb-browser-brokered-account-deny",
        native_amr=True,
    ),
    # admin-cli accepts the password grant in every realm, and a realm that
    # lets clients register themselves lets anyone add another such client.
    _GuardedFlow(
        base="direct grant",
        alias="akb direct grant",
        binding="directGrantFlow",
        credential_step="direct-grant-validate-password",
        guard="akb direct grant brokered-account guard",
        condition_config="akb-direct-grant-brokered-account-condition",
        deny_config="akb-direct-grant-brokered-account-deny",
        native_amr=False,
    ),
)


@dataclass(slots=True)
class _ExecutionNode:
    entry: dict[str, Any]
    children: list[_ExecutionNode]


@dataclass(frozen=True, slots=True)
class _GuardLocation:
    credential_step: dict[str, Any]
    parent_alias: str
    guard: _ExecutionNode | None
    guard_after_password: bool


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


def _execution_tree(executions: list[dict[str, Any]]) -> list[_ExecutionNode]:
    """Rebuild the nesting Keycloak flattens into ``level`` order."""
    roots: list[_ExecutionNode] = []
    path: list[_ExecutionNode] = []
    for entry in executions:
        level = entry.get("level")
        if type(level) is not int or not 0 <= level <= len(path):
            raise _fail("keycloak_auth_flow_read_failed")
        node = _ExecutionNode(entry, [])
        del path[level:]
        (path[-1].children if path else roots).append(node)
        path.append(node)
    return roots


def _locate_guard(flow: _GuardedFlow, tree: list[_ExecutionNode]) -> _GuardLocation:
    """Find the one password authenticator and the guard beside it.

    The guard must be a sibling that runs after the password: a conditional
    sub-flow evaluated before anyone is identified has no account to test, so
    in front of the password it would let everyone through.
    """
    found: list[tuple[str, list[_ExecutionNode], _ExecutionNode]] = []

    def _walk(parent_alias: str, siblings: list[_ExecutionNode]) -> None:
        for node in siblings:
            if node.entry.get("authenticationFlow") is True:
                _walk(
                    _required_string(node.entry, "displayName", "keycloak_auth_flow_read_failed"),
                    node.children,
                )
            elif node.entry.get("providerId") == flow.credential_step:
                found.append((parent_alias, siblings, node))

    _walk(flow.alias, tree)
    if len(found) != 1:
        raise _fail("keycloak_brokered_account_guard_ambiguous")
    parent_alias, siblings, password = found[0]
    guards = [
        node
        for node in siblings
        if node.entry.get("authenticationFlow") is True and node.entry.get("displayName") == flow.guard
    ]
    if len(guards) > 1:
        raise _fail("keycloak_brokered_account_guard_ambiguous")
    guard = guards[0] if guards else None
    return _GuardLocation(
        credential_step=password.entry,
        parent_alias=parent_alias,
        guard=guard,
        guard_after_password=guard is not None and siblings.index(guard) > siblings.index(password),
    )


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

    async def acquire_upgrade(self, spec: StandaloneSSOBootstrapSpec) -> str | None:
        return await self._token(
            spec,
            realm="master",
            client_id=spec.upgrade_client_id,
            client_secret=spec.upgrade_client_secret,
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
            "registrationAllowed": spec.local_realm_self_registration,
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
        return all(realm.get(key) == value for key, value in cls._realm_profile(spec).items())

    @staticmethod
    def _realm_events_match(realm: Mapping[str, object]) -> bool:
        types = realm.get("enabledEventTypes")
        return (
            realm.get("eventsEnabled") is True
            and realm.get("eventsExpiration") == REALM_EVENTS_EXPIRATION_SECONDS
            and isinstance(types, list)
            and sorted(types) == list(REALM_EVENT_TYPES)
        )

    async def realm_events_converged(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        management_token: str,
    ) -> bool:
        """Read the event settings through view-realm; never change them."""
        realm = await self._realm(spec, token=management_token)
        if realm is None:
            raise _fail("keycloak_realm_readback_failed")
        return self._realm_events_match(realm)

    async def apply_realm_events(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> None:
        """Turn on the realm's user events under one-time authority.

        Keycloak's events/config update leaves the listeners and the admin-event
        settings alone when the request omits them, so only the three owned
        settings travel and the realm keeps whatever else it has configured.
        """
        await self._request(
            spec,
            "PUT",
            f"/admin/realms/{_path(spec.realm)}/events/config",
            token=token,
            json_body={
                "eventsEnabled": True,
                "eventsExpiration": REALM_EVENTS_EXPIRATION_SECONDS,
                "enabledEventTypes": list(REALM_EVENT_TYPES),
            },
            expected=frozenset({204}),
            code="keycloak_realm_events_update_failed",
        )
        realm = await self._realm(spec, token=token)
        if realm is None or not self._realm_events_match(realm):
            raise _fail("keycloak_realm_events_readback_failed")

    async def _brokered_account_role(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> dict[str, Any] | None:
        response = await self._request(
            spec,
            "GET",
            f"/admin/realms/{_path(spec.realm)}/roles/{_path(BROKERED_ACCOUNT_ROLE)}",
            token=token,
            expected=frozenset({200, 404}),
            code="keycloak_brokered_account_role_read_failed",
        )
        if response.status_code == 404:
            return None
        try:
            return _object(response.json(), "keycloak_brokered_account_role_read_failed")
        except (TypeError, ValueError) as exc:
            raise _fail("keycloak_brokered_account_role_read_failed") from exc

    async def _reconcile_brokered_account_role(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> dict[str, Any]:
        role = await self._brokered_account_role(spec, token=token)
        if role is None:
            await self._request(
                spec,
                "POST",
                f"/admin/realms/{_path(spec.realm)}/roles",
                token=token,
                json_body={
                    "name": BROKERED_ACCOUNT_ROLE,
                    "description": BROKERED_ACCOUNT_ROLE_DESCRIPTION,
                },
                expected=frozenset({201}),
                code="keycloak_brokered_account_role_create_failed",
            )
            role = await self._brokered_account_role(spec, token=token)
        if role is None or role.get("name") != BROKERED_ACCOUNT_ROLE:
            raise _fail("keycloak_brokered_account_role_read_failed")
        return role

    async def _guard_location(
        self,
        spec: StandaloneSSOBootstrapSpec,
        flow: _GuardedFlow,
        *,
        token: str,
    ) -> _GuardLocation:
        value = await self._json(
            spec,
            f"/admin/realms/{_path(spec.realm)}/authentication/flows/{_path(flow.alias)}/executions",
            token=token,
            code="keycloak_auth_flow_read_failed",
        )
        return _locate_guard(
            flow,
            _execution_tree(_objects(value, "keycloak_auth_flow_read_failed")),
        )

    async def _execution_config(
        self,
        spec: StandaloneSSOBootstrapSpec,
        execution: Mapping[str, object],
        *,
        token: str,
    ) -> dict[str, Any] | None:
        config_id = execution.get("authenticationConfig")
        if config_id is None:
            return None
        if not isinstance(config_id, str) or not config_id:
            raise _fail("keycloak_auth_flow_read_failed")
        return _object(
            await self._json(
                spec,
                f"/admin/realms/{_path(spec.realm)}/authentication/config/{_path(config_id)}",
                token=token,
                code="keycloak_auth_flow_read_failed",
            ),
            "keycloak_auth_flow_read_failed",
        )

    async def _reconcile_execution_config(
        self,
        spec: StandaloneSSOBootstrapSpec,
        execution: Mapping[str, object],
        *,
        alias: str,
        values: Mapping[str, str],
        token: str,
    ) -> None:
        execution_id = _required_string(execution, "id", "keycloak_auth_flow_read_failed")
        config_id = execution.get("authenticationConfig")
        desired: dict[str, Any] = {"alias": alias, "config": dict(values)}
        if config_id is None:
            await self._request(
                spec,
                "POST",
                f"/admin/realms/{_path(spec.realm)}/authentication/executions/{_path(execution_id)}/config",
                token=token,
                json_body=desired,
                expected=frozenset({201}),
                code="keycloak_brokered_account_guard_update_failed",
            )
        elif isinstance(config_id, str) and config_id:
            current = await self._execution_config(spec, execution, token=token)
            if current is not None and current.get("alias") == alias and current.get("config") == dict(values):
                return
            desired["id"] = config_id
            await self._request(
                spec,
                "PUT",
                f"/admin/realms/{_path(spec.realm)}/authentication/config/{_path(config_id)}",
                token=token,
                json_body=desired,
                expected=frozenset({204}),
                code="keycloak_brokered_account_guard_update_failed",
            )
        else:
            raise _fail("keycloak_auth_flow_read_failed")

    async def _set_requirement(
        self,
        spec: StandaloneSSOBootstrapSpec,
        parent_alias: str,
        execution: Mapping[str, object],
        requirement: str,
        *,
        token: str,
    ) -> None:
        if execution.get("requirement") == requirement:
            return
        await self._request(
            spec,
            "PUT",
            f"/admin/realms/{_path(spec.realm)}/authentication/flows/{_path(parent_alias)}/executions",
            token=token,
            json_body={**execution, "requirement": requirement},
            expected=frozenset({204}),
            code="keycloak_brokered_account_guard_update_failed",
        )

    async def _reconcile_guarded_flow(
        self,
        spec: StandaloneSSOBootstrapSpec,
        flow: _GuardedFlow,
        *,
        token: str,
    ) -> None:
        realm_path = f"/admin/realms/{_path(spec.realm)}"
        flows = _objects(
            await self._json(
                spec,
                f"{realm_path}/authentication/flows",
                token=token,
                code="keycloak_auth_flow_read_failed",
            ),
            "keycloak_auth_flow_read_failed",
        )
        existing = _exact(flows, "alias", flow.alias, "keycloak_brokered_account_guard_conflict")
        if existing is None:
            await self._request(
                spec,
                "POST",
                f"{realm_path}/authentication/flows/{_path(flow.base)}/copy",
                token=token,
                json_body={"newName": flow.alias},
                expected=frozenset({201}),
                code="keycloak_brokered_account_guard_flow_copy_failed",
            )
        elif (
            existing.get("builtIn") is not False
            or existing.get("topLevel") is not True
            or existing.get("providerId") != "basic-flow"
        ):
            raise _fail("keycloak_brokered_account_guard_conflict")

        location = await self._guard_location(spec, flow, token=token)
        if flow.native_amr:
            await self._reconcile_execution_config(
                spec,
                location.credential_step,
                alias=_GUARDED_NATIVE_AMR_CONFIG_ALIAS,
                values=_NATIVE_AMR_VALUES,
                token=token,
            )
        if location.guard is None:
            # A flow alias is unique in the realm, so 409 here means a sub-flow
            # of this name exists somewhere it would not guard anything.
            await self._request(
                spec,
                "POST",
                f"{realm_path}/authentication/flows/{_path(location.parent_alias)}/executions/flow",
                token=token,
                json_body={
                    "alias": flow.guard,
                    "type": "basic-flow",
                    "description": "Refuses a realm password to an account an identity provider brought.",
                },
                expected=frozenset({201}),
                code="keycloak_brokered_account_guard_conflict",
            )
            location = await self._guard_location(spec, flow, token=token)
        guard = location.guard
        if guard is None or not location.guard_after_password:
            raise _fail("keycloak_brokered_account_guard_conflict")
        providers = [child.entry.get("providerId") for child in guard.children]
        if (
            any(child.entry.get("authenticationFlow") is True for child in guard.children)
            or len(providers) != len(set(providers))
            or not set(providers) <= {_BROKERED_ACCOUNT_CONDITION, _DENY_ACCESS}
        ):
            raise _fail("keycloak_brokered_account_guard_conflict")
        for provider in (_BROKERED_ACCOUNT_CONDITION, _DENY_ACCESS):
            if provider not in providers:
                await self._request(
                    spec,
                    "POST",
                    f"{realm_path}/authentication/flows/{_path(flow.guard)}/executions/execution",
                    token=token,
                    json_body={"provider": provider},
                    expected=frozenset({201}),
                    code="keycloak_brokered_account_guard_update_failed",
                )
        location = await self._guard_location(spec, flow, token=token)
        guard = location.guard
        if guard is None:
            raise _fail("keycloak_brokered_account_guard_readback_failed")
        children = {child.entry.get("providerId"): child.entry for child in guard.children}
        if set(children) != {_BROKERED_ACCOUNT_CONDITION, _DENY_ACCESS}:
            raise _fail("keycloak_brokered_account_guard_readback_failed")
        await self._reconcile_execution_config(
            spec,
            children[_BROKERED_ACCOUNT_CONDITION],
            alias=flow.condition_config,
            values={"condUserRole": BROKERED_ACCOUNT_ROLE, "negate": "false"},
            token=token,
        )
        await self._reconcile_execution_config(
            spec,
            children[_DENY_ACCESS],
            alias=flow.deny_config,
            values={"denyErrorMessage": _BROKERED_ACCOUNT_DENY_MESSAGE},
            token=token,
        )
        # Keycloak creates a sub-flow and its executions disabled. Arm the two
        # executions before the sub-flow, so the guard never runs half-built.
        for provider in (_BROKERED_ACCOUNT_CONDITION, _DENY_ACCESS):
            await self._set_requirement(spec, flow.guard, children[provider], "REQUIRED", token=token)
        await self._set_requirement(spec, location.parent_alias, guard.entry, "CONDITIONAL", token=token)

    async def _identity_providers(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> list[dict[str, Any]]:
        providers = _objects(
            await self._json(
                spec,
                f"/admin/realms/{_path(spec.realm)}/identity-provider/instances",
                token=token,
                params={"first": 0, "max": _IDENTITY_PROVIDER_LIMIT + 1},
                code="keycloak_identity_provider_read_failed",
            ),
            "keycloak_identity_provider_read_failed",
        )
        if len(providers) > _IDENTITY_PROVIDER_LIMIT:
            raise _fail("keycloak_identity_provider_catalog_truncated")
        return providers

    async def _identity_provider_marks(
        self,
        spec: StandaloneSSOBootstrapSpec,
        alias: str,
        *,
        token: str,
    ) -> list[dict[str, Any]]:
        mappers = _objects(
            await self._json(
                spec,
                f"/admin/realms/{_path(spec.realm)}/identity-provider/instances/{_path(alias)}/mappers",
                token=token,
                code="keycloak_identity_provider_mapper_read_failed",
            ),
            "keycloak_identity_provider_mapper_read_failed",
        )
        return [item for item in mappers if item.get("name") == BROKERED_ACCOUNT_MAPPER_NAME]

    async def _reconcile_identity_provider_mark(
        self,
        spec: StandaloneSSOBootstrapSpec,
        alias: str,
        *,
        token: str,
    ) -> None:
        marks = await self._identity_provider_marks(spec, alias, token=token)
        if len(marks) > 1:
            raise _fail("keycloak_identity_provider_mapper_duplicate")
        base = f"/admin/realms/{_path(spec.realm)}/identity-provider/instances/{_path(alias)}/mappers"
        desired = identity_provider_mapper(alias)
        if not marks:
            await self._request(
                spec,
                "POST",
                base,
                token=token,
                json_body=desired,
                expected=frozenset({201}),
                code="keycloak_identity_provider_mapper_create_failed",
            )
        elif not identity_provider_mapper_matches(marks[0], alias):
            mapper_id = _required_string(marks[0], "id", "keycloak_identity_provider_mapper_read_failed")
            await self._request(
                spec,
                "PUT",
                f"{base}/{_path(mapper_id)}",
                token=token,
                json_body={**desired, "id": mapper_id},
                expected=frozenset({204}),
                code="keycloak_identity_provider_mapper_update_failed",
            )
        marks = await self._identity_provider_marks(spec, alias, token=token)
        if len(marks) != 1 or not identity_provider_mapper_matches(marks[0], alias):
            raise _fail("keycloak_identity_provider_mapper_readback_failed")

    async def _mark_linked_accounts(
        self,
        spec: StandaloneSSOBootstrapSpec,
        alias: str,
        role: Mapping[str, object],
        *,
        token: str,
    ) -> None:
        """Mark the accounts already linked to ``alias``.

        The mapper marks an account when it arrives. Someone linked before it
        existed who already made a password, and never arrives again, would
        otherwise keep that password; marking them here is what makes the
        guard apply to passwords that exist today.
        """
        realm_path = f"/admin/realms/{_path(spec.realm)}"
        marked: set[str] = set()
        first = 0
        while True:
            page = _objects(
                await self._json(
                    spec,
                    f"{realm_path}/users",
                    token=token,
                    params={
                        "idpAlias": alias,
                        "first": first,
                        "max": _LINKED_ACCOUNT_PAGE,
                        "briefRepresentation": "true",
                    },
                    code="keycloak_linked_account_read_failed",
                ),
                "keycloak_linked_account_read_failed",
            )
            fresh = [
                user_id
                for user_id in (_required_string(user, "id", "keycloak_linked_account_read_failed") for user in page)
                if user_id not in marked
            ]
            for user_id in fresh:
                await self._request(
                    spec,
                    "POST",
                    f"{realm_path}/users/{_path(user_id)}/role-mappings/realm",
                    token=token,
                    json_body=[dict(role)],
                    expected=frozenset({204}),
                    code="keycloak_linked_account_mark_failed",
                )
            marked.update(fresh)
            if len(page) < _LINKED_ACCOUNT_PAGE:
                return
            if not fresh:
                raise _fail("keycloak_linked_account_read_failed")
            first += _LINKED_ACCOUNT_PAGE

    async def _brokered_account_guard_readback(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> bool:
        realm = await self._realm(spec, token=token)
        if realm is None:
            raise _fail("keycloak_realm_readback_failed")
        if any(realm.get(flow.binding) != flow.alias for flow in _GUARDED_FLOWS):
            raise _fail("keycloak_brokered_account_guard_readback_failed")
        if await self._brokered_account_role(spec, token=token) is None:
            raise _fail("keycloak_brokered_account_guard_readback_failed")
        for flow in _GUARDED_FLOWS:
            location = await self._guard_location(spec, flow, token=token)
            guard = location.guard
            if guard is None or not location.guard_after_password or guard.entry.get("requirement") != "CONDITIONAL":
                raise _fail("keycloak_brokered_account_guard_readback_failed")
            children = {child.entry.get("providerId"): child.entry for child in guard.children}
            if (
                len(guard.children) != 2
                or set(children) != {_BROKERED_ACCOUNT_CONDITION, _DENY_ACCESS}
                or any(entry.get("requirement") != "REQUIRED" for entry in children.values())
            ):
                raise _fail("keycloak_brokered_account_guard_readback_failed")
            condition = await self._execution_config(spec, children[_BROKERED_ACCOUNT_CONDITION], token=token)
            values = None if condition is None else condition.get("config")
            if (
                not isinstance(values, dict)
                or values.get("condUserRole") != BROKERED_ACCOUNT_ROLE
                or values.get("negate") != "false"
            ):
                raise _fail("keycloak_brokered_account_guard_readback_failed")
            if flow.native_amr:
                native = await self._execution_config(spec, location.credential_step, token=token)
                if (
                    native is None
                    or native.get("alias") != _GUARDED_NATIVE_AMR_CONFIG_ALIAS
                    or native.get("config") != _NATIVE_AMR_VALUES
                ):
                    raise _fail("keycloak_brokered_account_guard_readback_failed")
        for provider in await self._identity_providers(spec, token=token):
            if provider.get("enabled") is not True:
                continue
            alias = _required_string(provider, "alias", "keycloak_identity_provider_read_failed")
            marks = await self._identity_provider_marks(spec, alias, token=token)
            if len(marks) != 1 or not identity_provider_mapper_matches(marks[0], alias):
                return False
        return True

    async def brokered_account_guard_readback(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        management_token: str,
    ) -> bool:
        """Read the guard through view-realm; never change it.

        A realm whose flows no longer refuse a brokered account's password is
        refused outright. Whether every enabled identity provider still marks
        the accounts it brings is returned, because an unmarked provider leaves
        only its newcomers outside the guard and is fixed through the provider
        control rather than by refusing to start.
        """
        return await self._brokered_account_guard_readback(spec, token=management_token)

    async def apply_brokered_account_guard(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
    ) -> None:
        """Install the brokered-account password guard under one-time authority.

        The realm keeps Keycloak's built-in flows; it binds AKB-owned copies
        whose password step is followed by a conditional sub-flow that denies
        an account holding the brokered-account role. Every identity provider
        gets the mapper that grants that role, and the accounts they already
        link are marked now. The copies are bound last, so a failure part-way
        leaves the realm on the flows it had.
        """
        realm = await self._realm(spec, token=token)
        if realm is None:
            raise _fail("keycloak_realm_read_failed")
        if any(realm.get(flow.binding) not in {flow.base, flow.alias} for flow in _GUARDED_FLOWS):
            # Someone bound a flow of their own. Replacing it would discard
            # their change, and guarding it would mean reasoning about a flow
            # AKB did not write, so stop and say so.
            raise _fail("keycloak_authentication_flow_binding_unexpected")
        role = await self._reconcile_brokered_account_role(spec, token=token)
        for flow in _GUARDED_FLOWS:
            await self._reconcile_guarded_flow(spec, flow, token=token)
        unbound = {flow.binding: flow.alias for flow in _GUARDED_FLOWS if realm.get(flow.binding) != flow.alias}
        if unbound:
            # Only the bindings travel; Keycloak's realm update leaves every
            # field the request omits as it is.
            await self._request(
                spec,
                "PUT",
                f"/admin/realms/{_path(spec.realm)}",
                token=token,
                json_body=unbound,
                expected=frozenset({204}),
                code="keycloak_brokered_account_guard_bind_failed",
            )
        for provider in await self._identity_providers(spec, token=token):
            alias = _required_string(provider, "alias", "keycloak_identity_provider_read_failed")
            await self._reconcile_identity_provider_mark(spec, alias, token=token)
            await self._mark_linked_accounts(spec, alias, role, token=token)
        if not await self._brokered_account_guard_readback(spec, token=token):
            raise _fail("keycloak_brokered_account_guard_readback_failed")

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
    def _api_client(
        spec: StandaloneSSOBootstrapSpec,
        *,
        backchannel_logout_uri: str | None = None,
    ) -> dict[str, Any]:
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
            "frontchannelLogout": False,
            "fullScopeAllowed": False,
            "redirectUris": [f"{spec.akb_public_url.rstrip('/')}/api/v1/auth/keycloak/callback"],
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
                "backchannel.logout.url": (
                    backchannel_logout_uri
                    if backchannel_logout_uri is not None
                    else spec.backchannel_logout_uri_effective
                ),
                "backchannel.logout.session.required": "true",
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
            "frontchannelLogout": False,
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
            "frontchannelLogout": False,
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
            (f"/admin/realms/{_path(spec.realm)}/clients/{_path(client_uuid)}/protocol-mappers/models"),
            token=token,
            code="keycloak_mapper_read_failed",
        )
        return _objects(value, "keycloak_mapper_read_failed")

    @staticmethod
    def _api_identity_provider_mapper() -> dict[str, Any]:
        """Project signed broker provenance from Keycloak's user session."""
        return {
            "name": _API_IDENTITY_PROVIDER_MAPPER_NAME,
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

    @staticmethod
    def _mapper_matches(
        actual: Mapping[str, object],
        expected: Mapping[str, object],
    ) -> bool:
        if any(
            actual.get(field) != expected.get(field)
            for field in ("name", "protocol", "protocolMapper", "consentRequired")
        ):
            return False
        actual_config = actual.get("config")
        expected_config = expected.get("config")
        if not isinstance(actual_config, dict) or not isinstance(expected_config, dict):
            return False
        return all(actual_config.get(key) == value for key, value in expected_config.items())

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
        base = f"/admin/realms/{_path(spec.realm)}/clients/{_path(client_uuid)}/protocol-mappers/models"
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
        matches = [item for item in executions if item.get("providerId") == "auth-username-password-form"]
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
                (f"/admin/realms/{_path(spec.realm)}/authentication/executions/{_path(execution_id)}/config"),
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
                (f"/admin/realms/{_path(spec.realm)}/authentication/config/{_path(config_id)}"),
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
                (f"/admin/realms/{_path(spec.realm)}/authentication/config/{_path(config_id)}"),
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
            f"{realm_path}/users/{_path(service_user_id)}/role-mappings/clients/{_path(realm_management_uuid)}"
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
            _required_string(item, "name", "keycloak_management_role_read_failed"): item for item in current
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
            f"{realm_path}/clients/{_path(management_uuid)}/scope-mappings/clients/{_path(realm_management_uuid)}"
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
            _required_string(item, "name", "keycloak_management_scope_read_failed"): item for item in current_scope
        }
        scope_extras = [item for name, item in current_scope_by_name.items() if name not in desired_names]
        scope_missing = [item for item in desired_roles if item.get("name") not in current_scope_by_name]
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
                (f"{realm_path}/users/{_path(service_user_id)}/role-mappings/clients/{_path(realm_management_uuid)}"),
                token=token,
                code="keycloak_management_role_read_failed",
            ),
            "keycloak_management_role_read_failed",
        )
        return tuple(sorted(_required_string(item, "name", "keycloak_management_role_read_failed") for item in roles))

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
        return tuple(sorted(_required_string(item, "name", "keycloak_management_scope_read_failed") for item in roles))

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
                (f"/admin/realms/{_path(spec.realm)}/users/{_path(user_id)}/reset-password"),
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
            (f"/admin/realms/{_path(spec.realm)}/users/{_path(user_id)}/federated-identity"),
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
            "frontchannelLogout",
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
        return all(actual_attributes.get(key) == value for key, value in expected_attributes.items())

    async def reconcile(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        bootstrap_token: str,
    ) -> StandaloneSSOReadback:
        realm = await self._reconcile_realm(spec, token=bootstrap_token)
        await self.apply_realm_events(spec, token=bootstrap_token)
        realm_id = _required_string(realm, "id", "keycloak_realm_readback_failed")
        await self._reconcile_signing_key(
            spec,
            realm_id,
            token=bootstrap_token,
        )
        await self._reconcile_native_amr(spec, token=bootstrap_token)
        await self.apply_brokered_account_guard(spec, token=bootstrap_token)

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
                    "included.custom.audience": (f"{spec.akb_public_url.rstrip('/')}/api"),
                    "id.token.claim": "false",
                    "access.token.claim": "true",
                    "lightweight.claim": "false",
                },
            },
            token=bootstrap_token,
        )
        await self._reconcile_mapper(
            spec,
            api_uuid,
            self._api_identity_provider_mapper(),
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

    async def _readback(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        management_token: str,
        require_identity_provider_mapper: bool,
        source_backchannel_logout_uri: str | None,
        allow_current_backchannel_logout: bool,
    ) -> StandaloneSSOReadback:
        realm = await self._realm(spec, token=management_token)
        if realm is None or not self._realm_matches(spec, realm):
            raise _fail("keycloak_realm_readback_failed")
        realm_id = _required_string(realm, "id", "keycloak_realm_readback_failed")

        expected_clients = (
            (
                self._api_client(
                    spec,
                    backchannel_logout_uri=source_backchannel_logout_uri,
                ),
                "api",
            ),
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
            matches = actual is not None and self._selected_client_matches(
                actual,
                expected,
            )
            if not matches and actual is not None and role == "api" and allow_current_backchannel_logout:
                # A crash after the one-time authority updated the client but
                # before receipt persistence must converge on retry. Accept
                # only the exact source or exact target representation.
                matches = self._selected_client_matches(
                    actual,
                    self._api_client(spec),
                )
            if not matches:
                raise _fail("keycloak_client_readback_failed")
            assert actual is not None
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

        api_mappers = await self._protocol_mappers(
            spec,
            api_uuid,
            token=management_token,
        )
        api_mapper = _exact(
            api_mappers,
            "name",
            _API_AUDIENCE_MAPPER_NAME,
            "keycloak_mapper_duplicate",
        )
        if (
            api_mapper is None
            or api_mapper.get("protocolMapper") != "oidc-audience-mapper"
            or not isinstance(api_mapper.get("config"), dict)
            or api_mapper["config"].get("included.custom.audience") != f"{spec.akb_public_url.rstrip('/')}/api"
            or api_mapper["config"].get("id.token.claim") != "false"
            or api_mapper["config"].get("access.token.claim") != "true"
        ):
            raise _fail("keycloak_api_audience_readback_failed")
        identity_provider_mapper = _exact(
            api_mappers,
            "name",
            _API_IDENTITY_PROVIDER_MAPPER_NAME,
            "keycloak_mapper_duplicate",
        )
        if require_identity_provider_mapper and (
            identity_provider_mapper is None
            or not self._mapper_matches(
                identity_provider_mapper,
                self._api_identity_provider_mapper(),
            )
        ):
            raise _fail("keycloak_api_identity_provider_mapper_readback_failed")
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
        # Steady-state read-back deliberately does not inspect the credential
        # list. It runs on every re-install, and by then the administrator may
        # have completed the forced password change or had the credential
        # reset; whether one is present now is not evidence about this
        # installation's shape, and requiring it here would refuse a converged
        # realm on the next redeploy.
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

    async def readback(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        management_token: str,
    ) -> StandaloneSSOReadback:
        return await self._readback(
            spec,
            management_token=management_token,
            require_identity_provider_mapper=True,
            source_backchannel_logout_uri=None,
            allow_current_backchannel_logout=False,
        )

    async def readback_legacy_v1(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        management_token: str,
    ) -> StandaloneSSOReadback:
        return await self._readback(
            spec,
            management_token=management_token,
            require_identity_provider_mapper=False,
            source_backchannel_logout_uri=spec.legacy_backchannel_logout_uri,
            allow_current_backchannel_logout=True,
        )

    async def readback_legacy_v2(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        management_token: str,
    ) -> StandaloneSSOReadback:
        return await self._readback(
            spec,
            management_token=management_token,
            require_identity_provider_mapper=True,
            source_backchannel_logout_uri=spec.legacy_backchannel_logout_uri,
            allow_current_backchannel_logout=True,
        )

    async def readback_callback_migration(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        source_backchannel_logout_uri: str,
        management_token: str,
    ) -> StandaloneSSOReadback:
        return await self._readback(
            spec,
            management_token=management_token,
            require_identity_provider_mapper=True,
            source_backchannel_logout_uri=source_backchannel_logout_uri,
            allow_current_backchannel_logout=True,
        )

    async def upgrade_legacy_to_current(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        upgrade_token: str,
    ) -> StandaloneSSOReadback:
        expected_api = self._api_client(
            spec,
            backchannel_logout_uri=spec.legacy_backchannel_logout_uri,
        )
        api = await self._exact_client(
            spec,
            spec.realm,
            spec.api_client_id,
            token=upgrade_token,
        )
        if api is None or not (
            self._selected_client_matches(api, expected_api)
            or self._selected_client_matches(api, self._api_client(spec))
        ):
            raise _fail("keycloak_client_readback_failed")
        reconciled = await self._reconcile_client(
            spec,
            self._api_client(spec),
            token=upgrade_token,
        )
        api_uuid = _required_string(
            reconciled,
            "id",
            "keycloak_client_readback_failed",
        )
        await self._reconcile_mapper(
            spec,
            api_uuid,
            self._api_identity_provider_mapper(),
            token=upgrade_token,
        )
        return await self.readback(
            spec,
            management_token=upgrade_token,
        )

    async def upgrade_callback_to_current(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        source_backchannel_logout_uri: str,
        upgrade_token: str,
    ) -> StandaloneSSOReadback:
        expected_source = self._api_client(
            spec,
            backchannel_logout_uri=source_backchannel_logout_uri,
        )
        api = await self._exact_client(
            spec,
            spec.realm,
            spec.api_client_id,
            token=upgrade_token,
        )
        if api is None or not (
            self._selected_client_matches(api, expected_source)
            or self._selected_client_matches(api, self._api_client(spec))
        ):
            raise _fail("keycloak_client_readback_failed")
        await self._reconcile_client(
            spec,
            self._api_client(spec),
            token=upgrade_token,
        )
        return await self.readback(
            spec,
            management_token=upgrade_token,
        )

    async def _retire_one_time_client(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        client_id: str,
        token: str,
        missing_code: str,
        invalid_code: str,
        retire_code: str,
    ) -> None:
        if _CLIENT_ID_RE.fullmatch(client_id) is None:
            raise _fail(invalid_code)
        client = await self._exact_client(
            spec,
            "master",
            client_id,
            token=token,
        )
        if client is None:
            raise _fail(missing_code)
        client_uuid = _required_string(
            client,
            "id",
            invalid_code,
        )
        await self._request(
            spec,
            "DELETE",
            f"/admin/realms/master/clients/{_path(client_uuid)}",
            token=token,
            expected=frozenset({204}),
            code=retire_code,
        )

    async def _assert_one_time_client_retired(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        token: str,
        acquire_token: Callable[[], Awaitable[str | None]],
        check_code: str,
        still_active_code: str,
    ) -> None:
        for attempt in range(5):
            prior = await self._request(
                spec,
                "GET",
                "/admin/realms/master",
                token=token,
                expected=frozenset({200, 401, 403}),
                code=check_code,
            )
            prior_token_denied = prior.status_code in {401, 403}
            new_token_denied = await acquire_token() is None
            if prior_token_denied and new_token_denied:
                return
            if attempt < 4:
                await asyncio.sleep(0.5)
        raise _fail(still_active_code)

    async def retire_bootstrap(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        bootstrap_token: str,
    ) -> None:
        await self._retire_one_time_client(
            spec,
            client_id=spec.bootstrap_client_id,
            token=bootstrap_token,
            missing_code="keycloak_bootstrap_client_missing",
            invalid_code="keycloak_bootstrap_client_invalid",
            retire_code="keycloak_bootstrap_client_retire_failed",
        )

    async def assert_bootstrap_retired(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        bootstrap_token: str,
    ) -> None:
        await self._assert_one_time_client_retired(
            spec,
            token=bootstrap_token,
            acquire_token=lambda: self.acquire_bootstrap(spec),
            check_code="keycloak_bootstrap_retirement_check_failed",
            still_active_code="keycloak_bootstrap_client_still_active",
        )

    async def retire_upgrade(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        upgrade_token: str,
    ) -> None:
        await self._retire_one_time_client(
            spec,
            client_id=spec.upgrade_client_id,
            token=upgrade_token,
            missing_code="keycloak_upgrade_client_missing",
            invalid_code="keycloak_upgrade_client_invalid",
            retire_code="keycloak_upgrade_client_retire_failed",
        )

    async def assert_upgrade_retired(
        self,
        spec: StandaloneSSOBootstrapSpec,
        *,
        upgrade_token: str,
    ) -> None:
        await self._assert_one_time_client_retired(
            spec,
            token=upgrade_token,
            acquire_token=lambda: self.acquire_upgrade(spec),
            check_code="keycloak_upgrade_retirement_check_failed",
            still_active_code="keycloak_upgrade_client_still_active",
        )
