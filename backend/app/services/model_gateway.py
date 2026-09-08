"""Shared request identity for managed model API calls."""

from __future__ import annotations

import uuid

from app.config import settings
from app.services.workload_identity import WorkloadIdentityError, read_projected_token


def request_headers(api_key: str) -> dict[str, str]:
    """Build provider headers without changing standalone compatibility.

    The UUID is created once per logical SDK/HTTP call. A transport that retries
    the same call reuses the header map, while a distinct call gets a new ID.
    """
    headers: dict[str, str] = {}
    if settings.model_api_governance_mode == "platform_hard":
        if api_key:
            raise WorkloadIdentityError("Static model API keys are forbidden in platform_hard mode")
        token = read_projected_token(settings.platform_gateway_token_file)
        headers["Authorization"] = f"Bearer {token}"
        headers["Idempotency-Key"] = str(uuid.uuid4())
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers
