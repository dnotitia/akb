"""REST API routes for knowledge graph, relations, and provenance.

URI-canonical: every resource is addressed by its `uri` query param —
the same `akb://{vault}/{doc|table|file}/{...}` handle MCP clients use.
The frontend constructs the URI from `vault` + `path` (or file uuid)
before calling these endpoints.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.db.postgres import get_pool
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.kg_service import (
    LinkRelationType as RelationType,
    get_resource_relations,
    get_graph,
    get_health,
    get_overview,
    get_provenance,
    link_resources,
    unlink_resources,
)
from app.services.uri_service import parse_uri
from app.util.text import NFCModel

router = APIRouter()
logger = logging.getLogger("akb.api.knowledge")


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
    vault, rtype, ident = parsed.vault, parsed.kind, parsed.identifier
    if ident is None:
        raise HTTPException(
            status_code=400,
            detail=f"AKB URI is missing an identifier: {uri!r}.",
        )
    if expected_type and rtype != expected_type:
        raise HTTPException(
            status_code=400,
            detail=f"Expected a {expected_type} URI; got {rtype}.",
        )
    return vault, rtype, ident


# ── Relation write surface (link / unlink) ───────────────────
#
# REST twins of the MCP akb_link / akb_unlink handlers. Same writer
# gate, same-vault guard, and the same relation vocabulary — the two
# surfaces must accept and reject identically. `RelationType` is the
# single-source `LinkRelationType` from kg_service (which the MCP
# akb_link tool schema also derives from), so a relation one surface
# rejects can never sneak in through the other.


class LinkRequest(NFCModel):
    source: str
    target: str
    relation: RelationType
    metadata: dict | None = None


# kg_service.link_resources returns an err()-envelope dict instead of
# raising (the MCP path passes that envelope through verbatim). REST
# must translate it to the matching HTTP status; unknown codes → 400.
_LINK_ERR_STATUS = {
    "not_found": 404,
    "self_link": 400,
    "invalid_uri": 400,
    "invalid_argument": 400,
}


def _bridge_service_error(result: dict) -> dict:
    """Raise HTTPException when a kg_service call returned an err() dict.

    A non-dict result (a future service refactor returning a model, or a
    bare value) is passed through untouched rather than crashing on
    ``.get``. An err whose ``code`` isn't in the status map falls back to
    400 but is logged, so a newly-introduced service code surfaces in the
    logs instead of silently collapsing to a generic 400.
    """
    if not isinstance(result, dict):
        return result
    code = result.get("code")
    if code is not None:
        status = _LINK_ERR_STATUS.get(code)
        if status is None:
            logger.warning("Unmapped kg_service error code %r → 400", code)
            status = 400
        raise HTTPException(
            status_code=status,
            detail=result.get("error", "relation operation failed"),
        )
    return result


def _shared_link_vault(source: str, target: str) -> str:
    """Parse both endpoints and enforce the same-vault rule, mirroring
    the MCP link/unlink handlers. Returns the shared vault name; raises
    400 on a malformed URI or a cross-vault pair."""
    source_vault, _, _ = _parse_resource_uri(source)
    target_vault, _, _ = _parse_resource_uri(target)
    if source_vault != target_vault:
        raise HTTPException(
            status_code=400,
            detail="source and target must belong to the same vault",
        )
    return source_vault


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


@router.post("/relations", summary="Create a relation edge (link)")
async def create_relation(
    req: LinkRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    vault = _shared_link_vault(req.source, req.target)
    await check_vault_access(user.user_id, vault, required_role="writer")
    result = await link_resources(
        vault, req.source, req.target, req.relation,
        created_by=user.username, metadata=req.metadata,
    )
    return _bridge_service_error(result)


@router.delete("/relations", summary="Remove a relation edge (unlink)")
async def delete_relation(
    source: str = Query(..., description="Source resource URI"),
    target: str = Query(..., description="Target resource URI"),
    relation: RelationType | None = Query(
        None,
        description="Relation type to remove (one of the link vocabulary); "
        "omit to remove all edges between the two",
    ),
    user: AuthenticatedUser = Depends(get_current_user),
):
    vault = _shared_link_vault(source, target)
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    result = await unlink_resources(
        source, target, relation_type=relation, vault_id=access["vault_id"],
    )
    return _bridge_service_error(result)


@router.get("/graph", summary="Get knowledge graph (nodes + edges)")
async def vault_graph(
    uri: str | None = Query(None, description="Center resource URI (omit + pass vault for full vault graph)"),
    vault: str | None = Query(None, description="Vault name (only when uri is omitted)"),
    hops: int = Query(
        2,
        ge=1,
        le=5,
        description=(
            "BFS traversal radius in edge hops. Disambiguated from "
            "`/browse/{vault}?depth=` (collection-tree depth)."
        ),
    ),
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
        vault_name, resource_uri=uri, hops=hops, limit=limit,
        vault_id=access["vault_id"],
    )


@router.get("/graph/overview", summary="Get vault graph overview (degree-ranked top-K + totals)")
async def vault_graph_overview(
    vault: str = Query(..., description="Vault name"),
    top_k: int = Query(
        200, ge=1, le=1000,
        description="Keep the top-K highest-degree nodes (and the edges induced among them).",
    ),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Whole-vault overview ranked by node degree, with honest `nodes_total` /
    `edges_total` / `truncated` so the client can render "showing N of M".
    Replaces the recency-capped no-`uri` branch of `/graph` for overviews."""
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    return await get_overview(vault, vault_id=access["vault_id"], top_k=top_k)


@router.get("/graph/health", summary="Get vault graph health (hubs + orphans)")
async def vault_graph_health(
    vault: str = Query(..., description="Vault name"),
    hub_threshold: int = Query(
        5, ge=1, le=1000,
        description="Minimum degree for a node to count as a hub.",
    ),
    limit: int = Query(20, ge=1, le=200, description="Max hubs and max orphan sample size."),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """KB-health audit: over-connected hubs (degree ≥ `hub_threshold`) and the
    orphan documents (no relations) that the graph view exists to surface."""
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    return await get_health(
        vault, vault_id=access["vault_id"], hub_threshold=hub_threshold, limit=limit,
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
