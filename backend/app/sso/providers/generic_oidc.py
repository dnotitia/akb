"""Standards-based OIDC upstream brokered through Keycloak."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from urllib.parse import quote, urlsplit

from app.sso.models import ProviderConfigureSpec, ProviderReadback, ProviderState
from app.sso import local_realm
from app.sso.providers.keycloak_oidc import (
    KEYCLOAK_PROVIDER_ID,
    MARKER_SCHEMA_KEY,
    MARKER_SCHEMA_VALUE,
    MARKER_TYPE_KEY,
    MASKED_SECRET,
    ProviderDefinitionError,
    _CLIENT_ID_RE,
    _clean_text,
    _config_string,
    _https_url,
    _issuer_identity,
    _opaque_secret,
    _secret_configured,
    validate_alias,
)


PROVIDER_TYPE = "oidc"
DISCOVERY_FINGERPRINT_KEY = "akbDiscoveryFingerprint"

_REQUIRED_ENDPOINT_CODES = {
    "authorizationUrl": "provider_discovery_authorization_url_invalid",
    "tokenUrl": "provider_discovery_token_url_invalid",
    "jwksUrl": "provider_discovery_jwks_url_invalid",
}
_OPTIONAL_ENDPOINT_KEYS = (
    "userInfoUrl",
    "logoutUrl",
    "tokenIntrospectionUrl",
)


def _https_endpoint_url(value: str, *, code: str) -> str:
    """Validate an absolute OIDC endpoint while preserving its query component."""

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
        or parsed.fragment
        or any(character.isspace() for character in cleaned)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ProviderDefinitionError(code)
    return cleaned


def validate_spec(
    spec: ProviderConfigureSpec,
    *,
    broker_issuer: str,
) -> ProviderConfigureSpec:
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
    if discovery_url != f"{issuer}/.well-known/openid-configuration":
        raise ProviderDefinitionError("provider_discovery_url_mismatch")
    client_id = _clean_text(
        spec.client_id,
        maximum=255,
        code="provider_client_id_invalid",
    )
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


def _validated_endpoints(imported: Mapping[str, object]) -> dict[str, str]:
    endpoints: dict[str, str] = {}
    for key, code in _REQUIRED_ENDPOINT_CODES.items():
        value = imported.get(key)
        if not isinstance(value, str):
            raise ProviderDefinitionError(code)
        endpoints[key] = _https_endpoint_url(value, code=code)
    for key in _OPTIONAL_ENDPOINT_KEYS:
        value = imported.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ProviderDefinitionError("provider_discovery_optional_url_invalid")
        endpoints[key] = _https_endpoint_url(
            value,
            code="provider_discovery_optional_url_invalid",
        )
    return endpoints


def _endpoint_fingerprint(
    *,
    issuer: str,
    discovery_url: str,
    endpoints: Mapping[str, str],
) -> str:
    payload = {
        "discovery_url": discovery_url,
        "endpoints": dict(sorted(endpoints.items())),
        "issuer": issuer,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def render_representation(
    spec: ProviderConfigureSpec,
    imported: Mapping[str, object],
    *,
    preserve_secret: bool,
) -> dict[str, object]:
    if imported.get("issuer") != spec.issuer:
        raise ProviderDefinitionError("provider_discovery_issuer_mismatch")
    endpoints = _validated_endpoints(imported)
    config: dict[str, str] = {
        "issuer": spec.issuer,
        **endpoints,
        "useJwksUrl": "true",
        "validateSignature": "true",
        "clientId": spec.client_id,
        "clientSecret": MASKED_SECRET if preserve_secret else spec.client_secret or "",
        # client_secret_post is advertised by Entra and accepted by Keycloak;
        # it avoids hard-coding a provider family while keeping one bounded
        # confidential-client profile for the generic contribution.
        "clientAuthMethod": "client_secret_post",
        "defaultScope": "openid profile email",
        "syncMode": "FORCE",
        "pkceEnabled": "true",
        "pkceMethod": "S256",
        "metadataDescriptorUrl": spec.discovery_url,
        MARKER_TYPE_KEY: PROVIDER_TYPE,
        MARKER_SCHEMA_KEY: MARKER_SCHEMA_VALUE,
    }
    config[DISCOVERY_FINGERPRINT_KEY] = _endpoint_fingerprint(
        issuer=spec.issuer,
        discovery_url=spec.discovery_url,
        endpoints=endpoints,
    )
    return {
        "alias": spec.alias,
        "displayName": spec.display_name,
        "providerId": KEYCLOAK_PROVIDER_ID,
        "enabled": False,
        # The product administrator explicitly trusts the selected issuer as
        # the profile authority. AKB still resolves identity only by signed
        # issuer/subject; email is mutable profile and collision data and can
        # never adopt an existing account.
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


def _readback_endpoints(config: Mapping[str, object]) -> dict[str, str]:
    endpoints: dict[str, str] = {}
    for key, code in _REQUIRED_ENDPOINT_CODES.items():
        value = _config_string(config, key)
        if value is None:
            raise ProviderDefinitionError(code)
        endpoints[key] = _https_endpoint_url(value, code=code)
    for key in _OPTIONAL_ENDPOINT_KEYS:
        raw = config.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw:
            raise ProviderDefinitionError("provider_discovery_optional_url_invalid")
        endpoints[key] = _https_endpoint_url(
            raw,
            code="provider_discovery_optional_url_invalid",
        )
    return endpoints


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
    supports_logout = False
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
            endpoints = _readback_endpoints(config)
            expected_fingerprint = _endpoint_fingerprint(
                issuer=validated.issuer,
                discovery_url=validated.discovery_url,
                endpoints=endpoints,
            )
            if config.get(DISCOVERY_FINGERPRINT_KEY) != expected_fingerprint:
                raise ProviderDefinitionError("provider_readback_url_invalid")
            issuer = validated.issuer
            discovery_url = validated.discovery_url
            client_id = validated.client_id
            supports_logout = "logoutUrl" in endpoints
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
            config.get("clientAuthMethod") == "client_secret_post",
            config.get("defaultScope") == "openid profile email",
            config.get("syncMode") == "FORCE",
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
        supports_logout=supports_logout,
        supports_identity_migration=False,
    )
