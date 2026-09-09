"""Credential-safe evidence serialization for benchmark outputs."""

from __future__ import annotations

import base64
import dataclasses
import json
import re
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_evals.reporting import EvaluationReport, EvaluationReportAdapter

BEARER_RE = re.compile(r"Bearer\s+(?!\[redacted\])(?:\[[^\]]*\]|[^\s,}]+)", re.IGNORECASE)
SECRET_KEY_RE = re.compile(r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|private[_-]?key|secret)", re.IGNORECASE)


def redact_text(value: Any, secrets: Iterable[str] = ()) -> str:
    """Redact configured secret values and bearer material from text."""

    message = str(value) if value is not None else ""
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        message = message.replace(secret, "[redacted]")
    return BEARER_RE.sub("Bearer [redacted]", message)


def safe_json(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Convert arbitrary SDK values to JSON while dropping secret-bearing fields."""

    secret_values = tuple(secrets)
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True, exclude_none=False)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if (
                SECRET_KEY_RE.search(key_text)
                and not key_text.endswith(("_env", "_name"))
                and isinstance(item, (str, bytes))
            ):
                result[key_text] = "[redacted]"
            else:
                result[key_text] = safe_json(item, secret_values)
        return result
    if isinstance(value, (list, tuple, set)):
        return [safe_json(item, secret_values) for item in value]
    if isinstance(value, bytes):
        return {"binary_bytes": len(value), "base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return redact_text(value, secret_values) if isinstance(value, str) else value
    return redact_text(value, secret_values)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def serialize_report(report: EvaluationReport[Any, Any, Any], secrets: Iterable[str] = ()) -> dict[str, Any]:
    raw = EvaluationReportAdapter.dump_python(report, mode="json")
    return safe_json(raw, secrets)


def write_json(path: Path, value: Any, secrets: Iterable[str] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = safe_json(value, secrets)
    encoded = json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    for secret in secrets:
        if secret and secret in encoded:
            raise RuntimeError("evidence redaction failed")
    path.write_text(encoded, encoding="utf-8")
