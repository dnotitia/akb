"""OAuth 2.0 Protected Resource Metadata (RFC 9728).

A single read-only endpoint that points a remote-MCP client at the
authorization server it should authenticate against before calling
``/mcp``. AKB is a Resource Server here; the AS — including DCR,
authorize, consent, token, refresh — lives in the OIDC IdP
(Keycloak in the reference deployment).

This route is only meaningful when ``mcp_oauth_enabled`` is true. When
the deployment has not opted in, the metadata is 404 so a probing
client cleanly falls through to PAT-only behaviour without seeing
half-configured discovery data.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.config import settings

router = APIRouter(include_in_schema=False)


@router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata() -> dict:
    """RFC 9728 §3 — describe the AKB ``/mcp`` resource.

    Fields:

    - ``resource`` is the canonical identifier the access token's
      ``aud`` claim must match; we keep it as the publicly-advertised
      ``/mcp`` URL.
    - ``authorization_servers`` lists the issuer URL(s) the client
      may use to obtain an access token. For Keycloak this is
      ``<server>/realms/<realm>``; the client appends
      ``/.well-known/openid-configuration`` to discover endpoints
      (RFC 8414 + OIDC Discovery).
    - ``scopes_supported`` declares the scope vocabulary the client
      may request. ``offline_access`` is advertised so Claude Code
      (and any spec-compliant client) appends it to the pinned
      scopes; without that, the access token cannot be refreshed
      silently and the user gets a re-consent browser pop on every
      expiry. (akb realm's ``defaultOptionalClientScopes`` makes
      these requestable on a DCR-registered client.)
    - ``bearer_methods_supported`` follows §3 and limits acceptance
      to the ``Authorization`` header.
    """
    if not settings.mcp_oauth_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not settings.keycloak_enabled:
        # mcp_oauth without an IdP is a config bug (the lifecycle
        # validator catches it at startup, but be defensive here so a
        # mis-toggled live cluster still returns a usable 503 rather
        # than a misleading metadata document.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "MCP OAuth is enabled but no IdP (keycloak_enabled) is configured",
        )

    resource = settings.mcp_oauth_audience_effective
    return {
        "resource": resource,
        "authorization_servers": [settings.keycloak_issuer],
        "scopes_supported": [
            "akb:vault:read",
            "akb:vault:write",
            "offline_access",
        ],
        "bearer_methods_supported": ["header"],
        "resource_documentation": (
            f"{settings.public_base_url.rstrip('/')}/docs/mcp-clients/web-connectors"
            if settings.public_base_url
            else None
        ),
    }
