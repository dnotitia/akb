"""Validate MCP arguments against the operation schemas advertised by tools/list."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from mcp.types import Tool

from app.util.errors import INVALID_ARGUMENT, UNKNOWN_ARGUMENT, err
from app.util.text import fuzzy_hint
from mcp_server.tools import OPERATIONS


_OPERATION_TOOLS: dict[str, Tool] = {tool.name: tool for tool in OPERATIONS}
_VALIDATORS = {
    name: Draft202012Validator(tool.input_schema)
    for name, tool in _OPERATION_TOOLS.items()
}


def _field_path(path: Sequence[object]) -> str:
    field = ""
    for part in path:
        if isinstance(part, int):
            field += f"[{part}]"
        else:
            field = f"{field}.{part}" if field else str(part)
    return field or "arguments"


def _target_schema(schema: dict[str, Any], path: Sequence[object]) -> dict[str, Any]:
    current = schema
    for part in path:
        if isinstance(part, int):
            current = current.get("items", {})
        else:
            current = current.get("properties", {}).get(part, {})
    return current


def _first_error(name: str, arguments: dict[str, Any]) -> ValidationError | None:
    return next(_VALIDATORS[name].iter_errors(arguments), None)


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Return a stable domain error when arguments violate their public schema."""

    if name not in _VALIDATORS:
        return None
    allowed = sorted(_OPERATION_TOOLS[name].input_schema.get("properties", {}))
    unknown = [field for field in arguments if field not in allowed]
    if unknown:
        field = unknown[0]
        return err(
            f"Unknown argument '{field}' for {name}",
            code=UNKNOWN_ARGUMENT,
            hint=fuzzy_hint(field, allowed, label="arguments"),
            field=field,
            available_arguments=allowed,
        )

    error = _first_error(name, arguments)
    if error is None:
        return None

    schema = _OPERATION_TOOLS[name].input_schema
    path = list(error.absolute_path)
    missing: str | None = None
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = next(
            (field for field in error.validator_value if field not in error.instance),
            None,
        )
        if missing is not None:
            path.append(missing)
    field = _field_path(path)
    expected_schema = _target_schema(schema, path)
    expected_type = expected_schema.get("type")
    details: dict[str, Any] = {"field": field}
    if isinstance(expected_type, str):
        details["expected_type"] = expected_type

    if missing is not None:
        requirement = f"required {expected_type or 'value'}"
        hint = f"Add `{missing}` as a {expected_type or 'valid value'}; see the schema from tools/list."
    elif error.validator == "type":
        value_type = error.validator_value
        requirement = f"expected {value_type}"
        details["expected_type"] = value_type
        hint = (
            f"Pass `{field}` as a JSON {value_type}; do not serialize it as JSON text. "
            "See the schema from tools/list."
        )
    elif error.validator in {"enum", "const"}:
        allowed = error.validator_value if error.validator == "enum" else [error.validator_value]
        details["allowed_values"] = allowed
        requirement = f"expected one of {', '.join(map(str, allowed))}"
        hint = f"Use an allowed value for `{field}`: {', '.join(map(str, allowed))}."
    else:
        requirement = f"violates the declared {error.validator} constraint"
        hint = f"Correct `{field}` to match the schema from tools/list."

    details["schema_rule"] = str(error.validator)
    return err(
        f"Invalid argument `{field}` for {name}: {requirement}.",
        code=INVALID_ARGUMENT,
        hint=hint,
        **details,
    )
