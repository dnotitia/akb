"""Narrow Keycloak Admin REST control for built-in upstream SSO providers.

The adapter accepts provider-neutral specifications and renders only the
code-reviewed representation from the explicit registry. It never accepts an
arbitrary Keycloak JSON document, returns a secret, or includes upstream
response bodies in an error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.sso.models import (
    IdentityPrelinkReadback,
    ProviderConfigureSpec,
    ProviderMutationReadback,
    ProviderReadback,
)
from app.sso.providers import keycloak_oidc
from app.sso.providers.keycloak_oidc import ProviderDefinitionError
from app.sso.registry import ProviderDefinition, provider_definition


_CATALOG_LIMIT = 100
_CACHE_FRESH_SECONDS = 15.0
_CACHE_STALE_SECONDS = 60.0
_FAILURE_BACKOFF_SECONDS = 5.0
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_SUBJECT_LENGTH = 1024


class ProviderControlError(RuntimeError):
    """Value-less control-plane failure safe for logs and API responses."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> ProviderControlError:
    return ProviderControlError(code)


def _path(value: str) -> str:
    return quote(value, safe="")


def _opaque_subject(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_SUBJECT_LENGTH
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise _fail("identity_prelink_subject_invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _fail("identity_prelink_subject_invalid") from None
    # OIDC subjects are opaque and case-sensitive.  Whitespace is not
    # normalized: it either identifies the exact Keycloak user/link or fails.
    return value


def _object(value: object, *, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(code)
    return value


def _objects(value: object, *, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise _fail(code)
    return value


def _same_provider_configuration(
    before: ProviderReadback,
    after: ProviderReadback,
) -> bool:
    """Compare identity-affecting fields while ignoring the enabled state."""
    return (
        before.provider_type,
        before.alias,
        before.display_name,
        before.issuer,
        before.discovery_url,
        before.client_id,
        before.client_secret_configured,
        before.redirect_uri,
        before.supports_logout,
        before.supports_identity_migration,
    ) == (
        after.provider_type,
        after.alias,
        after.display_name,
        after.issuer,
        after.discovery_url,
        after.client_id,
        after.client_secret_configured,
        after.redirect_uri,
        after.supports_logout,
        after.supports_identity_migration,
    )


@dataclass(frozen=True, slots=True)
class KeycloakAdminConfig:
    internal_base_url: str
    public_base_url: str
    realm: str
    management_client_id: str
    management_client_secret: str = field(repr=False)
    verify_ssl: bool

    @property
    def broker_issuer(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/realms/{self.realm}"


class KeycloakProviderControl:
    """Read, configure, and toggle only AKB-marked provider instances."""

    def __init__(
        self,
        config: KeycloakAdminConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._transport = transport
        self._monotonic = monotonic
        self._catalog: tuple[ProviderReadback, ...] | None = None
        self._catalog_at = 0.0
        self._catalog_error_code: str | None = None
        self._catalog_error_at = 0.0
        self._catalog_lock = asyncio.Lock()

    @property
    def control_mode(self) -> str:
        if (
            self.config.management_client_id.strip()
            and self.config.management_client_secret
        ):
            return "direct"
        return "delegated"

    def _require_direct(self) -> None:
        if self.control_mode != "direct":
            raise _fail("keycloak_provider_control_delegated")
        if (
            not self.config.internal_base_url.strip()
            or not self.config.public_base_url.strip()
            or not self.config.realm.strip()
        ):
            raise _fail("keycloak_provider_control_invalid")

    @asynccontextmanager
    async def _client(self):
        base_url = f"{self.config.internal_base_url.rstrip('/')}/"
        async with httpx.AsyncClient(
            base_url=base_url,
            verify=self.config.verify_ssl,
            timeout=httpx.Timeout(20.0, connect=10.0),
            transport=self._transport,
        ) as client:
            yield client

    @staticmethod
    def _json(response: httpx.Response, *, code: str) -> object:
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise _fail(code) from exc

    async def _bounded_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        code: str,
        headers: Mapping[str, str] | None = None,
        json_body: object | None = None,
        form_body: Mapping[str, str] | None = None,
        params: Mapping[str, str | int | bool] | None = None,
    ) -> httpx.Response:
        request = client.build_request(
            method,
            path,
            headers=headers,
            json=json_body,
            data=form_body,
            params=params,
        )
        response: httpx.Response | None = None
        try:
            response = await client.send(request, stream=True)
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise _fail(code)
                content.extend(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=bytes(content),
                request=request,
            )
        except ProviderControlError:
            raise
        except httpx.HTTPError as exc:
            raise _fail("keycloak_provider_control_unreachable") from exc
        finally:
            if response is not None:
                await response.aclose()

    async def _token(self, client: httpx.AsyncClient) -> str:
        response = await self._bounded_request(
            client,
            "POST",
            (
                f"/realms/{_path(self.config.realm)}"
                "/protocol/openid-connect/token"
            ),
            code="keycloak_provider_token_invalid",
            form_body={
                "grant_type": "client_credentials",
                "client_id": self.config.management_client_id,
                "client_secret": self.config.management_client_secret,
            },
        )
        if response.status_code != 200:
            raise _fail("keycloak_provider_control_unauthorized")
        body = _object(
            self._json(response, code="keycloak_provider_token_invalid"),
            code="keycloak_provider_token_invalid",
        )
        token = body.get("access_token")
        if not isinstance(token, str) or not token or len(token) > 16_384:
            raise _fail("keycloak_provider_token_invalid")
        return token

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        token: str,
        expected: frozenset[int],
        code: str,
        json_body: object | None = None,
        params: Mapping[str, str | int | bool] | None = None,
    ) -> httpx.Response:
        response = await self._bounded_request(
            client,
            method,
            path,
            code=code,
            headers={"Authorization": f"Bearer {token}"},
            json_body=json_body,
            params=params,
        )
        if response.status_code not in expected:
            raise _fail(code)
        return response

    def _instances_path(self) -> str:
        return (
            f"/admin/realms/{_path(self.config.realm)}"
            "/identity-provider/instances"
        )

    async def _get_exact(
        self,
        client: httpx.AsyncClient,
        token: str,
        alias: str,
    ) -> dict[str, Any] | None:
        response = await self._request(
            client,
            "GET",
            f"{self._instances_path()}/{_path(alias)}",
            token=token,
            expected=frozenset({200, 404}),
            code="keycloak_provider_read_failed",
        )
        if response.status_code == 404:
            return None
        return _object(
            self._json(response, code="keycloak_provider_read_failed"),
            code="keycloak_provider_read_failed",
        )

    @staticmethod
    def _definition_for_representation(
        representation: Mapping[str, object],
    ) -> ProviderDefinition | None:
        config = representation.get("config")
        if not isinstance(config, dict):
            return None
        provider_type = config.get(keycloak_oidc.MARKER_TYPE_KEY)
        if not isinstance(provider_type, str):
            return None
        try:
            definition = provider_definition(provider_type)
        except ValueError:
            return None
        if not definition.module.is_managed_representation(representation):
            return None
        return definition

    def _readback(
        self,
        definition: ProviderDefinition,
        representation: Mapping[str, object],
    ) -> ProviderReadback:
        return definition.module.readback(
            representation,
            broker_base_url=self.config.public_base_url,
            broker_issuer=self.config.broker_issuer,
            realm=self.config.realm,
        )

    async def _fetch_catalog(self) -> tuple[ProviderReadback, ...]:
        self._require_direct()
        async with self._client() as client:
            token = await self._token(client)
            response = await self._request(
                client,
                "GET",
                self._instances_path(),
                token=token,
                params={"first": 0, "max": _CATALOG_LIMIT + 1, "realmOnly": True},
                expected=frozenset({200}),
                code="keycloak_provider_list_failed",
            )
            values = _objects(
                self._json(response, code="keycloak_provider_list_failed"),
                code="keycloak_provider_list_failed",
            )
        if len(values) > _CATALOG_LIMIT:
            raise _fail("keycloak_provider_catalog_truncated")
        providers: list[ProviderReadback] = []
        aliases: set[str] = set()
        for value in values:
            definition = self._definition_for_representation(value)
            if definition is None:
                continue
            provider = self._readback(definition, value)
            if not provider.alias or provider.alias in aliases:
                raise _fail("keycloak_provider_catalog_invalid")
            aliases.add(provider.alias)
            providers.append(provider)
        return tuple(sorted(providers, key=lambda item: item.alias))

    async def list_providers(
        self,
        *,
        force_refresh: bool = False,
        allow_stale: bool = False,
    ) -> tuple[ProviderReadback, ...]:
        self._require_direct()
        now = self._monotonic()
        if (
            not force_refresh
            and self._catalog is not None
            and now - self._catalog_at <= _CACHE_FRESH_SECONDS
        ):
            return self._catalog
        if (
            not force_refresh
            and self._catalog_error_code is not None
            and now - self._catalog_error_at <= _FAILURE_BACKOFF_SECONDS
        ):
            stale_limit = _CACHE_FRESH_SECONDS + _CACHE_STALE_SECONDS
            if (
                allow_stale
                and self._catalog is not None
                and now - self._catalog_at <= stale_limit
            ):
                return self._catalog
            raise _fail(self._catalog_error_code)
        async with self._catalog_lock:
            now = self._monotonic()
            if (
                not force_refresh
                and self._catalog is not None
                and now - self._catalog_at <= _CACHE_FRESH_SECONDS
            ):
                return self._catalog
            if (
                not force_refresh
                and self._catalog_error_code is not None
                and now - self._catalog_error_at <= _FAILURE_BACKOFF_SECONDS
            ):
                stale_limit = _CACHE_FRESH_SECONDS + _CACHE_STALE_SECONDS
                if (
                    allow_stale
                    and self._catalog is not None
                    and now - self._catalog_at <= stale_limit
                ):
                    return self._catalog
                raise _fail(self._catalog_error_code)
            try:
                catalog = await self._fetch_catalog()
            except ProviderControlError as error:
                now = self._monotonic()
                self._catalog_error_code = error.code
                self._catalog_error_at = now
                stale_limit = _CACHE_FRESH_SECONDS + _CACHE_STALE_SECONDS
                if (
                    allow_stale
                    and not force_refresh
                    and self._catalog is not None
                    and now - self._catalog_at <= stale_limit
                ):
                    return self._catalog
                raise
            self._catalog = catalog
            self._catalog_at = self._monotonic()
            self._catalog_error_code = None
            self._catalog_error_at = 0.0
            return catalog

    async def _import_discovery(
        self,
        client: httpx.AsyncClient,
        token: str,
        definition: ProviderDefinition,
        spec: ProviderConfigureSpec,
    ) -> dict[str, Any]:
        response = await self._request(
            client,
            "POST",
            (
                f"/admin/realms/{_path(self.config.realm)}"
                "/identity-provider/import-config"
            ),
            token=token,
            json_body={
                "providerId": definition.module.KEYCLOAK_PROVIDER_ID,
                "fromUrl": spec.discovery_url,
            },
            expected=frozenset({200}),
            code="keycloak_provider_discovery_import_failed",
        )
        return _object(
            self._json(response, code="keycloak_provider_discovery_import_failed"),
            code="keycloak_provider_discovery_import_failed",
        )

    async def configure(
        self,
        spec: ProviderConfigureSpec,
    ) -> ProviderMutationReadback:
        self._require_direct()
        try:
            definition = provider_definition(spec.provider_type)
            validated = definition.module.validate_spec(
                spec,
                broker_issuer=self.config.broker_issuer,
            )
        except (ValueError, ProviderDefinitionError) as exc:
            code = getattr(exc, "code", "unsupported_sso_provider_type")
            raise _fail(code) from exc

        async with self._client() as client:
            token = await self._token(client)
            existing = await self._get_exact(client, token, validated.alias)
            preserve_secret = False
            existing_readback: ProviderReadback | None = None
            if existing is None:
                if validated.client_secret is None:
                    raise _fail("provider_client_secret_required")
            else:
                existing_definition = self._definition_for_representation(existing)
                if (
                    existing_definition is None
                    or existing_definition.provider_type != definition.provider_type
                ):
                    raise _fail("provider_alias_conflict")
                existing_readback = self._readback(existing_definition, existing)
                if existing.get("enabled") is True:
                    raise _fail("provider_disable_before_reconfigure")
                if validated.client_secret is None:
                    if not existing_readback.client_secret_configured:
                        raise _fail("provider_client_secret_required")
                    preserve_secret = True

            imported = await self._import_discovery(
                client,
                token,
                definition,
                validated,
            )
            try:
                desired = definition.module.render_representation(
                    validated,
                    imported,
                    preserve_secret=preserve_secret,
                )
            except ProviderDefinitionError as exc:
                raise _fail(exc.code) from exc
            if existing is None:
                await self._request(
                    client,
                    "POST",
                    self._instances_path(),
                    token=token,
                    json_body=desired,
                    expected=frozenset({201}),
                    code="keycloak_provider_create_failed",
                )
            else:
                await self._request(
                    client,
                    "PUT",
                    f"{self._instances_path()}/{_path(validated.alias)}",
                    token=token,
                    json_body=desired,
                    expected=frozenset({204}),
                    code="keycloak_provider_update_failed",
                )
            representation = await self._get_exact(client, token, validated.alias)
        if representation is None:
            raise _fail("keycloak_provider_readback_failed")
        readback = self._readback(definition, representation)
        if (
            readback.alias != validated.alias
            or readback.display_name != validated.display_name
            or readback.issuer != validated.issuer
            or readback.discovery_url != validated.discovery_url
            or readback.client_id != validated.client_id
            or not readback.client_secret_configured
            or readback.state != "configured_disabled"
        ):
            raise _fail("keycloak_provider_readback_failed")
        self._catalog = None
        self._catalog_at = 0.0
        self._catalog_error_code = None
        self._catalog_error_at = 0.0
        return ProviderMutationReadback(before=existing_readback, after=readback)

    async def set_enabled(
        self,
        alias: str,
        *,
        enabled: bool,
    ) -> ProviderMutationReadback:
        self._require_direct()
        try:
            validated_alias = keycloak_oidc.validate_alias(alias)
        except ProviderDefinitionError as exc:
            raise _fail(exc.code) from exc
        async with self._client() as client:
            token = await self._token(client)
            representation = await self._get_exact(client, token, validated_alias)
            if representation is None:
                raise _fail("provider_not_found")
            definition = self._definition_for_representation(representation)
            if definition is None:
                raise _fail("provider_alias_conflict")
            current = self._readback(definition, representation)
            if enabled and current.state == "configuration_error":
                raise _fail("provider_configuration_invalid")

            already_desired = (
                representation.get("enabled") is enabled
                and representation.get("hideOnLogin") is (not enabled)
            )
            if not already_desired:
                desired = definition.module.with_enabled(
                    representation,
                    enabled=enabled,
                )
                await self._request(
                    client,
                    "PUT",
                    f"{self._instances_path()}/{_path(validated_alias)}",
                    token=token,
                    json_body=desired,
                    expected=frozenset({204}),
                    code="keycloak_provider_toggle_failed",
                )
                representation = await self._get_exact(client, token, validated_alias)
                if representation is None:
                    raise _fail("keycloak_provider_readback_failed")
            readback = self._readback(definition, representation)
        if readback.alias != validated_alias:
            raise _fail("keycloak_provider_readback_failed")
        if not _same_provider_configuration(current, readback):
            raise _fail("keycloak_provider_readback_failed")
        if enabled and readback.state != "enabled":
            raise _fail("keycloak_provider_readback_failed")
        if not enabled and not (
            representation.get("enabled") is False
            and representation.get("hideOnLogin") is True
        ):
            raise _fail("keycloak_provider_readback_failed")
        self._catalog = None
        self._catalog_at = 0.0
        self._catalog_error_code = None
        self._catalog_error_at = 0.0
        return ProviderMutationReadback(before=current, after=readback)

    async def verify_identity_prelink(
        self,
        alias: str,
        *,
        broker_subject: str,
        upstream_subject: str,
    ) -> IdentityPrelinkReadback:
        """Verify a one-time operator-created broker link without mutating it.

        The permanent AKB management client needs only read access to users and
        federated identities.  Creating the native broker user and attaching
        the federated identity remain explicit Keycloak-operator actions.
        """

        self._require_direct()
        try:
            validated_alias = keycloak_oidc.validate_alias(alias)
        except ProviderDefinitionError as exc:
            raise _fail(exc.code) from exc
        broker_subject = _opaque_subject(broker_subject)
        upstream_subject = _opaque_subject(upstream_subject)

        async with self._client() as client:
            token = await self._token(client)
            representation = await self._get_exact(client, token, validated_alias)
            if representation is None:
                raise _fail("provider_not_found")
            definition = self._definition_for_representation(representation)
            if definition is None:
                raise _fail("provider_alias_conflict")
            provider = self._readback(definition, representation)
            if (
                provider.state == "configuration_error"
                or provider.issuer is None
                or not provider.supports_identity_migration
            ):
                raise _fail("provider_configuration_invalid")

            user_base = (
                f"/admin/realms/{_path(self.config.realm)}/users/"
                f"{_path(broker_subject)}"
            )
            user_response = await self._request(
                client,
                "GET",
                user_base,
                token=token,
                expected=frozenset({200, 404}),
                code="identity_prelink_read_failed",
            )
            if user_response.status_code == 404:
                raise _fail("identity_prelink_user_not_found")
            user = _object(
                self._json(user_response, code="identity_prelink_read_failed"),
                code="identity_prelink_read_failed",
            )
            if user.get("id") != broker_subject:
                raise _fail("identity_prelink_user_mismatch")
            if user.get("enabled") is not True:
                raise _fail("identity_prelink_user_inactive")
            federation_link = user.get("federationLink")
            if federation_link not in (None, ""):
                raise _fail("identity_prelink_user_not_native")
            username = user.get("username")
            if (
                not isinstance(username, str)
                or not username
                or len(username) > 255
                or any(
                    ord(character) < 0x20 or ord(character) == 0x7F
                    for character in username
                )
            ):
                raise _fail("identity_prelink_read_failed")
            required_actions = user.get("requiredActions", [])
            if required_actions is None:
                required_actions = []
            if not isinstance(required_actions, list) or any(
                not isinstance(action, str) or not action
                for action in required_actions
            ):
                raise _fail("identity_prelink_read_failed")
            if required_actions:
                # UPDATE_PASSWORD, CONFIGURE_TOTP, and WebAuthn registration
                # can mint a local login authority after this check.
                raise _fail("identity_prelink_local_credential_present")

            credential_response = await self._request(
                client,
                "GET",
                f"{user_base}/credentials",
                token=token,
                expected=frozenset({200}),
                code="identity_prelink_read_failed",
            )
            credentials = _objects(
                self._json(
                    credential_response,
                    code="identity_prelink_read_failed",
                ),
                code="identity_prelink_read_failed",
            )
            if credentials:
                # Passwordless WebAuthn, OTP, and recovery credentials are
                # local login authorities too.  A migration broker user must
                # be reachable only through its exact upstream federation.
                raise _fail("identity_prelink_local_credential_present")

            link_response = await self._request(
                client,
                "GET",
                f"{user_base}/federated-identity",
                token=token,
                expected=frozenset({200}),
                code="identity_prelink_read_failed",
            )
            links = _objects(
                self._json(link_response, code="identity_prelink_read_failed"),
                code="identity_prelink_read_failed",
            )
            if not links:
                raise _fail("identity_prelink_missing")
            if len(links) != 1:
                raise _fail("identity_prelink_ambiguous")
            link = links[0]
            if link.get("identityProvider") != validated_alias:
                raise _fail("identity_prelink_missing")
            if link.get("userId") != upstream_subject:
                raise _fail("identity_prelink_subject_mismatch")
            linked_username = link.get("userName")
            if not isinstance(linked_username, str) or not linked_username:
                raise _fail("identity_prelink_read_failed")

        return IdentityPrelinkReadback(
            provider_alias=validated_alias,
            provider_state=provider.state,
            upstream_issuer=provider.issuer,
            broker_issuer=self.config.broker_issuer,
            broker_subject=broker_subject,
            upstream_subject=upstream_subject,
            broker_username=username,
        )


def _runtime_config() -> KeycloakAdminConfig:
    from app.config import settings

    return KeycloakAdminConfig(
        internal_base_url=(
            settings.keycloak_internal_url or settings.keycloak_server_url
        ),
        public_base_url=settings.keycloak_server_url,
        realm=settings.keycloak_realm,
        management_client_id=settings.keycloak_management_client_id,
        management_client_secret=settings.keycloak_management_client_secret,
        verify_ssl=settings.keycloak_verify_ssl,
    )


_runtime_control: KeycloakProviderControl | None = None
_runtime_control_config: KeycloakAdminConfig | None = None


def get_keycloak_provider_control() -> KeycloakProviderControl:
    """Return the process control, rebuilding if runtime settings changed."""
    global _runtime_control, _runtime_control_config
    config = _runtime_config()
    if _runtime_control is None or _runtime_control_config != config:
        _runtime_control = KeycloakProviderControl(config)
        _runtime_control_config = config
    return _runtime_control
