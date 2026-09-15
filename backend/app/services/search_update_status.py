"""Version-one, unit-preserving interpretation of per-Vault queue observations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from app.config import settings
from app.services.revision_backend import selected_document_revision_backend
from app.services.search_capabilities import (
    file_projection_enabled,
    metadata_enabled,
    native_derived_enabled,
)

_MAX_SAFE_INTEGER = 2**53 - 1


def _stage(raw: object, *, enabled: bool | None, unit: str, scope: str, native: bool = False) -> dict:
    names = ("pending", "retrying", "exhausted", "abandoned") if native else ("pending", "retrying", "abandoned")
    source = raw if isinstance(raw, Mapping) else {}
    counts = {
        key: source[key] for key in names
        if type(source.get(key)) is int and 0 <= source[key] <= _MAX_SAFE_INTEGER
    }
    pending = counts.get("pending")
    subsets = ("retrying", "exhausted") if native else ("retrying",)
    if pending is not None and (
        any(counts.get(key, 0) > pending for key in subsets)
        or (native and sum(counts.get(key, 0) for key in subsets) > pending)
    ):
        counts = {}  # Contradictory observations cannot certify clear work.
    mode = "unknown" if enabled is None else "enabled" if enabled else "disabled"
    if native and enabled is False and all(counts.get(key) == 0 for key in names):
        mode = "not_applicable"
    return {"mode": mode, "unit": unit, "scope": scope, **counts}


def build(backfill: object, metadata: object, projection: object, current_heads: object) -> dict:
    selected_backend = selected_document_revision_backend()
    upsert = backfill.get("upsert") if isinstance(backfill, Mapping) else None
    return {
        "version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "stages": {
            # Startup is unconditional: an absent embedding URL uses sparse indexing.
            "search_index": _stage(upsert, enabled=True, unit="chunks", scope="stored_chunks"),
            "file_projection": _stage(
                projection, enabled=file_projection_enabled(settings),
                unit="file_updates", scope="latest_file_intents", native=True,
            ),
            "content_preparation": _stage(
                current_heads, enabled=native_derived_enabled(settings),
                unit="revision_updates", scope="current_heads", native=True,
            ),
            "metadata": _stage(
                metadata,
                enabled=metadata_enabled(settings, selected_backend) if selected_backend is not None else None,
                unit="documents", scope="external_git_documents",
            ),
        },
    }
