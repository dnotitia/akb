"""Shared request identity for managed model API calls."""

from __future__ import annotations

import uuid

from app.config import settings


def request_headers(api_key: str) -> dict[str, str]:
    """Build provider headers without changing standalone compatibility.

    The UUID is created once per logical SDK/HTTP call. A transport that retries
    the same call reuses the header map, while a distinct call gets a new ID.
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if settings.model_api_governance_mode == "platform_hard":
        headers["Idempotency-Key"] = str(uuid.uuid4())
    return headers
