"""Pure response-shaping helpers for MCP tool envelopes."""

from __future__ import annotations

from app.models.document import BrowseResponse


def browse_payload(result: BrowseResponse, *, include_summary: bool) -> dict:
    """Keep navigation context while bounding large resource summaries.

    Collection summaries are small, first-class navigation context and stay
    visible by default. Document, table, and file summaries retain the
    historical opt-in behavior to avoid inflating large browse responses.
    """
    payload = result.model_dump()
    if not include_summary:
        for item in payload.get("items") or []:
            if item.get("type") != "collection":
                item.pop("summary", None)
    return payload
