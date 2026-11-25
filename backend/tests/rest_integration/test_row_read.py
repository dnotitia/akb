import httpx
import pytest

from tests.rest_integration.helpers import register_user

pytestmark = pytest.mark.live_services

_VAULT = "row-read-vault"
_TABLES = f"/api/v1/tables/{_VAULT}"
_ROWS = f"{_TABLES}/incidents/rows"
_QUERY = f"{_TABLES}/incidents/query"


@pytest.fixture
async def reader_headers(api: httpx.AsyncClient) -> dict[str, str]:
    owner = await register_user(api, label="row-read")
    headers = owner.headers
    response = await api.post(
        "/api/v1/vaults", headers=headers, params={"name": _VAULT, "description": "row-read-e2e"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["vault_id"]

    response = await api.post(_TABLES, headers=headers, json={
        "name": "incidents",
        "columns": [
            {"name": "title", "type": "text", "required": True},
            {"name": "severity", "type": "text"},
            {"name": "score", "type": "number"},
            {"name": "metadata", "type": "json"},
        ],
    })
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "incidents"

    response = await api.post(f"{_TABLES}/sql", headers=headers, json={"sql": """
        INSERT INTO incidents (title, severity, score, metadata, created_at) VALUES
        ('DB pressure', 'critical', 0.90, '{"tier":"gold","stats":{"count":9}}', '2026-01-03T00:00:00Z'),
        ('Minor typo', 'low', 0.10, '{"tier":"bronze","stats":{"count":1}}', '2026-01-02T00:00:00Z'),
        ('API outage', 'high', 0.95, '{"tier":"gold","stats":{"count":7}}', '2026-01-01T00:00:00Z')
    """})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "table_sql"
    return headers


async def test_get_rows_selects_filters_orders_and_counts(api, reader_headers):
    headers = {**reader_headers, "Prefer": "count=exact"}
    response = await api.get(_ROWS, headers=headers, params={
        "select": "title,severity,metadata->>tier",
        "severity": "in.(high,critical)",
        "order": "created_at.asc",
        "limit": 2,
    })
    assert response.status_code == 200, response.text
    assert response.headers["Content-Range"] == "0-1/2"
    body = response.json()
    assert body["kind"] == "table_query"
    assert "vaults" not in body
    assert body["columns"] == ["title", "severity", "metadata->>tier"]
    assert body["total"] == 2
    assert [row["severity"] for row in body["items"]] == ["high", "critical"]
    assert [row["metadata->>tier"] for row in body["items"]] == ["gold", "gold"]

    response = await api.get(_ROWS, headers=headers, params={
        "severity": "in.(high,critical)", "offset": 100, "limit": 2,
    })
    assert response.status_code == 200, response.text
    assert response.headers["Content-Range"] == "*/2"
    body = response.json()
    assert body["total"] == 2
    assert body["items"] == []


@pytest.mark.parametrize(
    ("column", "predicate"),
    [
        ("metadata#>>{stats,count}::int", "gt.5"),
        ("metadata", 'cs.{"tier":"gold"}'),
    ],
    ids=["path-cast", "object-containment"],
)
async def test_get_rows_filters_json(api, reader_headers, column, predicate):
    response = await api.get(
        _ROWS, headers=reader_headers, params={"select": "title", column: predicate, "order": "title.asc"},
    )
    assert response.status_code == 200, response.text
    assert [row["title"] for row in response.json()["items"]] == ["API outage", "DB pressure"]


async def test_query_ast_selects_filters_orders_and_counts(api, reader_headers):
    response = await api.post(_QUERY, headers=reader_headers, json={
        "select": ["title", "severity", "metadata->>tier"],
        "and": [
            {"col": "severity", "op": "in", "val": ["high", "critical"]},
            {"jsonb": {"col": "metadata", "path": ["tier"], "cast": None}, "op": "eq", "val": "gold"},
        ],
        "order": [{"col": "created_at", "dir": "asc"}],
        "limit": 2,
        "count": "exact",
    })
    assert response.status_code == 200, response.text
    assert response.headers["Content-Range"] == "0-1/2"
    body = response.json()
    assert body["kind"] == "table_query"
    assert "vaults" not in body
    assert body["columns"] == ["title", "severity", "metadata->>tier"]
    assert body["total"] == 2
    assert [row["title"] for row in body["items"]] == ["API outage", "DB pressure"]
    assert [row["metadata->>tier"] for row in body["items"]] == ["gold", "gold"]


async def test_get_rows_rejects_unknown_column(api, reader_headers):
    response = await api.get(_ROWS, headers=reader_headers, params={"sevverity": "eq.high"})
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "undefined_column"


async def test_query_ast_rejects_unknown_column(api, reader_headers):
    response = await api.post(
        _QUERY, headers=reader_headers, json={"filter": {"col": "sevverity", "op": "eq", "val": "high"}},
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "undefined_column"
