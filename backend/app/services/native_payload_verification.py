"""Verified adapter selection for native text payload placements.

This intentionally lightweight module is shared by native read, grep, and
derived paths so placement verification never pulls worker dependencies into a
public request path.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_reference_payload_store import M1ReferencePayloadStore


class NativePayloadPlacementError(RuntimeError):
    """A native head names no verified body adapter for its placement."""


def verify_native_head_body(head: Any) -> bytes:
    """Verify a Head body with the adapter selected by its immutable manifest."""
    selected_placement = head.get("selected_placement")
    if selected_placement == M1ReferencePayloadStore.selected_placement:
        return M1ReferencePayloadStore._verify_row(head)
    if selected_placement == M1PgBodyStore.selected_placement:
        return M1PgBodyStore._verify_row(head)
    raise NativePayloadPlacementError(
        f"Unsupported native payload placement: {selected_placement!r}"
    )


def payload_store_for_placement(
    pool: asyncpg.Pool,
    selected_placement: str,
    *,
    pg_body_store: M1PgBodyStore | None = None,
) -> M1PgBodyStore | M1ReferencePayloadStore:
    """Return the verified write adapter for one approved Head placement."""
    if selected_placement == M1PgBodyStore.selected_placement:
        return pg_body_store or M1PgBodyStore(pool)
    if selected_placement == M1ReferencePayloadStore.selected_placement:
        return M1ReferencePayloadStore(pool)
    raise NativePayloadPlacementError(
        f"Unsupported native payload placement: {selected_placement!r}"
    )
