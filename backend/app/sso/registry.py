"""Explicit built-in SSO provider registry."""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from app.sso.providers import keycloak_oidc


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    provider_type: str
    module: ModuleType


_DEFINITIONS = {
    keycloak_oidc.PROVIDER_TYPE: ProviderDefinition(
        provider_type=keycloak_oidc.PROVIDER_TYPE,
        module=keycloak_oidc,
    ),
}


def provider_definition(provider_type: str) -> ProviderDefinition:
    try:
        return _DEFINITIONS[provider_type]
    except KeyError as exc:
        raise ValueError("unsupported_sso_provider_type") from exc


def provider_types() -> tuple[str, ...]:
    return tuple(sorted(_DEFINITIONS))
