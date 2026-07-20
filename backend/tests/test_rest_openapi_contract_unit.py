"""REST OpenAPI contract guards for SDK code generation."""

import re
from collections import Counter

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from app.api.routes.tables import _raise_service_error
from app.main import app


HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
SUCCESS_STATUSES = ("200", "201", "202")
ERROR_STATUSES = ("400", "401", "403", "404", "409", "422", "500")


def _api_operations():
    schema = app.openapi()
    for path, path_item in schema["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        for method, operation in path_item.items():
            if method in HTTP_METHODS:
                yield path, method, operation


def test_bearer_auth_scheme_is_registered():
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["bearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "JWT or AKB personal access token supplied as a Bearer token.",
    }


def test_api_operations_have_codegen_safe_ids_tags_and_success_schema():
    operation_ids: list[str] = []
    for path, method, operation in _api_operations():
        op_id = operation.get("operationId")
        assert op_id, f"{method.upper()} {path} missing operationId"
        operation_ids.append(op_id)
        assert re.fullmatch(r"[a-z][A-Za-z0-9]*", op_id), (
            f"{method.upper()} {path} operationId is not camelCase: {op_id}"
        )
        assert "_api_" not in op_id and "__" not in op_id
        assert operation.get("tags"), f"{method.upper()} {path} missing tags"

        responses = operation.get("responses", {})
        success = next((responses.get(code) for code in SUCCESS_STATUSES if code in responses), None)
        if success is None and any(str(code).startswith("3") for code in responses):
            assert "200" not in responses
            continue
        assert success is not None, f"{method.upper()} {path} missing success response"
        content = success.get("content", {})
        if "application/json" in content:
            schema = content["application/json"].get("schema")
            assert schema, f"{method.upper()} {path} missing JSON success schema"
        else:
            assert content, f"{method.upper()} {path} missing success content schema"

    duplicates = [op_id for op_id, count in Counter(operation_ids).items() if count > 1]
    assert duplicates == []


def test_api_error_responses_reference_single_akb_error_component():
    schema = app.openapi()
    akb_error = schema["components"]["schemas"]["AkbError"]
    assert {"message", "code"}.issubset(akb_error["properties"])
    assert "details" in akb_error["properties"]
    assert "hint" in akb_error["properties"]
    assert "detail" in akb_error["properties"]
    assert "password_required" in akb_error["properties"]
    assert "slug" in akb_error["properties"]

    for path, method, operation in _api_operations():
        responses = operation.get("responses", {})
        for status in ERROR_STATUSES:
            error_schema = (
                responses.get(status, {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            assert error_schema == {"$ref": "#/components/schemas/AkbError"}, (
                f"{method.upper()} {path} {status} does not reference AkbError"
            )


def test_success_envelope_components_are_kind_discriminated():
    schemas = app.openapi()["components"]["schemas"]
    union = schemas["AkbSuccessEnvelope"]
    assert union["discriminator"] == {
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
        },
    }
    for name, kind in (
        ("AkbTableEnvelope", "table"),
        ("AkbTableMigrationEnvelope", "table_migration"),
        ("AkbTableSchemaEnvelope", "table_schema"),
        ("AkbVaultTableSchemaEnvelope", "vault_table_schema"),
        ("AkbTableQueryEnvelope", "table_query"),
        ("AkbTableSqlEnvelope", "table_sql"),
        ("AkbFileEnvelope", "file"),
        ("AkbDocumentEnvelope", "document"),
        ("AkbDocumentWriteEnvelope", "document_write"),
        ("AkbSearchEnvelope", "search"),
        ("AkbDrillDownEnvelope", "drill_down"),
        ("AkbGrepEnvelope", "grep"),
        ("AkbGraphNeighborsEnvelope", "graph_neighbors"),
        ("AkbGraphOverviewEnvelope", "graph_overview"),
        ("AkbGraphHealthEnvelope", "graph_health"),
        ("AkbRelationsEnvelope", "relations"),
        ("AkbRelationLinkEnvelope", "relation_link"),
        ("AkbRelationUnlinkEnvelope", "relation_unlink"),
        ("AkbProvenanceEnvelope", "provenance"),
    ):
        schema = schemas[name]
        assert "kind" in schema["required"]
        assert schema["properties"]["kind"]["enum"] == [kind]


def test_kind_envelope_routes_reference_typed_success_schemas():
    schema = app.openapi()
    expected = {
        ("/api/v1/documents", "post", "200"): "AkbDocumentWriteEnvelope",
        ("/api/v1/documents/{vault}/{doc_id}", "get", "200"): "AkbDocumentEnvelope",
        ("/api/v1/documents/{vault}/{doc_id}", "patch", "200"): "AkbDocumentWriteEnvelope",
        ("/api/v1/documents/{vault}/{doc_id}", "delete", "200"): "AkbDocumentEnvelope",
        ("/api/v1/browse/{vault}", "get", "200"): "AkbDocumentEnvelope",
        ("/api/v1/search", "get", "200"): "AkbSearchEnvelope",
        ("/api/v1/drill-down", "get", "200"): "AkbDrillDownEnvelope",
        ("/api/v1/grep", "get", "200"): "AkbGrepEnvelope",
        ("/api/v1/tables/{vault}", "post", "200"): "AkbTableEnvelope",
        ("/api/v1/tables/{vault}", "get", "200"): "AkbTableEnvelope",
        ("/api/v1/tables/{vault}/schema", "get", "200"): "AkbVaultTableSchemaEnvelope",
        ("/api/v1/tables/{vault}/migrations", "post", "200"): "AkbTableMigrationEnvelope",
        ("/api/v1/tables/{vault}/{table}/schema", "get", "200"): "AkbTableSchemaEnvelope",
        ("/api/v1/tables/{vault}/sql", "post", "200"): "AkbSqlEnvelope",
        ("/api/v1/tables/{vault}/{table}/rows", "get", "200"): "AkbTableQueryEnvelope",
        ("/api/v1/tables/{vault}/{table}/rows", "post", "201"): "AkbTableQueryEnvelope",
        ("/api/v1/tables/{vault}/{table}/rows", "patch", "200"): "AkbTableQueryEnvelope",
        ("/api/v1/tables/{vault}/{table}/rows", "delete", "200"): "AkbTableQueryEnvelope",
        ("/api/v1/tables/{vault}/{table}/query", "post", "200"): "AkbTableQueryEnvelope",
        ("/api/v1/tables/{vault}/{table_name}", "patch", "200"): "AkbTableEnvelope",
        ("/api/v1/tables/{vault}/{table_name}", "delete", "200"): "AkbTableEnvelope",
        ("/api/v1/files/{vault}/upload", "post", "200"): "AkbFileEnvelope",
        ("/api/v1/files/{vault}/{file_id}/confirm", "post", "200"): "AkbFileEnvelope",
        ("/api/v1/files/{vault}/{file_id}/download", "get", "200"): "AkbFileEnvelope",
        ("/api/v1/files/{vault}", "get", "200"): "AkbFileEnvelope",
        ("/api/v1/files/{vault}/{file_id}", "delete", "200"): "AkbFileEnvelope",
        ("/api/v1/graph", "get", "200"): "AkbGraphEnvelope",
        ("/api/v1/graph/overview", "get", "200"): "AkbGraphOverviewEnvelope",
        ("/api/v1/graph/health", "get", "200"): "AkbGraphHealthEnvelope",
        ("/api/v1/relations", "get", "200"): "AkbRelationsEnvelope",
        ("/api/v1/relations", "post", "200"): "AkbRelationLinkEnvelope",
        ("/api/v1/relations", "delete", "200"): "AkbRelationUnlinkEnvelope",
        ("/api/v1/provenance", "get", "200"): "AkbProvenanceEnvelope",
    }
    for (path, method, status), component in expected.items():
        success_schema = (
            schema["paths"][path][method]["responses"][status]
            ["content"]["application/json"]["schema"]
        )
        assert success_schema == {"$ref": f"#/components/schemas/{component}"}


def test_graph_rest_openapi_contract_is_codegen_typed():
    schema = app.openapi()
    paths = schema["paths"]
    expected = {
        ("/api/v1/graph", "get"): "graphNeighbors",
        ("/api/v1/graph/overview", "get"): "graphOverview",
        ("/api/v1/graph/health", "get"): "graphHealth",
        ("/api/v1/relations", "get"): "graphRelations",
        ("/api/v1/relations", "post"): "graphLink",
        ("/api/v1/relations", "delete"): "graphUnlink",
        ("/api/v1/provenance", "get"): "graphProvenance",
    }
    for (path, method), operation_id in expected.items():
        operation = paths[path][method]
        assert operation["operationId"] == operation_id
        assert operation["tags"] == ["graph"]

    schemas = schema["components"]["schemas"]
    graph_union = schemas["AkbGraphEnvelope"]
    assert graph_union["discriminator"] == {
        "propertyName": "kind",
        "mapping": {
            "graph_neighbors": "#/components/schemas/AkbGraphNeighborsEnvelope",
            "graph_overview": "#/components/schemas/AkbGraphOverviewEnvelope",
        },
    }
    assert {item["$ref"] for item in graph_union["oneOf"]} == set(
        graph_union["discriminator"]["mapping"].values()
    )

    node = schemas["AkbGraphNode"]
    assert node["properties"]["resource_type"]["enum"] == ["doc", "table", "file"]
    assert {"depth", "degree"}.issubset(node["properties"])
    edge = schemas["AkbGraphEdge"]
    assert edge["properties"]["kind"]["enum"] == ["implicit", "explicit"]
    relation = schemas["AkbRelation"]
    assert relation["properties"]["direction"]["enum"] == ["incoming", "outgoing"]
    assert "links_to" in relation["properties"]["relation"]["enum"]

    get_relations = paths["/api/v1/relations"]["get"]
    params = {param["name"]: param for param in get_relations["parameters"]}
    assert set(params["direction"]["schema"]["enum"]) == {"incoming", "outgoing", "both"}
    type_schema = params["type"]["schema"]
    type_enum = next(item["enum"] for item in type_schema["anyOf"] if "enum" in item)
    assert "links_to" in type_enum

    link_schema = schemas["LinkRequest"]
    assert "links_to" not in link_schema["properties"]["relation"]["enum"]


def test_document_openapi_contract_is_codegen_typed():
    schema = app.openapi()
    paths = schema["paths"]
    put = paths["/api/v1/documents"]["post"]
    get = paths["/api/v1/documents/{vault}/{doc_id}"]["get"]
    patch = paths["/api/v1/documents/{vault}/{doc_id}"]["patch"]
    delete = paths["/api/v1/documents/{vault}/{doc_id}"]["delete"]
    browse = paths["/api/v1/browse/{vault}"]["get"]

    assert put["operationId"] == "documentsPutDocument"
    assert get["operationId"] == "documentsGetDocument"
    assert patch["operationId"] == "documentsUpdateDocument"
    assert delete["operationId"] == "documentsDeleteDocument"
    assert browse["operationId"] == "documentsBrowseVault"
    assert browse["tags"] == ["documents"]

    for operation in (get, delete, browse):
        assert (
            operation["responses"]["200"]["content"]["application/json"]["schema"]
            == {"$ref": "#/components/schemas/AkbDocumentEnvelope"}
        )
    for operation in (put, patch):
        assert (
            operation["responses"]["200"]["content"]["application/json"]["schema"]
            == {"$ref": "#/components/schemas/AkbDocumentWriteEnvelope"}
        )

    schemas = schema["components"]["schemas"]
    document = schemas["AkbDocumentEnvelope"]
    assert document["properties"]["kind"]["enum"] == ["document"]
    assert "items" in document["properties"]
    assert "deleted" in document["properties"]
    assert "current_commit" in document["properties"]

    write = schemas["AkbDocumentWriteEnvelope"]
    assert write["properties"]["kind"]["enum"] == ["document_write"]
    assert {"kind", "uri", "vault", "path", "commit_hash", "chunks_indexed", "entities_found"}.issubset(
        write["required"]
    )


def test_search_openapi_contract_is_codegen_typed():
    schema = app.openapi()
    paths = schema["paths"]
    search = paths["/api/v1/search"]["get"]
    drill_down = paths["/api/v1/drill-down"]["get"]
    grep = paths["/api/v1/grep"]["get"]

    assert search["operationId"] == "searchSearchDocuments"
    assert drill_down["operationId"] == "searchDrillDown"
    assert grep["operationId"] == "searchGrepDocuments"

    assert (
        search["responses"]["200"]["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/AkbSearchEnvelope"}
    )
    assert (
        drill_down["responses"]["200"]["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/AkbDrillDownEnvelope"}
    )
    assert (
        grep["responses"]["200"]["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/AkbGrepEnvelope"}
    )

    search_params = {param["name"]: param for param in search["parameters"]}
    for name in (
        "q",
        "mode",
        "rerank",
        "vault",
        "collection",
        "type",
        "tags",
        "limit",
        "include_archived",
        "source_uris",
    ):
        assert search_params[name]["in"] == "query"

    mode_schema = search_params["mode"]["schema"]
    assert mode_schema["type"] == "string"
    assert mode_schema.get("enum", mode_schema.get("const")) in (["hybrid"], "hybrid")
    rerank_schema = search_params["rerank"]["schema"]
    assert rerank_schema.get("type") == "boolean" or {"type": "boolean"} in rerank_schema.get("anyOf", [])

    grep_params = {param["name"]: param for param in grep["parameters"]}
    for name in ("q", "vault", "collection", "regex", "case_sensitive", "limit", "count_only", "files_with_matches"):
        assert grep_params[name]["in"] == "query"

    schemas = schema["components"]["schemas"]
    assert {"kind", "query", "total", "returned", "total_matches", "results"}.issubset(
        schemas["AkbSearchEnvelope"]["required"]
    )
    assert {"kind", "uri", "sections"}.issubset(schemas["AkbDrillDownEnvelope"]["required"])
    assert {"kind", "pattern", "regex"}.issubset(schemas["AkbGrepEnvelope"]["required"])


def test_row_read_openapi_contract_is_codegen_typed():
    schema = app.openapi()
    paths = schema["paths"]
    rows = paths["/api/v1/tables/{vault}/{table}/rows"]["get"]
    query = paths["/api/v1/tables/{vault}/{table}/query"]["post"]

    assert rows["operationId"] == "tablesSelectRows"
    assert query["operationId"] == "tablesQueryRows"
    assert rows["tags"] == ["tables"]
    assert query["tags"] == ["tables"]
    for operation in (rows, query):
        assert (
            operation["responses"]["200"]["content"]["application/json"]["schema"]
            == {"$ref": "#/components/schemas/AkbTableQueryEnvelope"}
        )
        for status in ERROR_STATUSES:
            assert (
                operation["responses"][status]["content"]["application/json"]["schema"]
                == {"$ref": "#/components/schemas/AkbError"}
            )

    query_request_schema = query["requestBody"]["content"]["application/json"]["schema"]
    assert query_request_schema == {"$ref": "#/components/schemas/QueryRowsRequest"}

    table_query = schema["components"]["schemas"]["AkbTableQueryEnvelope"]
    assert {"kind", "columns", "items", "total"}.issubset(table_query["required"])
    assert {"vault", "table", "vaults"}.issubset(table_query["properties"])


def test_row_write_openapi_contract_is_codegen_typed():
    schema = app.openapi()
    paths = schema["paths"]
    rows_path = paths["/api/v1/tables/{vault}/{table}/rows"]
    query = paths["/api/v1/tables/{vault}/{table}/query"]["post"]

    expected = {
        "post": ("tablesInsertRows", "201"),
        "patch": ("tablesUpdateRows", "200"),
        "delete": ("tablesDeleteRows", "200"),
    }
    for method, (operation_id, success_status) in expected.items():
        operation = rows_path[method]
        assert operation["operationId"] == operation_id
        assert operation["tags"] == ["tables"]
        assert (
            operation["responses"][success_status]["content"]["application/json"]["schema"]
            == {"$ref": "#/components/schemas/AkbTableQueryEnvelope"}
        )
        assert "content" not in operation["responses"]["204"]
        for status in ERROR_STATUSES:
            assert (
                operation["responses"][status]["content"]["application/json"]["schema"]
                == {"$ref": "#/components/schemas/AkbError"}
            )

    assert query["operationId"] == "tablesQueryRows"
    assert (
        query["responses"]["201"]["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/AkbTableQueryEnvelope"}
    )
    assert "content" not in query["responses"]["204"]


def test_alter_table_openapi_contract_is_codegen_typed():
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/tables/{vault}/{table_name}"]["patch"]

    assert operation["operationId"] == "tablesAlterTable"
    assert operation["tags"] == ["tables"]
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/AkbTableEnvelope"}
    )
    assert (
        operation["requestBody"]["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/AlterTableRequest"}
    )
    for status in ERROR_STATUSES:
        assert (
            operation["responses"][status]["content"]["application/json"]["schema"]
            == {"$ref": "#/components/schemas/AkbError"}
        )


def test_table_migration_openapi_contract_is_codegen_typed():
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/tables/{vault}/migrations"]["post"]

    assert operation["operationId"] == "tablesApplyMigration"
    assert operation["tags"] == ["tables"]
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/AkbTableMigrationEnvelope"}
    )
    idempotency = next(
        param for param in operation["parameters"] if param["name"] == "Idempotency-Key"
    )
    assert idempotency["in"] == "header"
    assert idempotency["required"] is True
    assert idempotency["schema"]["type"] == "string"
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "items": {"additionalProperties": True, "type": "object"},
        "title": "Operations",
        "type": "array",
    }
    migration = schema["components"]["schemas"]["AkbTableMigrationEnvelope"]
    assert {"kind", "vault", "idempotency_key", "checksum", "applied", "operations", "results"}.issubset(
        migration["required"]
    )
    for status in ERROR_STATUSES:
        assert (
            operation["responses"][status]["content"]["application/json"]["schema"]
            == {"$ref": "#/components/schemas/AkbError"}
        )


def test_table_schema_openapi_contract_is_codegen_typed():
    schema = app.openapi()
    paths = schema["paths"]
    table_schema = paths["/api/v1/tables/{vault}/{table}/schema"]["get"]
    vault_schema = paths["/api/v1/tables/{vault}/schema"]["get"]

    assert table_schema["operationId"] == "tablesGetTableSchema"
    assert vault_schema["operationId"] == "tablesGetVaultSchema"
    assert table_schema["tags"] == ["tables"]
    assert vault_schema["tags"] == ["tables"]
    assert (
        table_schema["responses"]["200"]["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/AkbTableSchemaEnvelope"}
    )
    assert (
        vault_schema["responses"]["200"]["content"]["application/json"]["schema"]
        == {"$ref": "#/components/schemas/AkbVaultTableSchemaEnvelope"}
    )

    component = schema["components"]["schemas"]["AkbTableSchemaEnvelope"]
    assert {"kind", "vault", "name", "columns", "pg_types", "drift"}.issubset(
        component["required"]
    )
    column_props = component["properties"]["columns"]["items"]["properties"]
    for field in (
        "name",
        "type",
        "required",
        "default",
        "check",
        "enum",
        "unique",
        "index",
        "references",
        "on_delete",
        "pg_type",
        "drift",
    ):
        assert field in column_props
    for operation in (table_schema, vault_schema):
        for status in ERROR_STATUSES:
            assert (
                operation["responses"][status]["content"]["application/json"]["schema"]
                == {"$ref": "#/components/schemas/AkbError"}
            )


def test_http_exception_runtime_shape_matches_akb_error_schema():
    test_app = FastAPI()

    @test_app.get("/boom")
    async def boom():
        raise HTTPException(
            status_code=409,
            detail={"message": "Collection is not empty", "doc_count": 2},
        )

    for handler_key, handler in app.exception_handlers.items():
        test_app.add_exception_handler(handler_key, handler)

    response = TestClient(test_app).get("/boom")
    assert response.status_code == 409
    assert response.json() == {
        "message": "Collection is not empty",
        "error": "Collection is not empty",
        "code": "conflict",
        "detail": {"message": "Collection is not empty", "doc_count": 2},
        "details": {"doc_count": 2},
    }


def test_http_exception_preserves_nested_details_as_akb_error_details():
    test_app = FastAPI()

    @test_app.get("/boom")
    async def boom():
        raise HTTPException(
            status_code=400,
            detail={
                "message": "SQL failed",
                "code": "sql_error",
                "details": {"pg_sqlstate": "42601"},
            },
        )

    for handler_key, handler in app.exception_handlers.items():
        test_app.add_exception_handler(handler_key, handler)

    response = TestClient(test_app).get("/boom")
    assert response.status_code == 400
    assert response.json() == {
        "message": "SQL failed",
        "error": "SQL failed",
        "code": "sql_error",
        "detail": {
            "message": "SQL failed",
            "code": "sql_error",
            "details": {"pg_sqlstate": "42601"},
        },
        "details": {"pg_sqlstate": "42601"},
    }


def test_table_rest_bridge_promotes_service_err_dict_to_http_akb_error():
    with pytest.raises(HTTPException) as exc:
        _raise_service_error({
            "error": "Multi-statement SQL is not allowed.",
            "code": "multi_statement",
            "hint": "Send one statement at a time.",
            "details": {"separator_count": 2},
        })

    assert exc.value.status_code == 400
    assert exc.value.detail == {
        "message": "Multi-statement SQL is not allowed.",
        "code": "multi_statement",
        "hint": "Send one statement at a time.",
        "details": {"separator_count": 2},
    }


def test_framework_405_runtime_shape_is_not_internal():
    test_app = FastAPI()

    @test_app.get("/only-get")
    async def only_get():
        return {"ok": True}

    for handler_key, handler in app.exception_handlers.items():
        test_app.add_exception_handler(handler_key, handler)

    response = TestClient(test_app).post("/only-get")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"


def test_password_gate_compat_fields_stay_top_level():
    test_app = FastAPI()

    @test_app.get("/private-publication")
    async def private_publication():
        raise HTTPException(
            status_code=401,
            detail={"message": "Password required", "password_required": True, "slug": "s1"},
        )

    for handler_key, handler in app.exception_handlers.items():
        test_app.add_exception_handler(handler_key, handler)

    response = TestClient(test_app).get("/private-publication")
    assert response.status_code == 401
    body = response.json()
    assert body["message"] == "Password required"
    assert body["code"] == "permission_denied"
    assert body["password_required"] is True
    assert body["slug"] == "s1"
    assert body["details"] == {"password_required": True, "slug": "s1"}


def test_unhandled_exception_runtime_shape_matches_akb_error_schema():
    test_app = FastAPI()

    @test_app.get("/boom")
    async def boom():
        raise RuntimeError("hidden traceback detail")

    for handler_key, handler in app.exception_handlers.items():
        test_app.add_exception_handler(handler_key, handler)

    response = TestClient(test_app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    assert response.json() == {
        "message": "Internal server error",
        "error": "Internal server error",
        "code": "internal",
        "detail": "Internal server error",
    }


def test_redirect_operations_do_not_advertise_json_success():
    schema = app.openapi()
    for path in (
        "/api/v1/auth/keycloak/login",
        "/api/v1/auth/keycloak/callback",
        "/api/v1/auth/keycloak/logout",
    ):
        responses = schema["paths"][path]["get"]["responses"]
        assert "302" in responses
        assert "200" not in responses
        assert "application/json" not in responses["302"].get("content", {})


def test_non_json_success_operations_keep_their_media_types():
    schema = app.openapi()
    raw_content = schema["paths"]["/api/v1/public/{slug}/raw"]["get"]["responses"]["200"]["content"]
    assert "application/octet-stream" in raw_content
    assert "application/json" not in raw_content

    download_content = schema["paths"]["/api/v1/public/{slug}/download"]["get"]["responses"]["200"]["content"]
    assert "application/octet-stream" in download_content
    assert "text/csv" in download_content
    assert "application/json" not in download_content

    help_content = schema["paths"]["/api/v1/help/skill-template"]["get"]["responses"]["200"]["content"]
    assert "text/markdown" in help_content
    assert "application/json" not in help_content
