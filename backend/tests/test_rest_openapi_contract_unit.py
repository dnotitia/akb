"""REST OpenAPI contract guards for SDK code generation."""

import re
from collections import Counter

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

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
