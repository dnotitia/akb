"""Shared bounded request-body reads for raw binary upload endpoints."""

from __future__ import annotations

from fastapi import Request

from app.exceptions import AKBError


async def read_bounded_body(
    request: Request,
    *,
    max_bytes: int,
    too_large_message: str,
    invalid_length_message: str = "Invalid Content-Length",
) -> bytes:
    """Read at most ``max_bytes`` while checking both declared and real size.

    ``Content-Length`` is only an early rejection. The streaming counter is the
    authoritative bound because transfer encoding and intermediaries can omit
    or misstate the header.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError as exc:
            raise AKBError(invalid_length_message, status_code=400) from exc
        if declared_size < 0:
            raise AKBError(invalid_length_message, status_code=400)
        if declared_size > max_bytes:
            raise AKBError(too_large_message, status_code=413)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise AKBError(too_large_message, status_code=413)
    return bytes(body)
