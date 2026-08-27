"""REST API routes for browsing collections."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.exceptions import NotFoundError
from app.models.document import BrowseResponse
from app.services.access_service import check_vault_access
from app.services.access_contributions import role_level
from app.services.auth_service import AuthenticatedUser
from app.services.collection_service import (
    CollectionNotEmptyError,
    CollectionService,
    InvalidPathError,
)
from app.services.revision_backend import get_document_service

router = APIRouter()
doc_service = get_document_service()
collection_service = CollectionService()


class CreateCollectionRequest(BaseModel):
    path: str
    summary: str | None = None


class AkbCollectionSummary(BaseModel):
    path: str
    name: str
    summary: str | None
    doc_count: int


class AkbCollectionCreateEnvelope(BaseModel):
    kind: Literal["collection_create"]
    ok: Literal[True]
    created: bool
    collection: AkbCollectionSummary


class AkbCollectionDeleteEnvelope(BaseModel):
    kind: Literal["collection_delete"]
    ok: Literal[True]
    collection: str
    deleted_docs: int
    deleted_files: int
    deleted_sub_collections: int
    deleted_tables: int


@router.get(
    "/browse/{vault}",
    response_model=BrowseResponse,
    summary="Browse vault collections and documents",
    operation_id="documentsBrowseVault",
    tags=["documents"],
)
async def browse_vault(
    vault: str,
    collection: str | None = Query(None),
    depth: int = Query(
        1,
        ge=-1,
        description=(
            "Tree depth from browse root. 0 = root only; N = descend N levels; "
            "-1 = unbounded entire subtree."
        ),
    ),
    include_hashes: bool = Query(False, description="Include content hash/version metadata for documents and files."),
    include_archived: bool = Query(False, description="Include archived documents (hidden from browse by default)."),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="reader")
    return await doc_service.browse(
        vault, collection=collection, depth=depth, include_hashes=include_hashes,
        include_archived=include_archived,
    )


@router.post(
    "/collections/{vault}",
    response_model=AkbCollectionCreateEnvelope,
    summary="Create an empty collection",
    operation_id="collectionsCreateCollection",
    tags=["collections"],
)
async def create_collection(
    vault: str,
    body: CreateCollectionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        result = await collection_service.create(
            vault=vault,
            path=body.path,
            summary=body.summary,
            agent_id=user.user_id,
        )
        return {"kind": "collection_create", **result}
    except InvalidPathError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.delete(
    "/collections/{vault}/{path:path}",
    response_model=AkbCollectionDeleteEnvelope,
    summary="Delete a collection",
    operation_id="collectionsDeleteCollection",
    tags=["collections"],
)
async def delete_collection(
    vault: str,
    path: str,
    recursive: bool = Query(False),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Delete a collection row, optionally cascading over its docs + files.

    Notes:
        Recursive cascade is O(N) in the number of documents and files under
        the collection: each item costs a handful of PG round-trips
        (chunks, edges, s3-delete outbox, row DELETE) and the entire cascade
        runs inside a single transaction that also holds the per-vault git
        lock. For very large collections this can keep a PG connection and
        the vault's git worktree busy for many seconds. A future preview /
        HEAD endpoint (see plan Task 14+) will expose totals up-front so
        clients can confirm or paginate.
    """
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        result = await collection_service.delete(
            vault=vault,
            path=path,
            recursive=recursive,
            agent_id=user.user_id,
            allow_table_delete=role_level(access.get("role")) >= role_level("admin"),
        )
        return {"kind": "collection_delete", **result}
    except InvalidPathError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except CollectionNotEmptyError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "doc_count": exc.doc_count,
                "file_count": exc.file_count,
                "sub_collection_count": exc.sub_collection_count,
                "table_count": exc.table_count,
            },
        )
