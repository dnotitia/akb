"""Shared utility functions."""

import json
import uuid
from datetime import date, datetime, time


def json_default(obj):
    """`json.dumps(..., default=json_default)` — handles types stdlib json
    refuses but that routinely show up in our metadata: dates, datetimes,
    times, UUIDs, bytes. Kept generous on purpose so YAML-frontmatter
    ingestion paths don't need per-field sanitisation.
    """
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def dumps_jsonb(value) -> str:
    """Canonical encoder for JSONB column writes. Non-ASCII preserved
    (we store Korean/CJK paths and frontmatter liberally)."""
    return json.dumps(value, default=json_default, ensure_ascii=False)


def ensure_dict(value) -> dict:
    """Ensure a value is a dict — handles asyncpg returning JSON as str."""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(value, dict):
        return value
    return {}


def ensure_list(value) -> list:
    """Ensure a value is a list — handles asyncpg returning JSON arrays as str."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(value, list):
        return list(value)
    return []
