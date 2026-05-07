"""REST API routes for agent memory."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.services.auth_service import AuthenticatedUser
from app.services.memory_service import remember, recall, forget, forget_category
from app.util.text import NFCModel

router = APIRouter()


class RememberRequest(NFCModel):
    content: str
    category: str = "general"  # context, preference, learning, work, general


@router.post("/memory", summary="Store a memory")
async def store_memory(req: RememberRequest, user: AuthenticatedUser = Depends(get_current_user)):
    return await remember(user.user_id, req.content, req.category)


@router.get("/memory", summary="Recall memories")
async def recall_memories(
    category: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    user: AuthenticatedUser = Depends(get_current_user),
):
    memories = await recall(user.user_id, category, limit)
    return {"memories": memories, "total": len(memories)}


@router.delete("/memory/{memory_id}", summary="Forget a specific memory")
async def forget_memory(memory_id: str, user: AuthenticatedUser = Depends(get_current_user)):
    success = await forget(user.user_id, memory_id)
    return {"forgotten": success}


@router.delete("/memory/category/{category}", summary="Forget all memories in a category")
async def forget_by_category(category: str, user: AuthenticatedUser = Depends(get_current_user)):
    count = await forget_category(user.user_id, category)
    return {"forgotten": count, "category": category}
