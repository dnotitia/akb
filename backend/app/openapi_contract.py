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
    ("get", "/api/v1/activity/{vault}"): "#/components/schemas/AkbActivityEnvelope",
    ("get", "/api/v1/recent"): "#/components/schemas/AkbRecentChangesEnvelope",
    ("get", "/api/v1/history/{vault}/{doc_id}"): "#/components/schemas/AkbDocumentHistoryEnvelope",
    ("get", "/api/v1/diff/{vault}/{doc_id}"): "#/components/schemas/AkbDocumentDiffEnvelope",
    ("post", "/api/v1/documents"): "#/components/schemas/AkbDocumentWriteEnvelope",
    ("get", "/api/v1/documents/{vault}/{doc_id}"): "#/components/schemas/AkbDocumentEnvelope",
    ("patch", "/api/v1/documents/{vault}/{doc_id}"): "#/components/schemas/AkbDocumentWriteEnvelope",
    ("delete", "/api/v1/documents/{vault}/{doc_id}"): "#/components/schemas/AkbDocumentEnvelope",
    ("get", "/api/v1/browse/{vault}"): "#/components/schemas/AkbDocumentEnvelope",
    ("get", "/api/v1/search"): "#/components/schemas/AkbSearchEnvelope",
    ("get", "/api/v1/drill-down"): "#/components/schemas/AkbDrillDownEnvelope",
    ("get", "/api/v1/grep"): "#/components/schemas/AkbGrepEnvelope",
    ("post", "/api/v1/tables/{vault}"): "#/components/schemas/AkbTableEnvelope",
    ("get", "/api/v1/tables/{vault}"): "#/components/schemas/AkbTableEnvelope",
    ("get", "/api/v1/tables/{vault}/schema"): "#/components/schemas/AkbVaultTableSchemaEnvelope",
    ("post", "/api/v1/tables/{vault}/migrations"): "#/components/schemas/AkbTableMigrationEnvelope",
    ("get", "/api/v1/tables/{vault}/{table}/schema"): "#/components/schemas/AkbTableSchemaEnvelope",
    ("post", "/api/v1/tables/{vault}/sql"): "#/components/schemas/AkbSqlEnvelope",
    ("get", "/api/v1/tables/{vault}/{table}/rows"): "#/components/schemas/AkbTableQueryEnvelope",
    ("post", "/api/v1/tables/{vault}/{table}/rows"): "#/components/schemas/AkbTableQueryEnvelope",
    ("patch", "/api/v1/tables/{vault}/{table}/rows"): "#/components/schemas/AkbTableQueryEnvelope",
    ("delete", "/api/v1/tables/{vault}/{table}/rows"): "#/components/schemas/AkbTableQueryEnvelope",
    ("post", "/api/v1/tables/{vault}/{table}/query"): "#/components/schemas/AkbTableQueryEnvelope",
    ("patch", "/api/v1/tables/{vault}/{table_name}"): "#/components/schemas/AkbTableEnvelope",
    ("delete", "/api/v1/tables/{vault}/{table_name}"): "#/components/schemas/AkbTableEnvelope",
    ("post", "/api/v1/files/{vault}/upload"): "#/components/schemas/AkbFileEnvelope",
    ("post", "/api/v1/files/{vault}/{file_id}/confirm"): "#/components/schemas/AkbFileEnvelope",
    ("get", "/api/v1/files/{vault}/{file_id}/download"): "#/components/schemas/AkbFileEnvelope",
    ("get", "/api/v1/files/{vault}"): "#/components/schemas/AkbFileEnvelope",
    ("delete", "/api/v1/files/{vault}/{file_id}"): "#/components/schemas/AkbFileEnvelope",
    ("get", "/api/v1/graph"): "#/components/schemas/AkbGraphEnvelope",
    ("get", "/api/v1/graph/overview"): "#/components/schemas/AkbGraphOverviewEnvelope",
    ("get", "/api/v1/graph/health"): "#/components/schemas/AkbGraphHealthEnvelope",
    ("get", "/api/v1/relations"): "#/components/schemas/AkbRelationsEnvelope",
    ("post", "/api/v1/relations"): "#/components/schemas/AkbRelationLinkEnvelope",
    ("delete", "/api/v1/relations"): "#/components/schemas/AkbRelationUnlinkEnvelope",
    ("get", "/api/v1/provenance"): "#/components/schemas/AkbProvenanceEnvelope",
}
KIND_ADDITIONAL_SUCCESS_RESPONSE_REFS: dict[tuple[str, str], dict[str, str | None]] = {
    ("post", "/api/v1/tables/{vault}/{table}/rows"): {
        "204": None,
    },
    ("patch", "/api/v1/tables/{vault}/{table}/rows"): {
        "204": None,
    },
    ("delete", "/api/v1/tables/{vault}/{table}/rows"): {
        "204": None,
    },
    ("post", "/api/v1/tables/{vault}/{table}/query"): {
        "201": "#/components/schemas/AkbTableQueryEnvelope",
        "204": None,
    },
}
OPERATION_TAG_OVERRIDES = {
    ("get", "/api/v1/browse/{vault}"): ["documents"],
    ("get", "/api/v1/activity/{vault}"): ["activity"],
    ("get", "/api/v1/recent"): ["activity"],
    ("get", "/api/v1/history/{vault}/{doc_id}"): ["documents"],
    ("get", "/api/v1/diff/{vault}/{doc_id}"): ["documents"],
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
        if not route.operation_id:
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
            if tags := OPERATION_TAG_OVERRIDES.get((method, path)):
                operation["tags"] = tags
            else:
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
        _ensure_additional_success_responses(path, method, responses)
        return
    schema = media.setdefault("schema", {})
    if schema == {}:
        media["schema"] = {"$ref": "#/components/schemas/AkbJsonObject"}
    _ensure_additional_success_responses(path, method, responses)


def _ensure_additional_success_responses(
    path: str,
    method: str,
    responses: dict[str, Any],
) -> None:
    for status, ref in KIND_ADDITIONAL_SUCCESS_RESPONSE_REFS.get((method, path), {}).items():
        response = responses.setdefault(status, {"description": _success_description(status)})
        response.setdefault("description", _success_description(status))
        if ref is None:
            response.pop("content", None)
            continue
        content = response.setdefault("content", {})
        media = content.setdefault("application/json", {})
        media["schema"] = {"$ref": ref}


def _success_description(status: str) -> str:
    if status == "204":
        return "No Content"
    return "Successful Response"


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
                {"$ref": "#/components/schemas/AkbTableMigrationEnvelope"},
                {"$ref": "#/components/schemas/AkbTableSchemaEnvelope"},
                {"$ref": "#/components/schemas/AkbVaultTableSchemaEnvelope"},
                {"$ref": "#/components/schemas/AkbTableQueryEnvelope"},
                {"$ref": "#/components/schemas/AkbTableSqlEnvelope"},
                {"$ref": "#/components/schemas/AkbFileEnvelope"},
                {"$ref": "#/components/schemas/AkbDocumentEnvelope"},
                {"$ref": "#/components/schemas/AkbDocumentWriteEnvelope"},
                {"$ref": "#/components/schemas/AkbSearchEnvelope"},
                {"$ref": "#/components/schemas/AkbDrillDownEnvelope"},
                {"$ref": "#/components/schemas/AkbGrepEnvelope"},
                {"$ref": "#/components/schemas/AkbGraphNeighborsEnvelope"},
                {"$ref": "#/components/schemas/AkbGraphOverviewEnvelope"},
                {"$ref": "#/components/schemas/AkbGraphHealthEnvelope"},
                {"$ref": "#/components/schemas/AkbRelationsEnvelope"},
                {"$ref": "#/components/schemas/AkbRelationLinkEnvelope"},
                {"$ref": "#/components/schemas/AkbRelationUnlinkEnvelope"},
                {"$ref": "#/components/schemas/AkbProvenanceEnvelope"},
                {"$ref": "#/components/schemas/AkbActivityEnvelope"},
                {"$ref": "#/components/schemas/AkbRecentChangesEnvelope"},
                {"$ref": "#/components/schemas/AkbDocumentHistoryEnvelope"},
                {"$ref": "#/components/schemas/AkbDocumentDiffEnvelope"},
            ],
            "discriminator": {
                "propertyName": "kind",
                "mapping": {
                    "table": "#/components/schemas/AkbTableEnvelope",
                    "table_migration": "#/components/schemas/AkbTableMigrationEnvelope",
                    "table_schema": "#/components/schemas/AkbTableSchemaEnvelope",
                    "vault_table_schema": "#/components/schemas/AkbVaultTableSchemaEnvelope",
                    "table_query": "#/components/schemas/AkbTableQueryEnvelope",
                    "table_sql": "#/components/schemas/AkbTableSqlEnvelope",
                    "file": "#/components/schemas/AkbFileEnvelope",
                    "document": "#/components/schemas/AkbDocumentEnvelope",
                    "document_write": "#/components/schemas/AkbDocumentWriteEnvelope",
                    "search": "#/components/schemas/AkbSearchEnvelope",
                    "drill_down": "#/components/schemas/AkbDrillDownEnvelope",
                    "grep": "#/components/schemas/AkbGrepEnvelope",
                    "graph_neighbors": "#/components/schemas/AkbGraphNeighborsEnvelope",
                    "graph_overview": "#/components/schemas/AkbGraphOverviewEnvelope",
                    "graph_health": "#/components/schemas/AkbGraphHealthEnvelope",
                    "relations": "#/components/schemas/AkbRelationsEnvelope",
                    "relation_link": "#/components/schemas/AkbRelationLinkEnvelope",
                    "relation_unlink": "#/components/schemas/AkbRelationUnlinkEnvelope",
                    "provenance": "#/components/schemas/AkbProvenanceEnvelope",
                    "activity": "#/components/schemas/AkbActivityEnvelope",
                    "recent_changes": "#/components/schemas/AkbRecentChangesEnvelope",
                    "document_history": "#/components/schemas/AkbDocumentHistoryEnvelope",
                    "document_diff": "#/components/schemas/AkbDocumentDiffEnvelope",
                },
            },
        },
        "ActivityFileChange": {
            "type": "object",
            "required": ["path", "change"],
            "properties": {
                "path": {"type": "string"},
                "change": {"type": "string", "enum": ["added", "deleted", "modified"]},
            },
            "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
        },
        "ActivityEntry": {
            "type": "object",
            "required": ["hash", "subject", "author", "date", "action", "summary", "agent", "files"],
            "properties": {
                "hash": {"type": "string"},
                "subject": {"type": "string"},
                "author": {"type": "string"},
                "date": {"type": "string", "format": "date-time"},
                "action": {"type": "string"},
                "summary": {"type": "string"},
                "agent": {"type": "string"},
                "files": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/ActivityFileChange"},
                },
                "author_name": _nullable_string(),
            },
            "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
        },
        "RecentDocumentChange": {
            "type": "object",
            "required": ["doc_id", "vault", "path", "title", "type", "commit", "changed_at"],
            "properties": {
                "doc_id": {"type": "string"},
                "vault": {"type": "string"},
                "path": {"type": "string"},
                "title": {"type": "string"},
                "type": {"type": "string"},
                "commit": _nullable_string(),
                "changed_at": {
                    "anyOf": [
                        {"type": "string", "format": "date-time"},
                        {"type": "null"},
                    ],
                },
            },
            "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
        },
        "DocumentHistoryEntry": {
            "type": "object",
            "required": ["hash", "message", "author", "date"],
            "properties": {
                "hash": {"type": "string"},
                "message": {"type": "string"},
                "author": {"type": "string"},
                "date": {"type": "string", "format": "date-time"},
                "author_name": _nullable_string(),
            },
            "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
        },
        "AkbActivityEnvelope": _kind_schema(
            "activity",
            {
                "vault": {"type": "string"},
                "total": {"type": "integer"},
                "activity": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/ActivityEntry"},
                },
            },
            "Git-backed vault activity history.",
            required=("kind", "vault", "total", "activity"),
        ),
        "AkbRecentChangesEnvelope": _kind_schema(
            "recent_changes",
            {
                "changes": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/RecentDocumentChange"},
                },
            },
            "Recent documents visible to the current user.",
            required=("kind", "changes"),
        ),
        "AkbDocumentHistoryEnvelope": _kind_schema(
            "document_history",
            {
                "uri": {"type": "string"},
                "history": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/DocumentHistoryEntry"},
                },
            },
            "Git-backed document version history.",
            required=("kind", "uri", "history"),
        ),
        "AkbDocumentDiffEnvelope": _kind_schema(
            "document_diff",
            {
                "file": {"type": "string"},
                "commit": {"type": "string"},
                "type": {
                    "type": "string",
                    "enum": ["added", "deleted", "modified", "unknown", "unchanged"],
                },
                "diff": {"type": "string"},
                "error": _nullable_string(),
            },
            "Document diff at one commit.",
            required=("kind", "file", "commit", "type", "diff"),
        ),
        "AkbGraphNode": {
            "type": "object",
            "required": ["uri", "name", "resource_type"],
            "properties": {
                "uri": {"type": "string"},
                "name": {"type": "string"},
                "resource_type": {"type": "string", "enum": ["doc", "table", "file"]},
                "depth": _nullable_integer(),
                "degree": _nullable_integer(),
            },
            "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
        },
        "AkbGraphEdge": {
            "type": "object",
            "required": ["source", "target", "relation", "kind"],
            "properties": {
                "source": {"type": "string"},
                "target": {"type": "string"},
                "relation": {"type": "string", "enum": [
                    "depends_on", "related_to", "implements", "references",
                    "attached_to", "derived_from", "links_to",
                ]},
                "kind": {"type": "string", "enum": ["implicit", "explicit"]},
            },
            "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
        },
        "AkbRelation": {
            "type": "object",
            "required": ["direction", "relation", "uri", "resource_type"],
            "properties": {
                "direction": {"type": "string", "enum": ["incoming", "outgoing"]},
                "relation": {"type": "string", "enum": [
                    "depends_on", "related_to", "implements", "references",
                    "attached_to", "derived_from", "links_to",
                ]},
                "uri": {"type": "string"},
                "resource_type": {"type": "string", "enum": ["doc", "table", "file"]},
                "name": _nullable_string(),
            },
            "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
        },
        "AkbGraphEnvelope": {
            "oneOf": [
                {"$ref": "#/components/schemas/AkbGraphNeighborsEnvelope"},
                {"$ref": "#/components/schemas/AkbGraphOverviewEnvelope"},
            ],
            "discriminator": {
                "propertyName": "kind",
                "mapping": {
                    "graph_neighbors": "#/components/schemas/AkbGraphNeighborsEnvelope",
                    "graph_overview": "#/components/schemas/AkbGraphOverviewEnvelope",
                },
            },
        },
        "AkbGraphNeighborsEnvelope": _kind_schema(
            "graph_neighbors",
            {
                "nodes": {"type": "array", "items": {"$ref": "#/components/schemas/AkbGraphNode"}},
                "edges": {"type": "array", "items": {"$ref": "#/components/schemas/AkbGraphEdge"}},
            },
            "Resource-centered graph neighborhood.",
            required=("kind", "nodes", "edges"),
        ),
        "AkbGraphOverviewEnvelope": _kind_schema(
            "graph_overview",
            {
                "nodes": {"type": "array", "items": {"$ref": "#/components/schemas/AkbGraphNode"}},
                "edges": {"type": "array", "items": {"$ref": "#/components/schemas/AkbGraphEdge"}},
                "nodes_total": {"type": "integer"},
                "edges_total": {"type": "integer"},
                "returned": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "orphans_returned": {"type": "integer"},
                "orphans_truncated": {"type": "boolean"},
            },
            "Vault graph overview with connected and orphan totals.",
            required=("kind", "nodes", "edges", "nodes_total", "edges_total", "returned",
                      "truncated", "orphans_returned", "orphans_truncated"),
        ),
        "AkbGraphHealthEnvelope": _kind_schema(
            "graph_health",
            {
                "hubs": {"type": "array", "items": {"$ref": "#/components/schemas/AkbGraphNode"}},
                "orphans": {
                    "type": "object",
                    "required": ["count", "sample"],
                    "properties": {
                        "count": {"type": "integer"},
                        "sample": {"type": "array", "items": {"$ref": "#/components/schemas/AkbGraphNode"}},
                    },
                    "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
                },
            },
            "Vault graph health audit.",
            required=("kind", "hubs", "orphans"),
        ),
        "AkbRelationsEnvelope": _kind_schema(
            "relations",
            {
                "uri": {"type": "string"},
                "relations": {"type": "array", "items": {"$ref": "#/components/schemas/AkbRelation"}},
            },
            "One-hop resource relations.",
            required=("kind", "uri", "relations"),
        ),
        "AkbRelationLinkEnvelope": _kind_schema(
            "relation_link",
            {
                "linked": {"type": "boolean"},
                "source": {"type": "string"},
                "target": {"type": "string"},
                "relation": {"type": "string", "enum": [
                    "depends_on", "related_to", "implements", "references",
                    "attached_to", "derived_from",
                ]},
            },
            "Relation link result.",
            required=("kind", "linked", "source", "target", "relation"),
        ),
        "AkbRelationUnlinkEnvelope": _kind_schema(
            "relation_unlink",
            {
                "unlinked": {"type": "integer"},
                "source": {"type": "string"},
                "target": {"type": "string"},
            },
            "Relation unlink result.",
            required=("kind", "unlinked", "source", "target"),
        ),
        "AkbProvenanceEnvelope": _kind_schema(
            "provenance",
            {
                "doc_id": {"type": "string"},
                "title": {"type": "string"},
                "path": {"type": "string"},
                "vault": {"type": "string"},
                "uri": {"type": "string"},
                "created_by": _nullable_string(),
                "created_at": _nullable_string(),
                "updated_at": _nullable_string(),
                "current_commit": _nullable_string(),
                "relations": {"type": "array", "items": {"$ref": "#/components/schemas/AkbRelation"}},
            },
            "Flat document provenance payload.",
            required=("kind", "doc_id", "title", "path", "vault", "uri", "created_by",
                      "created_at", "updated_at", "current_commit", "relations"),
        ),
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
        "AkbTableMigrationEnvelope": _kind_schema(
            "table_migration",
            {
                "id": {"type": "string"},
                "vault": {"type": "string"},
                "idempotency_key": {"type": "string"},
                "checksum": {"type": "string"},
                "applied": {"type": "boolean"},
                "applied_at": {"type": "string", "format": "date-time"},
                "operations": {"type": "integer"},
                "results": JSON_OBJECT_ARRAY_SCHEMA,
            },
            "Idempotent table schema migration result.",
            required=(
                "kind",
                "vault",
                "idempotency_key",
                "checksum",
                "applied",
                "operations",
                "results",
            ),
        ),
        "AkbTableSchemaEnvelope": _kind_schema(
            "table_schema",
            {
                "uri": {"type": "string"},
                "vault": {"type": "string"},
                "collection": _nullable_string(),
                "name": {"type": "string"},
                "table": {"type": "string"},
                "sql_name": {"type": "string"},
                "description": _nullable_string(),
                "columns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": [
                            "name",
                            "type",
                            "required",
                            "unique",
                            "index",
                            "drift",
                        ],
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string"},
                            "required": {"type": "boolean"},
                            "default": {"$ref": "#/components/schemas/AkbJsonValue"},
                            "check": {"$ref": "#/components/schemas/AkbJsonValue"},
                            "enum": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/AkbJsonValue"},
                            },
                            "unique": {"type": "boolean"},
                            "index": {"type": "boolean"},
                            "references": {"$ref": "#/components/schemas/AkbJsonValue"},
                            "on_delete": _nullable_string(),
                            "pg_type": _nullable_string(),
                            "drift": JSON_OBJECT_SCHEMA,
                        },
                        "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
                    },
                },
                "unique_keys": JSON_OBJECT_ARRAY_SCHEMA,
                "indexes": JSON_OBJECT_ARRAY_SCHEMA,
                "pg_types": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "system_columns": {"type": "array", "items": {"type": "string"}},
                "drift": JSON_OBJECT_SCHEMA,
            },
            "Merged registry and live PostgreSQL schema for a vault table.",
            required=("kind", "vault", "name", "columns", "pg_types", "drift"),
        ),
        "AkbVaultTableSchemaEnvelope": _kind_schema(
            "vault_table_schema",
            {
                "vault": {"type": "string"},
                "tables": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/AkbTableSchemaEnvelope"},
                },
                "total": {"type": "integer"},
            },
            "Merged registry and live PostgreSQL schemas for every table in a vault.",
            required=("kind", "vault", "tables", "total"),
        ),
        "AkbTableQueryEnvelope": _kind_schema(
            "table_query",
            {
                "vault": {"type": "string"},
                "table": {"type": "string"},
                "vaults": {"type": "array", "items": {"type": "string"}},
                "columns": {"type": "array", "items": {"type": "string"}},
                "items": JSON_OBJECT_ARRAY_SCHEMA,
                "total": {"type": "integer"},
            },
            "Structured table query or SQL SELECT/WITH success envelope.",
            required=("kind", "columns", "items", "total"),
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
        "AkbDocumentEnvelope": _kind_schema(
            "document",
            {
                "uri": {"type": "string"},
                "vault": {"type": "string"},
                "path": {"type": "string"},
                "title": {"type": "string"},
                "type": {"type": "string"},
                "status": {"type": "string"},
                "summary": _nullable_string(),
                "domain": _nullable_string(),
                "created_by": _nullable_string(),
                "created_by_name": _nullable_string(),
                "created_at": {"type": "string", "format": "date-time"},
                "updated_at": {"type": "string", "format": "date-time"},
                "current_commit": _nullable_string(),
                "content_hash": _nullable_string(),
                "hash_algorithm": _nullable_string(),
                "tags": {"type": "array", "items": {"type": "string"}},
                "content": _nullable_string(),
                "is_public": {"type": "boolean"},
                "public_slug": _nullable_string(),
                "metadata_is_current": {"type": "boolean"},
                "items": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/BrowseItem"},
                },
                "hint": _nullable_string(),
                "deleted": {"type": "boolean"},
            },
            "Document read, browse, and delete success envelope.",
        ),
        "AkbDocumentWriteEnvelope": _kind_schema(
            "document_write",
            {
                "uri": {"type": "string"},
                "vault": {"type": "string"},
                "path": {"type": "string"},
                "commit_hash": {"type": "string"},
                "current_commit": _nullable_string(),
                "previous_commit": _nullable_string(),
                "content_hash": _nullable_string(),
                "previous_content_hash": _nullable_string(),
                "hash_algorithm": _nullable_string(),
                "action": _nullable_string(),
                "chunks_indexed": {"type": "integer"},
                "entities_found": {"type": "integer"},
            },
            "Document put and update success envelope.",
            required=("kind", "uri", "vault", "path", "commit_hash", "chunks_indexed", "entities_found"),
        ),
        "AkbSearchEnvelope": _kind_schema(
            "search",
            {
                "query": {"type": "string"},
                "total": {"type": "integer"},
                "returned": {"type": "integer"},
                "total_matches": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "hint": _nullable_string(),
                "degraded": {"type": "boolean"},
                "degradation_reason": _nullable_string(),
                "results": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/SearchResult"},
                },
            },
            "Hybrid search success envelope.",
            required=("kind", "query", "total", "returned", "total_matches", "results"),
        ),
        "AkbDrillDownEnvelope": _kind_schema(
            "drill_down",
            {
                "uri": {"type": "string"},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["chunk_index"],
                        "properties": {
                            "section_path": _nullable_string(),
                            "content": _nullable_string(),
                            "chunk_index": {"type": "integer"},
                        },
                        "additionalProperties": {"$ref": "#/components/schemas/AkbJsonValue"},
                    },
                },
            },
            "Document drill-down success envelope.",
            required=("kind", "uri", "sections"),
        ),
        "AkbGrepEnvelope": _kind_schema(
            "grep",
            {
                "pattern": {"type": "string"},
                "regex": {"type": "boolean"},
                "error": _nullable_string(),
                "returned_docs": _nullable_integer(),
                "returned_matches": _nullable_integer(),
                "total_docs": _nullable_integer(),
                "total_matches": _nullable_integer(),
                "truncated": _nullable_boolean(),
                "hint": _nullable_string(),
                "results": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/GrepResult"},
                        },
                        {"type": "null"},
                    ],
                },
                "by_doc": {
                    "anyOf": [
                        {
                            "type": "object",
                            "additionalProperties": {"type": "integer"},
                        },
                        {"type": "null"},
                    ],
                },
                "n_files": _nullable_integer(),
                "files": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ],
                },
            },
            "Literal grep success envelope.",
            required=("kind", "pattern", "regex"),
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


def _nullable_integer() -> dict[str, Any]:
    return {"anyOf": [{"type": "integer"}, {"type": "null"}]}


def _nullable_boolean() -> dict[str, Any]:
    return {"anyOf": [{"type": "boolean"}, {"type": "null"}]}


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
