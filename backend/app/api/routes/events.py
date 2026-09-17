"""REST route for the authenticated Vault Change Event Tail."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from starlette.responses import StreamingResponse

from app.api.deps import get_current_user
from app.exceptions import AKBError
from app.services import event_tail_service
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser


router = APIRouter()


@router.get(
    "/events/{vault}",
    summary="Tail retained Vault Change Events",
    operation_id="eventsTail",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Authenticated Server-Sent Events stream.",
            "content": {
                "text/event-stream": {
                    "schema": {
                        "type": "string",
                        "description": "SSE frames carrying change or checkpoint JSON data.",
                    },
                    "x-event-schemas": {
                        "change": {"$ref": "#/components/schemas/ChangeEventEnvelopeV1"},
                        "checkpoint": {"$ref": "#/components/schemas/TailCheckpointV1"},
                    },
                }
            },
        }
    },
)
async def events_tail(
    vault: str,
    request: Request,
    cursor: str | None = Query(None, description="Opaque Event Cursor to resume after."),
    start: Literal["earliest"] | None = Query(
        None,
        description="Replay from the earliest retained Vault event; mutually exclusive with a cursor.",
    ),
    kind: list[str] | None = Query(
        None,
        description="Repeat for exact Event Kind filters; omitted means all Vault-scoped kinds.",
    ),
    last_event_id: str | None = Header(
        None,
        alias="Last-Event-ID",
        description="Opaque Event Cursor; takes precedence over the cursor query parameter.",
    ),
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """Open a reader-gated stream whose source of truth is PostgreSQL."""
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    try:
        return await event_tail_service.open_tail(
            # The access check intentionally precedes all event queries.
            request=request,
            user_id=user.user_id,
            vault=vault,
            vault_id=access["vault_id"],
            last_event_id=last_event_id,
            cursor=cursor,
            start=start,
            kinds=kind,
        )
    except event_tail_service.EventCursorError as exc:
        raise AKBError(
            "Invalid Event Cursor",
            status_code=400,
            code="invalid_event_cursor",
        ) from exc
