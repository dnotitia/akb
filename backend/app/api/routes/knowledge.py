"""REST API routes for knowledge graph, relations, and provenance.

URI-canonical: every resource is addressed by its `uri` query param —
the same `akb://{vault}/{doc|table|file}/{...}` handle MCP clients use.
The frontend constructs the URI from `vault` + `path` (or file uuid)
before calling these endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.db.postgres import get_pool
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.kg_service import get_resource_relations, get_graph, get_provenance
from app.services.uri_service import parse_uri

router = APIRouter()


def _parse_resource_uri(uri: str, expected_type: str | None = None) -> tuple[str, str, str]:
    """Parse akb:// URI → (vault, type, identifier). Raise 400 on error.

    `expected_type`, when given, must match the URI's scheme tag.
    """
    parsed = parse_uri(uri)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid AKB URI: {uri!r}. Expected akb://<vault>/<type>/<id>.",
        )
    vault, rtype, ident = parsed
    if expected_type and rtype != expected_type:
        raise HTTPException(
            status_code=400,
            detail=f"Expected a {expected_type} URI; got {rtype}.",
        )
    return vault, rtype, ident


@router.get("/relations", summary="Get resource relations (1-hop)")
async def resource_relations(
    uri: str = Query(..., description="Resource URI"),
    direction: str = Query("both", enum=["incoming", "outgoing", "both"]),
    type: str | None = Query(None),
    user: AuthenticatedUser = Depends(get_current_user),
):
    vault, _rtype, _ident = _parse_resource_uri(uri)
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    relations = await get_resource_relations(
        vault, uri,
        vault_id=access["vault_id"],
        direction=direction, relation_type=type,
    )
    return {"uri": uri, "relations": relations}


@router.get("/graph", summary="Get knowledge graph (nodes + edges)")
async def vault_graph(
    uri: str | None = Query(None, description="Center resource URI (omit + pass vault for full vault graph)"),
    vault: str | None = Query(None, description="Vault name (only when uri is omitted)"),
    depth: int = Query(2, ge=1, le=5),
    limit: int = Query(50, ge=1, le=200),
    user: AuthenticatedUser = Depends(get_current_user),
):
    if uri:
        center_vault, _rtype, _ident = _parse_resource_uri(uri)
        vault_name = center_vault
    else:
        if not vault:
            raise HTTPException(
                status_code=400, detail="Either `uri` or `vault` is required",
            )
        vault_name = vault
    access = await check_vault_access(user.user_id, vault_name, required_role="reader")
    return await get_graph(
        vault_name, resource_uri=uri, depth=depth, limit=limit,
        vault_id=access["vault_id"],
    )


@router.get("/provenance", summary="Get document provenance")
async def document_provenance(
    uri: str = Query(..., description="Document URI"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    vault, _rtype, doc_path = _parse_resource_uri(uri, expected_type="doc")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT v.id AS vault_id, d.id AS doc_pk
              FROM documents d JOIN vaults v ON d.vault_id = v.id
             WHERE v.name = $1 AND d.path = $2
            """,
            vault, doc_path,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    await check_vault_access(user.user_id, vault, required_role="reader")
    return await get_provenance(str(row["doc_pk"]), vault_id=row["vault_id"])
