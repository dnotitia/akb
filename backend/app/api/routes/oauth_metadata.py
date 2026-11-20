"""OAuth protected-resource metadata for MCP and file controls."""

from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.services.tool_resource_policy import READ_SCOPE, WRITE_SCOPE

router = APIRouter(include_in_schema=False)


@router.get("/.well-known/oauth-protected-resource/{resource_path:path}")
async def protected_resource_metadata(resource_path: str) -> dict:
    resource = settings.mcp_oauth_audience_effective
    if not resource or f"/{resource_path}" != unquote(urlsplit(resource).path):
        raise HTTPException(404)
    if not settings.keycloak_enabled:
        raise HTTPException(503, "MCP OAuth requires a configured identity provider")
    return {
        "resource": resource,
        "authorization_servers": [settings.keycloak_issuer],
        "scopes_supported": ["profile", "email", READ_SCOPE, WRITE_SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{settings.public_base_url.rstrip('/')}/docs/mcp-clients/web-connectors",
    }
