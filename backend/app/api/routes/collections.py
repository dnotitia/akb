"""REST API routes for browsing collections."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.exceptions import NotFoundError
from app.models.document import BrowseResponse
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.collection_service import (
    CollectionNotEmptyError,
    CollectionService,
    InvalidPathError,
)
from app.services.document_service import DocumentService

router = APIRouter()
doc_service = DocumentService()
collection_service = CollectionService()


class CreateCollectionRequest(BaseModel):
    path: str
    summary: str | None = None


@router.get("/browse/{vault}", response_model=BrowseResponse, summary="Browse vault collections and documents")
async def browse_vault(
    vault: str,
    collection: str | None = Query(None),
    depth: int = Query(1, ge=1, le=2),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="reader")
    return await doc_service.browse(vault, collection=collection, depth=depth)


@router.post("/collections/{vault}", summary="Create an empty collection")
async def create_collection(
    vault: str,
    body: CreateCollectionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        return await collection_service.create(
            vault=vault,
            path=body.path,
            summary=body.summary,
            agent_id=user.user_id,
        )
    except InvalidPathError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.delete("/collections/{vault}/{path:path}", summary="Delete a collection")
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
    await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        return await collection_service.delete(
            vault=vault,
            path=path,
            recursive=recursive,
            agent_id=user.user_id,
        )
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
            },
        )
