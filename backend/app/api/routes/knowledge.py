"""REST API routes for knowledge graph, relations, and provenance."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.db.postgres import get_pool
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.kg_service import get_resource_relations, get_graph, get_provenance, resolve_doc_to_uri

router = APIRouter()


@router.get("/relations/{vault}/{doc_id:path}", summary="Get resource relations (1-hop)")
async def document_relations(
    vault: str,
    doc_id: str,
    direction: str = Query("both", enum=["incoming", "outgoing", "both"]),
    type: str | None = Query(None),
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    resource_uri = await resolve_doc_to_uri(vault, doc_id)
    if not resource_uri:
        raise HTTPException(status_code=404, detail=f"Document not found: {doc_id}")
    relations = await get_resource_relations(
        vault, resource_uri,
        vault_id=access["vault_id"],
        direction=direction, relation_type=type,
    )
    return {"doc_id": doc_id, "resource_uri": resource_uri, "relations": relations}


@router.get("/graph/{vault}", summary="Get knowledge graph (nodes + edges)")
async def vault_graph(
    vault: str,
    doc_id: str | None = Query(None, description="Center node (omit for full vault graph)"),
    depth: int = Query(2, ge=1, le=5),
    limit: int = Query(50, ge=1, le=200),
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    resource_uri = None
    if doc_id:
        resource_uri = await resolve_doc_to_uri(vault, doc_id)
    return await get_graph(
        vault, resource_uri=resource_uri, depth=depth, limit=limit,
        vault_id=access["vault_id"],
    )


@router.get("/provenance/{doc_id}", summary="Get document provenance")
async def document_provenance(
    doc_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT v.name AS vault_name, v.id AS vault_id
            FROM documents d JOIN vaults v ON d.vault_id = v.id
            WHERE d.id::text = $1 OR d.metadata->>'id' = $1
            """,
            doc_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    await check_vault_access(user.user_id, row["vault_name"], required_role="reader")
    return await get_provenance(doc_id, vault_id=row["vault_id"])
