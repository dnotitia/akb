"""Opt-in schema contract checks against real HTTP/PostgreSQL responses.

Set AKB_TABLE_SCHEMA_TEST_URL and AKB_E2E_PAT, or AKB_E2E_USERNAME /
AKB_E2E_PASSWORD. AKB_E2E_CA_FILE supports a private CA without disabling TLS.
Only randomly named test Vaults are removed. No calls occur in unit runs.
"""

from __future__ import annotations

import os
import ssl
import uuid

import httpx
import pytest
from jsonschema import Draft202012Validator


@pytest.fixture(scope="module", params=["create", "alter", "migration"])
def schema_installation(request):
    origin = os.getenv("AKB_TABLE_SCHEMA_TEST_URL")
    if not origin:
        pytest.skip("set AKB_TABLE_SCHEMA_TEST_URL for the real PostgreSQL HTTP lane")
    ca_file = os.getenv("AKB_E2E_CA_FILE")
    verify = ssl.create_default_context(cafile=ca_file) if ca_file else True
    with httpx.Client(base_url=origin, verify=verify, timeout=30) as client:
        token = os.getenv("AKB_E2E_PAT")
        if not token:
            username, password = os.getenv("AKB_E2E_USERNAME"), os.getenv("AKB_E2E_PASSWORD")
            if not username or not password:
                pytest.fail("provide a fixture PAT or the runtime's fixture credentials")
            login = client.post("/api/v1/auth/login", json={"username": username, "password": password})
            assert login.status_code == 200
            token = login.json()["token"]
        client.headers["Authorization"] = "Bearer " + token
        contract = client.get("/openapi.json")
        assert contract.status_code == 200
        assert contract.headers.get("content-type", "").startswith("application/json"), (
            "use the API origin serving /openapi.json, not an SPA fallback"
        )
        openapi = contract.json()
        vault = "schema-contract-test-" + uuid.uuid4().hex[:12]
        created = client.post("/api/v1/vaults", params={"name": vault})
        assert created.status_code == 200, created.text
        try:
            table = "items"
            base = f"/api/v1/tables/{vault}"
            values = ["todo", "done"]
            made = client.post(base, json={"name": table, "columns": [
                {"name": "label", "type": "text", "required": True},
                {"name": "score", "type": "number", "enum": None},
                {"name": "state", "type": "enum", "enum": values, "default": "todo"},
            ]})
            assert made.status_code == 200, made.text
            row = {"label": "unchanged row", "score": 7, "state": "todo"}
            inserted = client.post(
                f"{base}/{table}/rows", headers={"Prefer": "return=representation"}, json=[row],
            )
            assert inserted.status_code == 201, inserted.text
            # PostgreSQL NUMERIC is returned as a decimal string on this wire
            # surface. Pin the pre-change row, not the JSON input's number type.
            expected_row = {**row, "score": str(row["score"])}
            before = client.get(f"{base}/{table}/rows", params={"select": "label,score,state"})
            assert before.status_code == 200, before.text
            assert before.json()["items"] == [expected_row]
            if request.param == "alter":
                values = ["todo", "done", "archived"]
                changed = client.patch(f"{base}/{table}", json={
                    "add_columns": [{"name": "note", "type": "text"}],
                    "alter_columns": [{"name": "state", "set_enum": values}],
                })
                assert changed.status_code == 200, changed.text
            elif request.param == "migration":
                changed = client.post(f"{base}/migrations", headers={"Idempotency-Key": str(uuid.uuid4())}, json=[
                    {"op": "add_column", "table": table, "column": {
                        "name": "note", "type": "text", "enum": None,
                    }},
                ])
                assert changed.status_code == 200, changed.text
            yield client, vault, table, openapi, values, expected_row, request.param
        finally:
            deleted = client.delete("/api/v1/vaults/" + vault)
            assert deleted.status_code == 200, deleted.text


@pytest.mark.parametrize("scope", ["vault", "table"])
def test_actual_schema_responses_validate_and_preserve_constraints(schema_installation, scope):
    client, vault, table, contract, values, row, phase = schema_installation
    path = "/api/v1/tables/{vault}/schema" if scope == "vault" else "/api/v1/tables/{vault}/{table}/schema"
    response = client.get(path.format(vault=vault, table=table))
    assert response.status_code == 200, response.text
    payload = response.json()
    response_schema = contract["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    # Validate the actual endpoint output against this installation's OpenAPI,
    # including recursive local refs, rather than a duplicated example schema.
    Draft202012Validator({**response_schema, "components": contract["components"]}).validate(payload)
    if scope == "vault":
        assert payload["total"] == 1
        payload = payload["tables"][0]
    columns = {col["name"]: col for col in payload["columns"]}
    assert "enum" not in columns["label"]
    assert "enum" not in columns["score"]
    assert columns["label"]["required"] is True
    assert columns["score"]["required"] is False
    assert columns["state"]["enum"] == values
    assert payload["drift"]["has_drift"] is False
    if phase != "create":
        assert "enum" not in columns["note"]

    rows = client.get(f"/api/v1/tables/{vault}/{table}/rows", params={"select": "label,score,state"})
    assert rows.status_code == 200, rows.text
    assert rows.json()["items"] == [row]
    rejected = client.post(f"/api/v1/tables/{vault}/{table}/rows", json=[{
        "label": "invalid row", "state": "invalid",
    }])
    assert rejected.status_code == 400, rejected.text
