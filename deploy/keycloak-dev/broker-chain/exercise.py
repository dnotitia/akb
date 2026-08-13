"""Exercise the real two-Keycloak provider lifecycle without printing secrets."""

from __future__ import annotations

import asyncio
import json

import httpx

from app.sso.keycloak_admin import KeycloakAdminConfig, KeycloakProviderControl
from app.sso.models import ProviderConfigureSpec


BROKER = "https://broker.localhost:19443"
UPSTREAM_ISSUER = "https://upstream.localhost:19444/realms/workforce"


async def main() -> None:
    control = KeycloakProviderControl(
        KeycloakAdminConfig(
            internal_base_url=BROKER,
            public_base_url=BROKER,
            realm="akb",
            management_client_id="akb-sso-manager",
            management_client_secret="fixture-only-management-secret",  # pragma: allowlist secret
            verify_ssl=False,
        )
    )
    configured_mutation = await control.configure(
        ProviderConfigureSpec(
            provider_type="keycloak-oidc",
            alias="workforce",
            display_name="Company SSO",
            issuer=UPSTREAM_ISSUER,
            discovery_url=f"{UPSTREAM_ISSUER}/.well-known/openid-configuration",
            client_id="akb-broker",
            client_secret="fixture-only-upstream-client-secret",  # pragma: allowlist secret
        )
    )
    configured = configured_mutation.after
    if configured_mutation.before is not None or configured.state != "configured_disabled":
        raise RuntimeError("fixture_configure_readback_failed")
    preserved_mutation = await control.configure(
        ProviderConfigureSpec(
            provider_type="keycloak-oidc",
            alias="workforce",
            display_name="Company SSO",
            issuer=UPSTREAM_ISSUER,
            discovery_url=f"{UPSTREAM_ISSUER}/.well-known/openid-configuration",
            client_id="akb-broker",
            client_secret=None,
        )
    )
    if (
        preserved_mutation.before is None
        or not preserved_mutation.before.client_secret_configured
        or not preserved_mutation.after.client_secret_configured
    ):
        raise RuntimeError("fixture_secret_preservation_failed")
    enabled_mutation = await control.set_enabled("workforce", enabled=True)
    enabled = enabled_mutation.after
    catalog = await control.list_providers(force_refresh=True)
    if enabled.state != "enabled" or [item.alias for item in catalog] != ["workforce"]:
        raise RuntimeError("fixture_enable_readback_failed")

    async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
        response = await client.get(
            f"{BROKER}/realms/akb/protocol/openid-connect/auth",
            params={
                "client_id": "fixture-browser",
                "redirect_uri": "https://client.localhost/callback",
                "response_type": "code",
                "scope": "openid",
                "kc_idp_hint": "workforce",
            },
        )
    if response.status_code != 200:
        raise RuntimeError("fixture_broker_redirect_failed")
    upstream_request = next(
        (
            item.request.url
            for item in (*response.history, response)
            if item.request.url.host == "upstream.localhost"
            and item.request.url.path
            == "/realms/workforce/protocol/openid-connect/auth"
        ),
        None,
    )
    if upstream_request is None or upstream_request.params.get("client_id") != "akb-broker":
        raise RuntimeError("fixture_upstream_redirect_invalid")

    disabled = (await control.set_enabled("workforce", enabled=False)).after
    if disabled.state != "configured_disabled":
        raise RuntimeError("fixture_disable_readback_failed")
    print(
        json.dumps(
            {
                "schema_version": 1,
                "provider_type": enabled.provider_type,
                "alias": enabled.alias,
                "configure_state": configured.state,
                "enabled_state": enabled.state,
                "disabled_state": disabled.state,
                "broker_redirect": "verified",
                "secret_preservation": "verified",  # pragma: allowlist secret
                "client_secret_exposed": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
