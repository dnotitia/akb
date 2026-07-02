"""OpenAPI contract normalization for the REST API surface."""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field


class AkbErrorModel(BaseModel):
    """Canonical REST error shape advertised to OpenAPI clients."""

    message: str = Field(description="Human-readable error message.")
    code: str = Field(description="Stable machine-readable error code.")
    details: dict[str, Any] | list[Any] | str | int | float | bool | None = Field(
        default=None,
        description="Optional structured error metadata.",
    )
    hint: str | None = Field(default=None, description="Optional recovery hint.")
    detail: dict[str, Any] | list[Any] | str | int | float | bool | None = Field(
        default=None,
        description="Deprecated FastAPI detail alias, kept for legacy clients.",
    )
    error: str | None = Field(
        default=None,
        description="Deprecated alias for message, kept for legacy clients.",
    )
    password_required: bool | None = Field(
        default=None,
        description="Legacy public-publication password gate flag.",
    )
    slug: str | None = Field(
        default=None,
        description="Legacy public-publication slug for password-gate responses.",
    )


JSON_VALUE_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "integer"},
        {"type": "boolean"},
        {"type": "null"},
        {
            "type": "array",
            "items": {"$ref": "#/components/schemas/AkbJsonValue"},
        },
        {
            "type": "object",
            "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
        },
    ]
}

JSON_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
}

JSON_OBJECT_ARRAY_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"$ref": "#/components/schemas/AkbJsonObject"},
}

ERROR_STATUSES = ("400", "401", "403", "404", "409", "422", "500")
SUCCESS_STATUSES = ("200", "201", "202")
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
KIND_SUCCESS_RESPONSE_REFS = {
    ("post", "/api/v1/tables/{vault}"): "#/components/schemas/AkbTableEnvelope",
    ("get", "/api/v1/tables/{vault}"): "#/components/schemas/AkbTableEnvelope",
    ("post", "/api/v1/tables/{vault}/sql"): "#/components/schemas/AkbSqlEnvelope",
    ("delete", "/api/v1/tables/{vault}/{table_name}"): "#/components/schemas/AkbTableEnvelope",
    ("post", "/api/v1/files/{vault}/upload"): "#/components/schemas/AkbFileEnvelope",
    ("post", "/api/v1/files/{vault}/{file_id}/confirm"): "#/components/schemas/AkbFileEnvelope",
    ("get", "/api/v1/files/{vault}/{file_id}/download"): "#/components/schemas/AkbFileEnvelope",
    ("get", "/api/v1/files/{vault}"): "#/components/schemas/AkbFileEnvelope",
    ("delete", "/api/v1/files/{vault}/{file_id}"): "#/components/schemas/AkbFileEnvelope",
}


def install_openapi_contract(app: FastAPI) -> None:
    """Install the API-wide OpenAPI rules required by SDK codegen.

    Most REST handlers predate strict SDK generation and return plain dicts.
    The runtime payloads are intentionally left alone here; this layer gives
    codegen a stable, typed OpenAPI surface without touching every route body.
    """

    _prepare_api_routes(app)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        _install_components(schema)
        _normalize_api_operations(schema)
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def _prepare_api_routes(app: FastAPI) -> None:
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path_format.startswith("/api/v1"):
            continue
        if not route.tags:
            route.tags = [_namespace_for_path(route.path_format)]
        route.operation_id = _operation_id(route)


def _install_components(schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas["AkbError"] = AkbErrorModel.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    schemas["AkbJsonValue"] = JSON_VALUE_SCHEMA
    schemas["AkbJsonObject"] = JSON_OBJECT_SCHEMA
    schemas.update(_success_envelope_schemas())
    security = components.setdefault("securitySchemes", {})
    security["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "description": "JWT or AKB personal access token supplied as a Bearer token.",
    }


def _normalize_api_operations(schema: dict[str, Any]) -> None:
    for path, path_item in schema.get("paths", {}).items():
        if not path.startswith("/api/v1"):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation.setdefault("tags", [_namespace_for_path(path)])
            operation.setdefault("operationId", _operation_id_from_schema(path, method, operation))
            _ensure_success_response(path, method, operation)
            _ensure_error_responses(operation)


def _ensure_success_response(path: str, method: str, operation: dict[str, Any]) -> None:
    responses = operation.setdefault("responses", {})
    status = next((code for code in SUCCESS_STATUSES if code in responses), None)
    if status is None:
        if any(str(code).startswith("3") for code in responses):
            return
        status = "200"
    response = responses.setdefault(status, {"description": "Successful Response"})
    content = response.setdefault("content", {})
    if content and "application/json" not in content:
        return
    media = content.setdefault("application/json", {})
    if ref := KIND_SUCCESS_RESPONSE_REFS.get((method, path)):
        media["schema"] = {"$ref": ref}
        return
    schema = media.setdefault("schema", {})
    if schema == {}:
        media["schema"] = {"$ref": "#/components/schemas/AkbJsonObject"}


def _ensure_error_responses(operation: dict[str, Any]) -> None:
    responses = operation.setdefault("responses", {})
    for status in ERROR_STATUSES:
        response = responses.setdefault(status, {"description": _error_description(status)})
        content = response.setdefault("content", {})
        media = content.setdefault("application/json", {})
        media["schema"] = {"$ref": "#/components/schemas/AkbError"}


def _error_description(status: str) -> str:
    return {
        "400": "Bad Request",
        "401": "Unauthorized",
        "403": "Forbidden",
        "404": "Not Found",
        "409": "Conflict",
        "422": "Validation Error",
        "500": "Internal Server Error",
    }[status]


def _success_envelope_schemas() -> dict[str, dict[str, Any]]:
    return {
        "AkbSuccessEnvelope": {
            "description": "HTTP success envelope union. SDKs unwrap this to {data,error}.",
            "oneOf": [
                {"$ref": "#/components/schemas/AkbTableEnvelope"},
                {"$ref": "#/components/schemas/AkbTableQueryEnvelope"},
                {"$ref": "#/components/schemas/AkbTableSqlEnvelope"},
                {"$ref": "#/components/schemas/AkbFileEnvelope"},
            ],
            "discriminator": {
                "propertyName": "kind",
                "mapping": {
                    "table": "#/components/schemas/AkbTableEnvelope",
                    "table_query": "#/components/schemas/AkbTableQueryEnvelope",
                    "table_sql": "#/components/schemas/AkbTableSqlEnvelope",
                    "file": "#/components/schemas/AkbFileEnvelope",
                },
            },
        },
        "AkbSqlEnvelope": {
            "description": "SQL execution success envelope.",
            "oneOf": [
                {"$ref": "#/components/schemas/AkbTableQueryEnvelope"},
                {"$ref": "#/components/schemas/AkbTableSqlEnvelope"},
            ],
            "discriminator": {
                "propertyName": "kind",
                "mapping": {
                    "table_query": "#/components/schemas/AkbTableQueryEnvelope",
                    "table_sql": "#/components/schemas/AkbTableSqlEnvelope",
                },
            },
        },
        "AkbTableEnvelope": _kind_schema(
            "table",
            {
                "uri": {"type": "string"},
                "vault": {"type": "string"},
                "collection": _nullable_string(),
                "name": {"type": "string"},
                "sql_name": {"type": "string"},
                "description": _nullable_string(),
                "columns": JSON_OBJECT_ARRAY_SCHEMA,
                "unique_keys": JSON_OBJECT_ARRAY_SCHEMA,
                "indexes": JSON_OBJECT_ARRAY_SCHEMA,
                "row_count": {"type": "integer"},
                "created_at": {"type": "string", "format": "date-time"},
                "deleted": {"type": "boolean"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["kind"],
                        "properties": {"kind": _kind_property("table")},
                        "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
                    },
                },
                "total": {"type": "integer"},
            },
            "Table resource, list, mutation, and delete success envelope.",
        ),
        "AkbTableQueryEnvelope": _kind_schema(
            "table_query",
            {
                "vaults": {"type": "array", "items": {"type": "string"}},
                "columns": {"type": "array", "items": {"type": "string"}},
                "items": JSON_OBJECT_ARRAY_SCHEMA,
                "total": {"type": "integer"},
            },
            "SQL SELECT/WITH success envelope.",
            required=("kind", "vaults", "columns", "items", "total"),
        ),
        "AkbTableSqlEnvelope": _kind_schema(
            "table_sql",
            {
                "vaults": {"type": "array", "items": {"type": "string"}},
                "result": {"type": "string"},
            },
            "SQL mutation success envelope.",
            required=("kind", "vaults", "result"),
        ),
        "AkbFileEnvelope": _kind_schema(
            "file",
            {
                "uri": {"type": "string"},
                "id": {"type": "string"},
                "vault": {"type": "string"},
                "collection": _nullable_string(),
                "name": {"type": "string"},
                "mime_type": {"type": "string"},
                "size_bytes": {"type": "integer"},
                "description": _nullable_string(),
                "upload_url": {"type": "string"},
                "download_url": {"type": "string"},
                "s3_key": {"type": "string"},
                "content_hash": _nullable_string(),
                "hash_algorithm": _nullable_string(),
                "etag": _nullable_string(),
                "storage_version": _nullable_string(),
                "expires_in": {"type": "integer"},
                "deleted": {"type": "boolean"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["kind"],
                        "properties": {"kind": _kind_property("file")},
                        "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
                    },
                },
                "total": {"type": "integer"},
            },
            "File resource, list, upload, download, and delete success envelope.",
        ),
    }


def _kind_schema(
    kind: str,
    properties: dict[str, Any],
    description: str,
    *,
    required: tuple[str, ...] = ("kind",),
) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "required": list(required),
        "properties": {
            "kind": _kind_property(kind),
            **properties,
        },
        "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
    }


def _kind_property(kind: str) -> dict[str, Any]:
    return {
        "type": "string",
        "enum": [kind],
        "description": "Success envelope discriminator.",
    }


def _nullable_string() -> dict[str, Any]:
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _operation_id(route: APIRoute) -> str:
    method = _route_method(route)
    return _operation_id_from_schema(route.path_format, method.lower(), {"tags": route.tags})


def _operation_id_from_schema(path: str, method: str, operation: dict[str, Any]) -> str:
    namespace = _namespace_from_tags(operation.get("tags")) or _namespace_for_path(path)
    segments = _path_segments(path)
    if segments and segments[0] == namespace:
        segments = segments[1:]
    noun = "".join(_camel_token(segment) for segment in segments) or "Root"
    return f"{namespace}{_camel_token(method)}{noun}"


def _route_method(route: APIRoute) -> str:
    methods = sorted((route.methods or set()) & {m.upper() for m in HTTP_METHODS})
    return methods[0] if methods else "GET"


def _namespace_from_tags(tags: object) -> str | None:
    if not isinstance(tags, list) or not tags:
        return None
    first = str(tags[0])
    parts = re.findall(r"[A-Za-z0-9]+", first)
    if not parts:
        return None
    head = parts[0].lower()
    tail = "".join(part.capitalize() for part in parts[1:])
    return head + tail


def _namespace_for_path(path: str) -> str:
    segments = _path_segments(path)
    return segments[0] if segments else "api"


def _path_segments(path: str) -> list[str]:
    raw = [segment for segment in path.split("/") if segment]
    if raw[:2] == ["api", "v1"]:
        raw = raw[2:]
    out: list[str] = []
    for segment in raw:
        if segment.startswith("{") and segment.endswith("}"):
            segment = segment[1:-1].split(":", 1)[0]
        words = re.findall(r"[A-Za-z0-9]+", segment)
        if words:
            out.append("".join(words))
    return out


def _camel_token(value: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", value)
    return "".join(part[:1].upper() + part[1:] for part in parts)
