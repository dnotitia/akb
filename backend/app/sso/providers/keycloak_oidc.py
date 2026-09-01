"""Reference upstream provider: a Keycloak realm brokered through Keycloak."""

from __future__ import annotations

from collections.abc import Mapping
import re
from urllib.parse import quote, urlsplit

from app.sso.models import ProviderConfigureSpec, ProviderReadback, ProviderState
from app.util.text import to_nfc
from app.sso import local_realm


PROVIDER_TYPE = "keycloak-oidc"
KEYCLOAK_PROVIDER_ID = "oidc"
MARKER_TYPE_KEY = "akbProviderType"
MARKER_SCHEMA_KEY = "akbProviderSchema"
MARKER_SCHEMA_VALUE = "1"
MASKED_SECRET = "**********"  # pragma: allowlist secret
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,255}$")


class ProviderDefinitionError(ValueError):
    """Value-less provider contract rejection safe for public diagnostics."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_alias(value: str) -> str:
    alias = _clean_text(value, maximum=63, code="provider_alias_invalid")
    if not _ALIAS_RE.fullmatch(alias):
        raise ProviderDefinitionError("provider_alias_invalid")
    return alias


def _clean_text(value: str, *, maximum: int, code: str) -> str:
    cleaned = to_nfc(value).strip()
    if (
        not cleaned
        or len(cleaned) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in cleaned)
    ):
        raise ProviderDefinitionError(code)
    return cleaned


def _opaque_secret(value: str, *, code: str) -> str:
    if (
        not value
        or len(value) > 4096
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ProviderDefinitionError(code)
    return value


def _https_url(value: str, *, code: str) -> str:
    cleaned = _clean_text(value, maximum=2048, code=code)
    try:
        parsed = urlsplit(cleaned)
        port = parsed.port
    except ValueError as exc:
        raise ProviderDefinitionError(code) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ProviderDefinitionError(code)
    return cleaned


def _issuer_identity(value: str) -> tuple[str, int | None, str]:
    parsed = urlsplit(value)
    port = parsed.port
    if port == 443:
        port = None
    return (
        (parsed.hostname or "").lower(),
        port,
        parsed.path.rstrip("/"),
    )


def validate_spec(spec: ProviderConfigureSpec, *, broker_issuer: str) -> ProviderConfigureSpec:
    if spec.provider_type != PROVIDER_TYPE:
        raise ProviderDefinitionError("provider_type_mismatch")
    alias = validate_alias(spec.alias)
    if local_realm.is_local_alias(alias):
        raise ProviderDefinitionError("provider_alias_reserved")
    display_name = _clean_text(
        spec.display_name,
        maximum=80,
        code="provider_display_name_invalid",
    )
    issuer = _https_url(spec.issuer, code="provider_issuer_invalid").rstrip("/")
    if _issuer_identity(issuer) == _issuer_identity(broker_issuer):
        raise ProviderDefinitionError("provider_issuer_is_broker")
    discovery_url = _https_url(
        spec.discovery_url,
        code="provider_discovery_url_invalid",
    )
    expected_discovery = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    if discovery_url != expected_discovery:
        raise ProviderDefinitionError("provider_discovery_url_mismatch")
    client_id = _clean_text(spec.client_id, maximum=255, code="provider_client_id_invalid")
    if not _CLIENT_ID_RE.fullmatch(client_id):
        raise ProviderDefinitionError("provider_client_id_invalid")
    secret = spec.client_secret
    if secret is not None:
        secret = _opaque_secret(secret, code="provider_client_secret_invalid")
    return ProviderConfigureSpec(
        provider_type=PROVIDER_TYPE,
        alias=alias,
        display_name=display_name,
        issuer=issuer,
        discovery_url=discovery_url,
        client_id=client_id,
        client_secret=secret,
    )


def is_managed_representation(value: Mapping[str, object]) -> bool:
    config = value.get("config")
    return bool(
        isinstance(config, dict)
        and config.get(MARKER_TYPE_KEY) == PROVIDER_TYPE
        and config.get(MARKER_SCHEMA_KEY) == MARKER_SCHEMA_VALUE
    )


def _required_imported_url(
    imported: Mapping[str, object],
    key: str,
    *,
    code: str,
) -> str:
    value = imported.get(key)
    if not isinstance(value, str):
        raise ProviderDefinitionError(code)
    return _https_url(value, code=code)


def _required_keycloak_endpoint(
    imported: Mapping[str, object],
    key: str,
    *,
    issuer: str,
    suffix: str,
    code: str,
) -> str:
    value = _required_imported_url(imported, key, code=code)
    if value != f"{issuer}{suffix}":
        raise ProviderDefinitionError(code)
    return value


def render_representation(
    spec: ProviderConfigureSpec,
    imported: Mapping[str, object],
    *,
    preserve_secret: bool,
) -> dict[str, object]:
    imported_issuer = imported.get("issuer")
    if imported_issuer != spec.issuer:
        raise ProviderDefinitionError("provider_discovery_issuer_mismatch")
    authorization_url = _required_keycloak_endpoint(
        imported,
        "authorizationUrl",
        issuer=spec.issuer,
        suffix="/protocol/openid-connect/auth",
        code="provider_discovery_authorization_url_invalid",
    )
    token_url = _required_keycloak_endpoint(
        imported,
        "tokenUrl",
        issuer=spec.issuer,
        suffix="/protocol/openid-connect/token",
        code="provider_discovery_token_url_invalid",
    )
    jwks_url = _required_keycloak_endpoint(
        imported,
        "jwksUrl",
        issuer=spec.issuer,
        suffix="/protocol/openid-connect/certs",
        code="provider_discovery_jwks_url_invalid",
    )
    config: dict[str, str] = {
        "issuer": spec.issuer,
        "authorizationUrl": authorization_url,
        "tokenUrl": token_url,
        "jwksUrl": jwks_url,
        "useJwksUrl": "true",
        "validateSignature": "true",
        "clientId": spec.client_id,
        "clientSecret": MASKED_SECRET if preserve_secret else spec.client_secret or "",
        "clientAuthMethod": "client_secret_basic",
        "defaultScope": "openid profile email",
        # The broker may trust the upstream email only after the signed ID
        # token proves the standard OIDC verification claim.  Keycloak's
        # trustEmail fallback otherwise treats a missing claim as verified.
        "syncMode": "FORCE",
        "filteredByClaim": "true",
        "claimFilterName": "email_verified",
        "claimFilterValue": "true",
        "pkceEnabled": "true",
        "pkceMethod": "S256",
        "metadataDescriptorUrl": spec.discovery_url,
        MARKER_TYPE_KEY: PROVIDER_TYPE,
        MARKER_SCHEMA_KEY: MARKER_SCHEMA_VALUE,
    }
    optional_endpoints = {
        "userInfoUrl": "/protocol/openid-connect/userinfo",
        "logoutUrl": "/protocol/openid-connect/logout",
        "tokenIntrospectionUrl": "/protocol/openid-connect/token/introspect",
    }
    for key, suffix in optional_endpoints.items():
        value = imported.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ProviderDefinitionError("provider_discovery_optional_url_invalid")
            endpoint = _https_url(
                value,
                code="provider_discovery_optional_url_invalid",
            )
            if endpoint != f"{spec.issuer}{suffix}":
                raise ProviderDefinitionError("provider_discovery_optional_url_invalid")
            config[key] = endpoint
    return {
        "alias": spec.alias,
        "displayName": spec.display_name,
        "providerId": KEYCLOAK_PROVIDER_ID,
        "enabled": False,
        "trustEmail": True,
        "storeToken": False,
        "addReadTokenRoleOnCreate": False,
        "authenticateByDefault": False,
        "linkOnly": False,
        "hideOnLogin": True,
        "firstBrokerLoginFlowAlias": "first broker login",
        "config": config,
    }


def with_enabled(
    representation: Mapping[str, object],
    *,
    enabled: bool,
) -> dict[str, object]:
    updated = dict(representation)
    updated["enabled"] = enabled
    updated["hideOnLogin"] = not enabled
    return updated


def _config_string(config: Mapping[str, object], key: str) -> str | None:
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def _secret_configured(config: Mapping[str, object]) -> bool:
    value = _config_string(config, "clientSecret")
    return bool(
        value
        and (
            value == MASKED_SECRET
            or (
                value.startswith("${vault.")
                and value.endswith("}")
                and len(value) <= 512
                and "{" not in value[2:]
                and "}" not in value[:-1]
            )
        )
    )


def readback(
    representation: Mapping[str, object],
    *,
    broker_base_url: str,
    broker_issuer: str,
    realm: str,
) -> ProviderReadback:
    config_value = representation.get("config")
    config: Mapping[str, object] = config_value if isinstance(config_value, dict) else {}
    alias_value = representation.get("alias")
    raw_alias = alias_value if isinstance(alias_value, str) else ""
    alias_is_valid = True
    try:
        alias = validate_alias(raw_alias)
    except ProviderDefinitionError:
        alias = ""
        alias_is_valid = False
    display_value = representation.get("displayName")
    display_is_valid = True
    try:
        display_name = _clean_text(
            display_value if isinstance(display_value, str) else "",
            maximum=80,
            code="provider_display_name_invalid",
        )
    except ProviderDefinitionError:
        display_name = alias
        display_is_valid = False
    secret_configured = _secret_configured(config)
    raw_issuer = _config_string(config, "issuer")
    raw_discovery_url = _config_string(config, "metadataDescriptorUrl")
    raw_client_id = _config_string(config, "clientId")
    issuer: str | None = None
    discovery_url: str | None = None
    client_id: str | None = None
    values_are_valid = False
    if (
        alias_is_valid
        and display_is_valid
        and raw_issuer is not None
        and raw_discovery_url is not None
        and raw_client_id is not None
    ):
        try:
            validated = validate_spec(
                ProviderConfigureSpec(
                    provider_type=PROVIDER_TYPE,
                    alias=alias,
                    display_name=display_name,
                    issuer=raw_issuer,
                    discovery_url=raw_discovery_url,
                    client_id=raw_client_id,
                    client_secret=MASKED_SECRET,
                ),
                broker_issuer=broker_issuer,
            )
            expected_endpoints = {
                "authorizationUrl": "/protocol/openid-connect/auth",
                "tokenUrl": "/protocol/openid-connect/token",
                "jwksUrl": "/protocol/openid-connect/certs",
            }
            for key, suffix in expected_endpoints.items():
                value = _config_string(config, key)
                if value != f"{validated.issuer}{suffix}":
                    raise ProviderDefinitionError("provider_readback_url_invalid")
            optional_endpoints = {
                "userInfoUrl": "/protocol/openid-connect/userinfo",
                "logoutUrl": "/protocol/openid-connect/logout",
                "tokenIntrospectionUrl": "/protocol/openid-connect/token/introspect",
            }
            for key, suffix in optional_endpoints.items():
                value = _config_string(config, key)
                if value is not None and value != f"{validated.issuer}{suffix}":
                    raise ProviderDefinitionError("provider_readback_url_invalid")
            issuer = validated.issuer
            discovery_url = validated.discovery_url
            client_id = validated.client_id
            values_are_valid = True
        except ProviderDefinitionError:
            values_are_valid = False
    common_matches = all(
        (
            values_are_valid,
            representation.get("providerId") == KEYCLOAK_PROVIDER_ID,
            representation.get("trustEmail") is True,
            representation.get("storeToken") is False,
            representation.get("addReadTokenRoleOnCreate") is False,
            representation.get("authenticateByDefault") is False,
            representation.get("linkOnly") is False,
            representation.get("firstBrokerLoginFlowAlias") == "first broker login",
            config.get(MARKER_TYPE_KEY) == PROVIDER_TYPE,
            config.get(MARKER_SCHEMA_KEY) == MARKER_SCHEMA_VALUE,
            config.get("useJwksUrl") == "true",
            config.get("validateSignature") == "true",
            config.get("clientAuthMethod") == "client_secret_basic",
            config.get("defaultScope") == "openid profile email",
            config.get("syncMode") == "FORCE",
            config.get("filteredByClaim") == "true",
            config.get("claimFilterName") == "email_verified",
            config.get("claimFilterValue") == "true",
            config.get("pkceEnabled") == "true",
            config.get("pkceMethod") == "S256",
            secret_configured,
        )
    )
    enabled = representation.get("enabled") is True
    hidden = representation.get("hideOnLogin") is True
    if common_matches and enabled and not hidden:
        state: ProviderState = "enabled"
    elif common_matches and not enabled and hidden:
        state = "configured_disabled"
    else:
        state = "configuration_error"
    redirect_uri = (
        f"{broker_base_url.rstrip('/')}/realms/{quote(realm, safe='')}/broker/"
        f"{quote(alias, safe='')}/endpoint"
    )
    return ProviderReadback(
        provider_type=PROVIDER_TYPE,
        alias=alias,
        display_name=display_name,
        state=state,
        enabled=enabled,
        issuer=issuer,
        discovery_url=discovery_url,
        client_id=client_id,
        client_secret_configured=secret_configured,
        redirect_uri=redirect_uri,
        post_logout_redirect_uri=f"{redirect_uri}/logout_response",
        supports_logout=True,
        # This capability means AKB can verify an operator-created exact
        # Keycloak prelink and atomically add the broker identity to one
        # existing AKB user.  It does not imply automatic/email-based linking.
        supports_identity_migration=True,
    )
