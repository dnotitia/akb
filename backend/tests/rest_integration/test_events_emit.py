import asyncpg
import httpx
import pytest

from tests.rest_integration.helpers import register_user

pytestmark = pytest.mark.live_services


async def test_table_writes_emit_transactional_events(api: httpx.AsyncClient, pg_pool: asyncpg.Pool):
    owner = await register_user(api, label="events")
    vault = f"events-{owner.id.hex[:12]}"
    table = "orders"
    table_uri = f"akb://{vault}/table/{table}"

    response = await api.post("/api/v1/vaults", headers=owner.headers, params={"name": vault})
    vault_id = await pg_pool.fetchval("SELECT id FROM vaults WHERE name = $1", vault)
    assert str(vault_id) == response.raise_for_status().json()["vault_id"]

    response = await api.post(
        f"/api/v1/tables/{vault}",
        headers=owner.headers,
        json={"name": table, "description": "d", "columns": [{"name": "sku", "type": "text", "required": True}]},
    )
    assert response.raise_for_status().json()["uri"] == table_uri
    created = await pg_pool.fetch(
        """
        SELECT resource_uri, actor_id, payload->>'table_name' AS table_name
        FROM events WHERE vault_id = $1 AND kind = 'table.create'
        """,
        vault_id,
    )
    assert [dict(event) for event in created] == [
        {"resource_uri": table_uri, "actor_id": owner.username, "table_name": table}
    ]

    protocol_version = "2025-03-26"
    mcp_headers = {**owner.headers, "Accept": "application/json, text/event-stream"}
    response = await api.post(
        "/mcp/",
        headers=mcp_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "events-integration", "version": "1"},
            },
        },
    )
    assert response.raise_for_status().json()["result"]["protocolVersion"] == protocol_version
    assert "mcp-session-id" not in response.headers
    mcp_headers["MCP-Protocol-Version"] = protocol_version

    count_query = "SELECT COUNT(*) FROM events WHERE vault_id = $1 AND kind = 'table.rows_changed'"
    baseline = await pg_pool.fetchval(count_query, vault_id)
    statements = [
        ("INSERT INTO orders (sku) VALUES ('one')", 1, False),
        ("INSERT INTO orders (sku) VALUES ('bulk-a'), ('bulk-b')", 2, False),
        ("UPDATE orders SET sku = 'updated' WHERE sku LIKE 'bulk-%'", 3, False),
        ("UPDATE orders SET sku = 'missing' WHERE sku = 'does-not-exist'", 3, False),
        ("INSERT INTO orders (sku) VALUES (NULL)", 3, True),
        ("DELETE FROM orders WHERE sku = 'updated'", 4, False),
    ]
    for call_id, (sql, expected_events, expected_error) in enumerate(statements, start=2):
        response = await api.post(
            "/mcp/",
            headers=mcp_headers,
            json={
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {"name": "akb_sql", "arguments": {"vault": vault, "sql": sql}},
            },
        )
        result = response.raise_for_status().json()["result"]
        assert result.get("isError", False) is expected_error, result
        assert await pg_pool.fetchval(count_query, vault_id) == baseline + expected_events, sql

    first = await pg_pool.fetchrow(
        """
        SELECT resource_uri, actor_id, payload->>'operation' AS operation
        FROM events WHERE vault_id = $1 AND kind = 'table.rows_changed' ORDER BY id LIMIT 1
        """,
        vault_id,
    )
    assert dict(first) == {"resource_uri": table_uri, "actor_id": owner.username, "operation": "insert"}

    response = await api.delete(f"/api/v1/tables/{vault}/{table}", headers=owner.headers)
    response.raise_for_status()
    dropped = await pg_pool.fetch(
        "SELECT resource_uri FROM events WHERE vault_id = $1 AND kind = 'table.drop'",
        vault_id,
    )
    assert [event["resource_uri"] for event in dropped] == [table_uri]
