"""Table schema HTTP responses validated against the production OpenAPI contract."""

from __future__ import annotations

import uuid

import pytest
from jsonschema import Draft202012Validator

from .helpers import register_user

pytestmark = pytest.mark.live_services


@pytest.fixture(params=["create", "alter", "migration"])
async def schema_installation(request, api):
    owner = await register_user(api, label="schema-owner")
    headers = owner.headers
    contract = await api.get("/openapi.json", headers=headers)
    assert contract.status_code == 200
    assert contract.headers["content-type"].startswith("application/json")
    openapi = contract.json()
    vault = "schema-contract-test-" + uuid.uuid4().hex[:12]
    created = await api.post("/api/v1/vaults", params={"name": vault}, headers=headers)
    assert created.status_code == 200, created.text
    try:
        table = "items"
        base = f"/api/v1/tables/{vault}"
        values = ["todo", "done"]
        made = await api.post(base, headers=headers, json={"name": table, "columns": [
            {"name": "label", "type": "text", "required": True},
            {"name": "score", "type": "number", "enum": None},
            {"name": "state", "type": "enum", "enum": values, "default": "todo"},
        ]})
        assert made.status_code == 200, made.text
        row = {"label": "unchanged row", "score": 7, "state": "todo"}
        inserted = await api.post(
            f"{base}/{table}/rows", headers={**headers, "Prefer": "return=representation"}, json=[row],
        )
        assert inserted.status_code == 201, inserted.text
        # PostgreSQL NUMERIC is returned as a decimal string on this wire
        # surface. Pin the pre-change row, not the JSON input's number type.
        expected_row = {**row, "score": str(row["score"])}
        before = await api.get(f"{base}/{table}/rows", params={"select": "label,score,state"}, headers=headers)
        assert before.status_code == 200, before.text
        assert before.json()["items"] == [expected_row]
        if request.param == "alter":
            values = ["todo", "done", "archived"]
            changed = await api.patch(f"{base}/{table}", headers=headers, json={
                "add_columns": [{"name": "note", "type": "text"}],
                "alter_columns": [{"name": "state", "set_enum": values}],
            })
            assert changed.status_code == 200, changed.text
        elif request.param == "migration":
            migration_headers = {**headers, "Idempotency-Key": str(uuid.uuid4())}
            operations = [{"op": "add_column", "table": table, "column": {
                "name": "note", "type": "text", "enum": None,
            }}]
            changed = await api.post(f"{base}/migrations", headers=migration_headers, json=operations)
            assert changed.status_code == 200, changed.text
            applied = changed.json()
            assert applied["applied"] is True
            schema_before = await api.get(f"{base}/{table}/schema", headers=headers)
            assert schema_before.status_code == 200, schema_before.text

            replay = await api.post(f"{base}/migrations", headers=migration_headers, json=operations)
            assert replay.status_code == 200, replay.text
            replayed = replay.json()
            assert replayed["applied"] is False
            assert replayed["results"] == []
            for key in ("idempotency_key", "checksum", "applied_at", "operations"):
                assert replayed[key] == applied[key]
            schema_after = await api.get(f"{base}/{table}/schema", headers=headers)
            assert schema_after.status_code == 200, schema_after.text
            assert schema_after.json() == schema_before.json()
        yield headers, vault, table, openapi, values, expected_row, request.param
    finally:
        deleted = await api.delete("/api/v1/vaults/" + vault, headers=headers)
        assert deleted.status_code == 200, deleted.text

@pytest.mark.parametrize("scope", ["vault", "table"])
async def test_actual_schema_responses_validate_and_preserve_constraints(api, schema_installation, scope):
    headers, vault, table, contract, values, row, phase = schema_installation
    path = "/api/v1/tables/{vault}/schema" if scope == "vault" else "/api/v1/tables/{vault}/{table}/schema"
    response = await api.get(path.format(vault=vault, table=table), headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    response_schema = contract["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    # Validate the actual endpoint output against this installation's OpenAPI,
    # including recursive local refs, rather than a duplicated example schema.
    Draft202012Validator({**response_schema, "components": contract["components"]}).validate(payload)
    if scope == "vault":
        assert payload["total"] == 1
        payload = payload["tables"][0]
    names = [col["name"] for col in payload["columns"]]
    expected_names = {"label", "score", "state"} | ({"note"} if phase != "create" else set())
    assert len(names) == len(set(names))
    assert set(names) == expected_names
    columns = {col["name"]: col for col in payload["columns"]}
    assert "enum" not in columns["label"]
    assert "enum" not in columns["score"]
    assert columns["label"]["required"] is True
    assert columns["score"]["required"] is False
    assert columns["state"]["enum"] == values
    assert payload["drift"]["has_drift"] is False
    if phase != "create":
        assert "enum" not in columns["note"]

    rows = await api.get(f"/api/v1/tables/{vault}/{table}/rows", params={"select": "label,score,state"}, headers=headers)
    assert rows.status_code == 200, rows.text
    assert rows.json()["items"] == [row]
    rejected = await api.post(f"/api/v1/tables/{vault}/{table}/rows", headers=headers, json=[{
        "label": "invalid row", "state": "invalid",
    }])
    assert rejected.status_code == 400, rejected.text
