"""REST API routes for document CRUD."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.services import template_registry
from app.services.access_service import check_vault_access, list_accessible_vaults
from app.models.document import (
    DocumentPutRequest,
    DocumentPutResponse,
    DocumentResponse,
    DocumentUpdateRequest,
)
from app.services.auth_service import AuthenticatedUser
from app.services.document_service import DocumentService
from app.util.text import to_nfc


class VaultTemplateCollection(BaseModel):
    path: str
    name: str


class VaultTemplate(BaseModel):
    name: str
    display_name: str
    description: str
    collection_count: int
    collections: list[VaultTemplateCollection]


router = APIRouter()
doc_service = DocumentService()


@router.post("/vaults", summary="Create a new vault")
async def create_vault(name: str, description: str = "", template: str | None = None, public_access: str = "none", user: AuthenticatedUser = Depends(get_current_user)):
    name = to_nfc(name)
    description = to_nfc(description)
    vault_id = await doc_service.create_vault(name, description, owner_id=user.user_id, template=template, public_access=public_access)
    return {"vault_id": vault_id, "name": name, "template": template, "public_access": public_access}


@router.get(
    "/vaults/templates",
    response_model=list[VaultTemplate],
    summary="List available vault templates",
)
async def list_vault_templates(user: AuthenticatedUser = Depends(get_current_user)):
    return [
        VaultTemplate(
            name=s.name,
            display_name=s.display_name,
            description=s.description,
            collection_count=s.collection_count,
            collections=[
                VaultTemplateCollection(path=c.path, name=c.name)
                for c in s.collections
            ],
        )
        for s in template_registry.list_summaries()
    ]


@router.get("/vaults", summary="List accessible vaults")
async def list_vaults(user: AuthenticatedUser = Depends(get_current_user)):
    return {"vaults": await list_accessible_vaults(user.user_id)}


@router.post("/documents", response_model=DocumentPutResponse, summary="Put a document")
async def put_document(req: DocumentPutRequest, user: AuthenticatedUser = Depends(get_current_user)):
    await check_vault_access(user.user_id, req.vault, required_role="writer")
    return await doc_service.put(req, agent_id=user.username)


@router.get("/documents/{vault}/{doc_id:path}", response_model=DocumentResponse, summary="Get a document")
async def get_document(vault: str, doc_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    await check_vault_access(user.user_id, vault, required_role="reader")
    return await doc_service.get(vault, doc_id)


@router.patch("/documents/{vault}/{doc_id:path}", response_model=DocumentPutResponse, summary="Update a document")
async def update_document(vault: str, doc_id: str, req: DocumentUpdateRequest, user: AuthenticatedUser = Depends(get_current_user)):
    await check_vault_access(user.user_id, vault, required_role="writer")
    return await doc_service.update(vault, doc_id, req, agent_id=user.username)


@router.delete("/documents/{vault}/{doc_id:path}", summary="Delete a document")
async def delete_document(vault: str, doc_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    await check_vault_access(user.user_id, vault, required_role="writer")
    await doc_service.delete(vault, doc_id, agent_id=user.username)
    return {"deleted": True}
