"""Opaque keyset boundaries for the personal latest-document feed."""

import base64
import json
import uuid
from datetime import datetime

from app.exceptions import AKBError


def decode_cursor(value: str | None, scope: str, vault: str | None):
    if value is None:
        return None
    try:
        if len(value) > 1024:
            raise ValueError
        data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise ValueError
        if data["scope"] != scope or data["vault"] != vault:
            raise ValueError
        changed_at = datetime.fromisoformat(data["at"])
        if changed_at.tzinfo is None:
            raise ValueError
        return changed_at, uuid.UUID(data["id"])
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        raise AKBError("Invalid recent changes cursor", status_code=400) from None


def encode_cursor(change: dict, scope: str, vault: str | None) -> str:
    payload = {"at": change["changed_at"], "id": change["resource_id"],
               "scope": scope, "vault": vault}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
