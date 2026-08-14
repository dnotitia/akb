"""Live OpenAPI drift guards for the generic app control plane."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile

from app.config import settings

settings.git_storage_path = tempfile.mkdtemp(prefix="akb-control-plane-openapi-")

from app.main import app


FIXTURE = (
    Path(__file__).parents[2]
    / "packages"
    / "akb-client"
    / "test"
    / "fixtures"
    / "openapi.control-plane.json"
)
ERROR_STATUSES = ("400", "401", "403", "404", "409", "422", "500")


def test_control_plane_fixture_matches_live_operation_contract():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    live = app.openapi()
    operations = []
    for path, path_item in fixture["paths"].items():
        for method, expected in path_item.items():
            if method not in {"get", "post", "patch", "put", "delete"}:
                continue
            operation = live["paths"][path][method]
            operations.append(operation["operationId"])
            assert operation["operationId"] == expected["operationId"]

            for status in ("200", "201", "202"):
                if status not in expected["responses"]:
                    assert status not in operation["responses"]
                    continue
                expected_schema = expected["responses"][status]["content"]["application/json"]["schema"]
                actual_schema = operation["responses"][status]["content"]["application/json"]["schema"]
                assert actual_schema == expected_schema, f"{operation['operationId']} {status}"

            expected_body = expected.get("requestBody")
            actual_body = operation.get("requestBody")
            if expected_body is None:
                assert actual_body is None, operation["operationId"]
            else:
                assert actual_body.get("required") == expected_body.get("required")
                assert actual_body["content"]["application/json"]["schema"] == expected_body["content"]["application/json"]["schema"]

            expected_headers = {
                parameter["name"]
                for parameter in expected.get("parameters", [])
                if parameter.get("in") == "header" and parameter.get("required") is True
            }
            actual_headers = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "header" and parameter.get("required") is True
            }
            assert actual_headers == expected_headers, operation["operationId"]

            for status in ERROR_STATUSES:
                assert (
                    operation["responses"][status]["content"]["application/json"]["schema"]
                    == {"$ref": "#/components/schemas/AkbError"}
                ), f"{operation['operationId']} {status}"

    assert len(operations) == 31
    assert len(set(operations)) == len(operations)
    for schema_name, expected_schema in fixture["components"]["schemas"].items():
        assert live["components"]["schemas"].get(schema_name) == expected_schema
    assert fixture["components"].get("securitySchemes") == live["components"].get("securitySchemes")


def test_registry_openapi_advertises_manifest_shape_and_natural_key_replay_contract():
    schema = app.openapi()
    manifest = schema["components"]["schemas"]["ReleaseManifest"]
    assert manifest["required"] == ["steps"]
    assert manifest["properties"]["steps"]["type"] == "array"

    app_create = schema["paths"]["/api/v1/apps"]["post"]
    release_create = schema["paths"]["/api/v1/apps/{app_id}/releases"]["post"]
    assert not any(
        parameter["name"] == "Idempotency-Key"
        for parameter in app_create.get("parameters", [])
    )
    assert not any(
        parameter["name"] == "Idempotency-Key"
        for parameter in release_create.get("parameters", [])
    )
    assert "app_key" in app_create["description"]
    assert "replayed" in app_create["description"]
    assert "409" in app_create["description"]
    assert "app_id" in release_create["description"]
    assert "version" in release_create["description"]
    assert "replayed" in release_create["description"]
    assert "409" in release_create["description"]
