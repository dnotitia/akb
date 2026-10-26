"""Self-only, authenticated profile photo bytes (never public or presigned)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.bounded_body import read_bounded_body
from app.api.deps import get_current_user
from app.exceptions import AKBError
from app.models.user_avatar import AvatarWriteResponse
from app.openapi_contract import AkbErrorModel
from app.services import user_avatar_service as avatars
from app.services.auth_service import AuthenticatedUser

router = APIRouter()
_body_slots = asyncio.Semaphore(2)
_BODY_TIMEOUT_SECONDS = 30
_ADMISSION_TIMEOUT_SECONDS = 5
_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": AkbErrorModel} for status in (408, 413, 415, 429)
}


async def avatar_user(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    avatars.require_carrier(user)
    if request.headers.get("x-akb-delegated-authorization") is not None:
        raise AKBError("Delegated credentials cannot manage profile photos.", 403,
                       code="human_session_required")
    return user


@router.put("/auth/me/avatar", summary="Upload or replace your private profile photo",
            response_model=AvatarWriteResponse, responses=_ERRORS,
            openapi_extra={"requestBody": {"required": True, "content": {
                mime: {"schema": {"type": "string", "format": "binary"}}
                for mime in avatars.ALLOWED_CONTENT_TYPES}}})
async def upload_avatar(request: Request, response: Response,
                        expected_revision: str = Query(..., min_length=1, max_length=36),
                        user: AuthenticatedUser = Depends(avatar_user)):
    expected = avatars.parse_expected_revision(expected_revision)
    await avatars.preflight_write(user, expected)
    acquired = False
    try:
        try:
            async with asyncio.timeout(_ADMISSION_TIMEOUT_SECONDS):
                await _body_slots.acquire()
                acquired = True
        except TimeoutError:
            error = AKBError("Photo uploads are busy. Try again shortly.", 429, code="avatar_busy")
            error.retry_after_secs = 5  # type: ignore[attr-defined]
            raise error from None
        try:
            async with asyncio.timeout(_BODY_TIMEOUT_SECONDS):
                body = await read_bounded_body(request, max_bytes=avatars.MAX_UPLOAD_BYTES,
                                               too_large_message="Choose an image no larger than 10 MiB.")
        except TimeoutError:
            raise AKBError("Photo upload timed out. Try again.", 408, code="avatar_upload_timeout") from None
        except AKBError as exc:
            if exc.status_code == 413:
                raise AKBError(exc.message, 413, code="avatar_too_large") from None
            raise
        state = await avatars.save_photo(user, expected, body, request.headers.get("content-type", ""))
        response.headers["Cache-Control"] = "private, no-store"
        return {"avatar": state.model_dump()}
    finally:
        if acquired:
            _body_slots.release()


@router.delete("/auth/me/avatar", summary="Remove your private profile photo",
               response_model=AvatarWriteResponse, responses=_ERRORS)
async def delete_avatar(response: Response,
                        expected_revision: str = Query(..., min_length=1, max_length=36),
                        user: AuthenticatedUser = Depends(avatar_user)):
    expected = avatars.parse_expected_revision(expected_revision)
    await avatars.preflight_write(user, expected)
    state = await avatars.remove_photo(user, expected)
    response.headers["Cache-Control"] = "private, no-store"
    return {"avatar": state.model_dump()}


@router.get("/auth/me/avatar", summary="Read your current private profile photo",
            response_class=Response, responses={200: {"content": {"image/webp": {
                "schema": {"type": "string", "format": "binary"}}}}})
async def get_avatar(v: uuid.UUID = Query(...), user: AuthenticatedUser = Depends(avatar_user)):
    body = await avatars.read_photo(user, v)
    return Response(body, media_type="image/webp", headers={"Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"})
