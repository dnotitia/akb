"""Help router — exposes seed templates + vault-skill preview for agents."""
from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.services.document_service import VAULT_SKILL_SEED_TEMPLATE, DocumentService
from app.services.auth_service import AuthenticatedUser
from app.api.deps import get_current_user
from app.services.access_service import check_vault_access
from mcp_server.help import render_vault_skill_response

router = APIRouter()
doc_service = DocumentService()


@router.get("/skill-template", summary="Default vault-skill template body")
async def get_skill_template() -> PlainTextResponse:
    """Return the canonical vault-skill seed body as text/markdown.

    Frontend uses this to populate the 'Create from template' button so the
    seed body stays in sync with the backend's vault-create seeding.

    The `{vault}` placeholder is left intact for the caller to substitute.
    """
    return PlainTextResponse(
        content=VAULT_SKILL_SEED_TEMPLATE,
        media_type="text/markdown",
    )


@router.get("/vault-skill-preview/{vault}", summary="Agent-view preview of a vault's vault-skill")
async def get_vault_skill_preview(
    vault: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PlainTextResponse:
    """Return the same markdown that `akb_help(topic='vault-skill', vault=X)` would emit.

    Used by the frontend AGENT preview segment (S6) — keeps the agent view and
    the MCP response in sync without forcing the frontend to speak MCP-over-HTTP.
    """
    # check_vault_access raises NotFoundError or ForbiddenError (both AKBError)
    # which the global exception handler converts to JSON; no need for manual if-check.
    await check_vault_access(user.user_id, vault, required_role="reader")

    async def _fetch(v: str, doc_id: str):
        try:
            resp = await doc_service.get(v, doc_id)
        except Exception:
            return None
        return {
            "content": getattr(resp, "content", "") or "",
            "commit": getattr(resp, "current_commit", None),
            "updated_at": str(getattr(resp, "updated_at", "")),
        }

    body = await render_vault_skill_response(vault, _fetch)
    return PlainTextResponse(content=body, media_type="text/markdown")
