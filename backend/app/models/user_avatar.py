"""Browser-owned, private profile photo API contracts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer


class AvatarState(BaseModel):
    revision: UUID | None = None
    url: str | None = None
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)

    @model_serializer(mode="wrap")
    def serialize_state(self, handler: SerializerFunctionWrapHandler) -> dict:
        result = handler(self)
        # Revision and URL carry meaningful nulls; dimensions only exist for
        # a photo. This also applies when nested in typed auth/me responses.
        if self.width is None:
            result.pop("width", None)
        if self.height is None:
            result.pop("height", None)
        return result


class AvatarWriteResponse(BaseModel):
    avatar: AvatarState


class AvatarCapability(BaseModel):
    supported: Literal[True] = True
    can_upload: bool
    can_remove: bool
    reason: str | None = None
    max_upload_bytes: int
    allowed_content_types: list[str]


class ProfileCapabilities(BaseModel):
    avatar: AvatarCapability


class CurrentUserProfile(BaseModel):
    user_id: str
    username: str
    email: str
    display_name: str | None
    is_admin: bool
    auth_method: str
    account_kind: str
    key_class: str | None
    avatar: AvatarState
    profile_capabilities: ProfileCapabilities
