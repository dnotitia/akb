"""Provider-neutral models for the AKB SSO control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ProviderState = Literal[
    "configured_disabled",
    "enabled",
    "configuration_error",
]


@dataclass(frozen=True, slots=True)
class ProviderConfigureSpec:
    provider_type: str
    alias: str
    display_name: str
    issuer: str
    discovery_url: str
    client_id: str
    client_secret: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderReadback:
    provider_type: str
    alias: str
    display_name: str
    state: ProviderState
    enabled: bool
    issuer: str | None
    discovery_url: str | None
    client_id: str | None
    client_secret_configured: bool
    redirect_uri: str
    supports_logout: bool
    supports_identity_migration: bool

    def admin_view(self) -> dict[str, object]:
        return {
            "provider_type": self.provider_type,
            "alias": self.alias,
            "display_name": self.display_name,
            "state": self.state,
            "enabled": self.enabled,
            "issuer": self.issuer,
            "discovery_url": self.discovery_url,
            "client_id": self.client_id,
            "client_secret_configured": self.client_secret_configured,
            "redirect_uri": self.redirect_uri,
            "capabilities": {
                "supports_logout": self.supports_logout,
                "supports_identity_migration": self.supports_identity_migration,
            },
        }

    def public_view(self, *, login_url: str | None) -> dict[str, object]:
        return {
            "provider_type": self.provider_type,
            "alias": self.alias,
            "display_name": self.display_name,
            "login_url": login_url,
        }

    def audit_view(self) -> dict[str, object]:
        """Return bounded, secret-free mutation evidence."""
        return {
            "provider_type": self.provider_type,
            "alias": self.alias,
            "display_name": self.display_name,
            "state": self.state,
            "enabled": self.enabled,
            "issuer": self.issuer,
            "client_id": self.client_id,
            "client_secret_configured": self.client_secret_configured,
        }


@dataclass(frozen=True, slots=True)
class ProviderMutationReadback:
    """Secret-free before/after evidence from one successful mutation."""

    before: ProviderReadback | None
    after: ProviderReadback

    def audit_view(self) -> dict[str, object]:
        return {
            "before": self.before.audit_view() if self.before is not None else None,
            "after": self.after.audit_view(),
        }


@dataclass(frozen=True, slots=True)
class IdentityPrelinkReadback:
    """Exact, read-only evidence for a brokered Keycloak user link."""

    provider_alias: str
    provider_state: ProviderState
    upstream_issuer: str
    broker_issuer: str
    broker_subject: str = field(repr=False)
    upstream_subject: str = field(repr=False)
    broker_username: str
