"""REST routes for knowledge-bundle export/import (OKF, extensible by format)."""

import io

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user
from app.services import knowledge_io
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.document_service import DocumentService

router = APIRouter()
doc_service = DocumentService()


@router.get(
    "/vaults/{vault}/export",
    responses={
        200: {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": True,
                    }
                },
                "application/zip": {
                    "schema": {"type": "string", "format": "binary"}
                },
            },
            "description": "Knowledge bundle as JSON or zip archive",
        }
    },
    summary="Export a vault as a knowledge bundle",
)
async def export_vault(
    vault: str,
    format: str = Query("okf", description="Bundle format (currently: okf)"),
    as_: str = Query("zip", alias="as", description="Response shape: zip | json"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Reader role required. Returns a zip archive by default, or the bundle as
    a JSON ``{path: content}`` map with ``?as=json``."""
    await check_vault_access(user.user_id, vault, required_role="reader")
    try:
        files = await knowledge_io.export_vault(vault, fmt=format, doc_service=doc_service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if as_ == "json":
        return {"format": format, "vault": vault, "file_count": len(files), "files": files}
    data = knowledge_io.bundle_to_zip(files)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{vault}.okf.zip"'},
    )


@router.post("/vaults/{vault}/import", summary="Import a knowledge bundle into a vault")
async def import_vault(
    vault: str,
    file: UploadFile = File(..., description="Bundle archive (.zip)"),
    format: str = Query("okf", description="Bundle format (currently: okf)"),
    status: str | None = Query(None, description="Override status for imported docs"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Writer role required. Accepts a zip archive (the export format). Existing
    document paths are skipped; per-document failures are reported, not fatal."""
    await check_vault_access(user.user_id, vault, required_role="writer")
    raw = await file.read()
    try:
        bundle = knowledge_io.zip_to_bundle(raw)
        return await knowledge_io.import_bundle(
            vault, bundle, fmt=format, actor_id=user.username,
            doc_service=doc_service, status=status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
