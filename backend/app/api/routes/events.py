"""REST route for the authenticated Vault Change Event Tail."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from starlette.responses import StreamingResponse

from app.api.deps import get_current_user
from app.exceptions import AKBError
from app.services import event_tail_service
from app.services.auth_service import AuthenticatedUser


router = APIRouter()

# Every refusal of a new tail for capacity, quota or a slow access check
# carries this header (the AKBError handler sets it from the error).
_RETRY_AFTER_HEADER = {
    "description": "Seconds to wait before retrying.",
    "schema": {"type": "integer", "minimum": 0},
}


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
        },
        429: {
            "description": (
                "Too Many Requests: this account already holds its quota of open event streams on this "
                "server (`event_tail_user_quota`). Close streams it no longer reads."
            ),
            "headers": {"Retry-After": _RETRY_AFTER_HEADER},
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/AkbError"}}},
        },
        503: {
            "description": (
                "Service Unavailable: this server already holds its maximum of open event streams "
                "(`event_tail_capacity`), or the caller's access could not be confirmed in time to start "
                "the stream (`event_tail_unavailable`). Retry after the Retry-After delay and resume from "
                "the last cursor received."
            ),
            "headers": {"Retry-After": _RETRY_AFTER_HEADER},
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/AkbError"}}},
        },
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
    # open_tail checks reader access before any event query, on a connection
    # within the event tail's database budget, so reconnects cannot take more
    # of the pool than that budget.
    try:
        return await event_tail_service.open_tail(
            request=request,
            user_id=user.user_id,
            vault=vault,
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
