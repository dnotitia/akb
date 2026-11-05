"""Path-specific OAuth protected-resource metadata for tool access."""

from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException

from app.config import settings

router = APIRouter(include_in_schema=False)


@router.get("/.well-known/oauth-protected-resource/{resource_path:path}")
async def protected_resource_metadata(resource_path: str) -> dict:
    if not settings.mcp_oauth_enabled:
        raise HTTPException(404)
    if resource_path != urlsplit(settings.mcp_oauth_resource).path.lstrip("/"):
        raise HTTPException(404)
    if not settings.keycloak_enabled or not settings.mcp_oauth_native_client_id:
        raise HTTPException(503, "Tool OAuth requires an IdP and a configured public native client")
    return {
        "resource": settings.mcp_oauth_audience_effective,
        "authorization_servers": [settings.keycloak_issuer],
        "akb_native_client_id": settings.mcp_oauth_native_client_id,
        "scopes_supported": ["openid", "profile", "email", "akb:vault:read", "akb:vault:write"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{settings.public_base_url.rstrip('/')}/docs/mcp-clients/web-connectors",
    }
