"""Help router — exposes seed templates + vault-skill preview for agents."""
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.services.document_service import VAULT_SKILL_SEED_TEMPLATE
from app.services.revision_backend import get_document_service
from app.services.auth_service import AuthenticatedUser
from app.api.deps import get_current_user
from app.services.access_service import check_vault_access
from mcp_server.help import render_vault_skill_response

router = APIRouter()
doc_service = get_document_service()


MARKDOWN_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "content": {
            "text/markdown": {"schema": {"type": "string"}},
        },
        "description": "Markdown text",
    }
}


@router.get(
    "/skill-template",
    response_class=PlainTextResponse,
    responses=MARKDOWN_RESPONSE,
    summary="Default vault-skill template body",
)
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


@router.get(
    "/vault-skill-preview/{vault}",
    response_class=PlainTextResponse,
    responses=MARKDOWN_RESPONSE,
    summary="Agent-view preview of a vault's vault-skill",
)
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
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    from app.services import vault_skill_service

    async def _fetch(v: str, doc_id: str):
        resp = await vault_skill_service.fetch_for_authorized_reader(
            v, str(access["vault_id"]), documents=doc_service,
        )
        if resp is None:
            return None
        return {
            "content": resp["content"],
            "commit": resp["version"],
            "updated_at": "",
        }

    body = await render_vault_skill_response(vault, _fetch)
    return PlainTextResponse(content=body, media_type="text/markdown")
