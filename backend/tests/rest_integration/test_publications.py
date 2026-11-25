"""REST contracts for live document and table-query publications."""

import time
from collections.abc import AsyncIterator
from secrets import token_urlsafe
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from tests.rest_integration.helpers import register_user

pytestmark = pytest.mark.live_services

DOCUMENT_TITLE = "Pub Doc"
DOCUMENT_BODY = "# Top\n\n## Alpha\nAlpha content\n\n## Beta\nBeta content\n\n## Gamma\nGamma content"


@pytest.fixture
async def vault(api: httpx.AsyncClient) -> AsyncIterator[tuple[str, httpx.Headers]]:
    owner = await register_user(api, label="publication")
    headers = httpx.Headers({"Authorization": f"Bearer {owner.jwt}"})
    name = owner.username
    created = await api.post("/api/v1/vaults", params={"name": name}, headers=headers)
    assert created.status_code == 200, created.text
    assert created.json()["name"] == name
    try:
        yield name, headers
    finally:
        deleted = await api.delete(f"/api/v1/vaults/{name}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"] is True


async def _create_document(
    api,
    vault,
    *,
    collection="docs",
    slug="published",
    title=DOCUMENT_TITLE,
    content=DOCUMENT_BODY,
):
    name, headers = vault
    response = await api.post(
        "/api/v1/documents",
        headers=headers,
        json={
            "vault": name,
            "collection": collection,
            "slug": slug,
            "title": title,
            "content": content,
            "type": "note",
            "tags": ["pub", "test"],
        },
    )
    assert response.status_code == 200, response.text
    document = response.json()
    assert document["uri"] and document["path"]
    return document


@pytest.fixture
async def document(api, vault):
    return await _create_document(api, vault)


async def _create_publication(api, vault, resource_type, **options):
    name, headers = vault
    return await api.post(
        f"/api/v1/publications/{name}/create",
        headers=headers,
        json={"resource_type": resource_type, **options},
    )


async def _publish(api, vault, resource_type, **options):
    response = await _create_publication(api, vault, resource_type, **options)
    assert response.status_code == 200, response.text
    publication = response.json()
    assert publication["slug"]
    return publication


async def _publication_list(api, vault, **params):
    name, headers = vault
    response = await api.get(f"/api/v1/publications/{name}", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()["publications"]


async def _public_document(api, vault, document, **options):
    publication = await _publish(api, vault, "document", uri=document["uri"], **options)
    return f"/api/v1/public/{publication['slug']}"


@pytest.fixture
async def products(api, vault):
    name, headers = vault
    table = await api.post(
        f"/api/v1/tables/{name}",
        headers=headers,
        json={
            "name": "products",
            "columns": [
                {"name": "name", "type": "text", "required": True},
                {"name": "category", "type": "text"},
                {"name": "price", "type": "number"},
            ],
        },
    )
    assert table.status_code == 200, table.text
    assert table.json()["uri"]
    seed = await api.post(
        f"/api/v1/tables/{name}/sql",
        headers=headers,
        json={
            "sql": (
                "INSERT INTO products (name, category, price) VALUES "
                "('Apple', 'food', 1), ('Bagel', 'food', 2), "
                "('Chair', 'furniture', 50), ('Desk', 'furniture', 200)"
            )
        },
    )
    assert seed.status_code == 200, seed.text
    assert seed.json()["affected_rows"] == 4


async def test_document_publication_shape_and_capabilities(api, vault, document):
    name, headers = vault
    publication = await _publish(api, vault, "document", uri=document["uri"])
    assert publication["vault"] == name
    assert not {
        "id",
        "publication_id",
        "public_url",
        "public_url_full",
        "public_base",
        "snapshot_s3_key",
        "password_hash",
        "vault_id",
    }.intersection(publication)
    share_url = urlsplit(publication["share_url"])
    assert share_url.scheme in {"http", "https"} and share_url.netloc

    path = f"/api/v1/public/{publication['slug']}"
    response = await api.get(path)
    assert response.status_code == 200, response.text
    resolved = response.json()
    assert resolved["resource_type"] == "document"
    assert resolved["title"] == DOCUMENT_TITLE
    assert resolved["content_unavailable"] is False
    assert resolved["tags"] == ["pub", "test"]
    assert not {"created_by", "status", "created_at"}.intersection(resolved)

    anonymous = await api.get(f"{path}/capabilities")
    assert anonymous.status_code == 200, anonymous.text
    assert anonymous.json()["can_edit"] is False
    owner = await api.get(f"{path}/capabilities", headers=headers)
    assert owner.status_code == 200, owner.text
    assert owner.json()["can_edit"] is True


async def test_section_filter_limits_content_and_summary_to_granted_section(api, vault, document):
    alpha = await _public_document(api, vault, document, section_filter="Alpha")
    response = await api.get(alpha)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["section_filter"] == "Alpha"
    assert "## Alpha" in body["content"]
    assert "## Beta" not in body["content"]

    gamma = await _public_document(api, vault, document, section_filter="Gamma")
    response = await api.get(gamma)
    assert response.status_code == 200, response.text
    assert response.json()["summary"] == "Gamma content"

    missing = await _public_document(api, vault, document, section_filter="Nonexistent")
    response = await api.get(missing)
    assert response.status_code == 200, response.text
    assert response.json()["section_not_found"] is True
    assert response.json()["content"] == ""


async def test_publication_expiration_options(api, vault, document):
    publication = await _publish(api, vault, "document", uri=document["uri"], expires_in="1h")
    assert publication["expires_at"]
    assert (await api.get(f"/api/v1/public/{publication['slug']}")).status_code == 200

    rejected = await _create_publication(
        api, vault, "document", uri=document["uri"], expires_in="banana",
    )
    assert rejected.status_code == 400, rejected.text
    assert "invalid expires_in" in rejected.json()["detail"].lower()

    never = await _publish(api, vault, "document", uri=document["uri"], expires_in="never")
    assert never["expires_at"] is None


async def test_password_access_token_binding_and_revocation(api, vault, document):
    password = token_urlsafe(24)
    wrong_password = password + "-wrong"
    publication = await _publish(api, vault, "document", uri=document["uri"], password=password)
    assert publication["password_protected"] is True
    path = f"/api/v1/public/{publication['slug']}"

    assert (await api.get(path)).status_code == 401
    assert (await api.get(path, headers={"x-publication-password": wrong_password})).status_code == 401
    assert (await api.get(path, headers={"x-publication-password": password})).status_code == 200
    assert (await api.get(path, params={"password": password})).status_code == 401

    denied = await api.post(f"{path}/auth", json={"password": wrong_password})
    assert denied.status_code == 401, denied.text
    assert "token" not in denied.json()
    issued = await api.post(f"{path}/auth", json={"password": password})
    assert issued.status_code == 200, issued.text
    token = issued.json()["token"]
    assert token
    assert (await api.get(path, params={"token": token})).status_code == 200

    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert (await api.get(path, params={"token": tampered})).status_code == 401
    other = await _public_document(api, vault, document, password=password + "-other")
    assert (await api.get(other, params={"token": token})).status_code == 401

    name, headers = vault
    deleted = await api.delete(f"/api/v1/publications/{name}/{publication['slug']}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert (await api.get(path, params={"token": token})).status_code == 404


async def test_password_budget_covers_rotating_clients_and_owner_can_still_authenticate(api, vault, document):
    password = token_urlsafe(24)
    wrong_password = password + "-wrong"
    path = await _public_document(api, vault, document, password=password)
    statuses = set()
    for client in range(1, 36):
        response = await api.post(
            f"{path}/auth",
            json={"password": wrong_password},
            headers={"X-Forwarded-For": f"192.0.2.{client}"},
        )
        statuses.add(response.status_code)
    assert statuses == {401, 429}

    client = {"X-Forwarded-For": "198.51.100.1"}
    correct = {"password": password}
    assert (await api.post(f"{path}/auth", json=correct, headers=client)).status_code == 429
    _, owner = vault
    assert (await api.post(f"{path}/auth", json=correct, headers={**client, **owner})).status_code == 200


async def test_document_views_are_capped_across_page_and_download(api, vault, document):
    path = await _public_document(api, vault, document, max_views=2)
    assert (await api.get(path)).status_code == 200
    assert (await api.get(path)).status_code == 200
    assert (await api.get(path)).status_code == 410
    assert (await api.get(f"{path}/download")).status_code == 410

    exhausted = await _public_document(api, vault, document, max_views=0)
    assert (await api.get(exhausted)).status_code == 410


async def test_document_view_grant_reuses_last_view_and_meta_is_free(api, vault, document, monkeypatch):
    from app.api.routes import public

    now = int(time.time())
    monkeypatch.setattr(public, "time", SimpleNamespace(time=lambda: now))
    path = await _public_document(api, vault, document, max_views=1)
    for _ in range(2):
        assert (await api.get(f"{path}/meta")).status_code == 200

    response = await api.get(path)
    assert response.status_code == 200, response.text
    grant = response.json()["view_grant"]
    assert grant
    assert (await api.get(f"{path}/download", params={"grant": grant})).status_code == 200
    assert (await api.get(f"{path}/download")).status_code == 410

    now += 2
    reopened = await api.get(path, params={"grant": grant})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["view_grant"] == grant
    assert (await api.get(path)).status_code == 410


async def test_publication_list_filter_and_delete(api, vault, document):
    name, headers = vault
    assert await _publication_list(api, vault) == []

    documents = [
        (await _publish(api, vault, "document", uri=document["uri"]))["slug"]
        for _ in range(6)
    ]
    query = await _publish(api, vault, "table_query", query_sql="SELECT 1 AS value")

    assert {pub["slug"] for pub in await _publication_list(api, vault)} == {*documents, query["slug"]}
    assert {pub["slug"] for pub in await _publication_list(api, vault, resource_type="document")} == set(documents)
    assert await _publication_list(api, vault, resource_type="banana") == []

    deleted = await api.delete(f"/api/v1/publications/{name}/{documents[0]}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert (await api.get(f"/api/v1/public/{documents[0]}")).status_code == 404
    assert (await api.delete(f"/api/v1/publications/{name}/bogus", headers=headers)).status_code == 404


async def test_table_query_url_parameters_and_output_formats(api, vault, products):
    publication = await _publish(
        api,
        vault,
        "table_query",
        query_sql=(
            "SELECT name, category, price FROM products "
            "WHERE category = :cat AND price >= :min ORDER BY price DESC"
        ),
        query_params={"cat": {"type": "text", "default": "food"}, "min": {"type": "number", "default": 0}},
        title="Products",
    )
    path = f"/api/v1/public/{publication['slug']}"
    defaults = await api.get(path)
    assert defaults.status_code == 200, defaults.text
    assert defaults.json()["total"] == 2
    assert [row["name"] for row in defaults.json()["rows"]] == ["Bagel", "Apple"]

    override = await api.get(path, params={"cat": "furniture", "min": 100})
    assert override.status_code == 200, override.text
    assert override.json()["total"] == 1
    assert override.json()["rows"][0]["name"] == "Desk"

    csv = await api.get(path, params={"format": "csv"})
    assert csv.status_code == 200, csv.text
    assert csv.text.splitlines() == ["name,category,price", "Bagel,food,2", "Apple,food,1"]
    html = await api.get(path, params={"format": "html"})
    assert html.status_code == 200, html.text
    assert "<table" in html.text


async def test_view_grant_parameter_is_reserved_across_table_query_formats(api, vault, products):
    publication = await _publish(
        api,
        vault,
        "table_query",
        query_sql="SELECT name FROM products WHERE category = :grant ORDER BY name",
        query_params={"grant": {"type": "text", "default": "food"}},
    )
    path = f"/api/v1/public/{publication['slug']}"
    page = await api.get(path, params={"grant": "furniture"})
    assert page.status_code == 200, page.text
    assert page.json()["rows"] == [{"name": "Apple"}, {"name": "Bagel"}]
    for endpoint, extra_params in ((path, {"format": "csv"}), (f"{path}/download", {})):
        csv = await api.get(endpoint, params={"grant": "furniture", **extra_params})
        assert csv.status_code == 200, csv.text
        assert csv.text.splitlines() == ["name", "Apple", "Bagel"]


@pytest.mark.parametrize(
    ("options", "error"),
    [
        pytest.param({"query_sql": "DELETE FROM products"}, "only select", id="write"),
        pytest.param({"query_sql": "SELECT 1; SELECT 2"}, "multi-statement", id="multiple-statements"),
        pytest.param({}, "query_sql", id="missing-query"),
        pytest.param({"query_sql": "   "}, "only select", id="blank-query"),
        pytest.param(
            {"query_sql": "SELECT 1", "query_params": {"unused": {"type": "text"}}},
            "unused",
            id="unused-parameter",
        ),
        pytest.param({"query_sql": "SELECT :missing"}, "undeclared", id="undeclared-parameter"),
    ],
)
async def test_invalid_table_query_is_rejected_at_creation(api, vault, options, error):
    response = await _create_publication(api, vault, "table_query", **options)
    assert response.status_code == 400, response.text
    assert error in response.json()["detail"].lower()


async def test_table_query_with_sql_in_comment_leaves_table_intact(api, vault, products):
    # The comment is inert SQL. The validator may decline it at creation.
    await _create_publication(api, vault, "table_query", query_sql="SELECT * FROM products -- ; DROP TABLE products")
    name, headers = vault
    counted = await api.post(
        f"/api/v1/tables/{name}/sql",
        headers=headers,
        json={"sql": "SELECT COUNT(*) FROM products"},
    )
    assert counted.status_code == 200, counted.text
    assert counted.json()["items"][0]["count"] == 4


async def test_nonfinite_table_values_serialize_to_json_null(api, vault):
    publication = await _publish(
        api,
        vault,
        "table_query",
        query_sql="SELECT float8 'NaN' AS nanv, float8 'Infinity' AS infv, float8 '2.5' AS okv",
    )
    response = await api.get(f"/api/v1/public/{publication['slug']}")
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == [{"nanv": None, "infv": None, "okv": 2.5}]


async def test_document_and_table_embed_and_oembed(api, vault, document):
    path = await _public_document(api, vault, document)
    embedded = await api.get(f"{path}/embed")
    assert embedded.status_code == 200, embedded.text
    assert embedded.json()["embed"] is True
    assert embedded.json()["title"] == DOCUMENT_TITLE

    query = await _publish(api, vault, "table_query", query_sql="SELECT 1 AS value")
    assert (await api.get(f"/api/v1/public/{query['slug']}/embed")).status_code == 200
    oembed = await api.get("/api/v1/oembed", params={"url": f"/p/{query['slug']}"})
    assert oembed.status_code == 200, oembed.text
    assert oembed.json()["type"] == "rich"

    protected = await _publish(api, vault, "document", uri=document["uri"], password="embed-secret")
    protected_oembed = await api.get("/api/v1/oembed", params={"url": f"/p/{protected['slug']}"})
    assert protected_oembed.status_code == 200, protected_oembed.text
    assert protected_oembed.json()["title"] == "Protected AKB publication"
    assert (await api.get(f"/api/v1/public/{protected['slug']}/embed")).status_code == 401

    disabled = await _public_document(api, vault, document, allow_embed=False)
    assert (await api.get(f"{disabled}/embed")).status_code == 403
    assert (await api.get("/api/v1/oembed", params={"url": "https://example.com/wrong"})).status_code == 400


async def test_unavailable_or_unsupported_publication_requests(api, vault, document):
    name, headers = vault
    assert (await api.get("/api/v1/public/totallybogusslug123")).status_code == 404
    assert (await api.get("/api/v1/public/%21%40%23")).status_code in {400, 404}
    assert (await api.get(f"/api/v1/publications/{name}")).status_code == 401

    unauthorized = await api.post(
        f"/api/v1/publications/{name}/create", json={"resource_type": "document", "uri": document["uri"]},
    )
    assert unauthorized.status_code == 401, unauthorized.text

    missing_document = await _create_publication(
        api, vault, "document", uri=f"akb://{name}/doc/missing/nonexistent.md",
    )
    assert missing_document.status_code == 400, missing_document.text
    assert "not found" in missing_document.json()["detail"].lower()

    invalid_file = await _create_publication(api, vault, "file", uri=f"akb://{name}/file/not-a-uuid")
    assert invalid_file.status_code == 400, invalid_file.text
    assert invalid_file.json()["detail"]

    invalid_mode = await _create_publication(api, vault, "document", uri=document["uri"], mode="snapshot")
    assert invalid_mode.status_code == 422, invalid_mode.text

    publication = await _publish(api, vault, "document", uri=document["uri"])
    for slug, status in ((publication["slug"], 400), ("totally-bogus-slug", 404)):
        snap = await api.post(f"/api/v1/publications/{name}/{slug}/snapshot", headers=headers)
        assert snap.status_code == status, snap.text


async def test_document_delete_removes_publication_and_owner_listing(api, vault, document):
    name, headers = vault
    publication = await _publish(api, vault, "document", uri=document["uri"])
    slug = publication["slug"]
    assert (await api.get(f"/api/v1/public/{slug}")).status_code == 200

    removed = await api.delete(f"/api/v1/documents/{name}/{document['path']}", headers=headers)
    assert removed.status_code == 200, removed.text
    assert (await api.get(f"/api/v1/public/{slug}")).status_code == 404
    assert slug not in {pub["slug"] for pub in await _publication_list(api, vault)}


async def test_collection_delete_prevents_old_publication_from_resolving_recreated_document(api, vault):
    name, headers = vault
    original = await _create_document(api, vault, collection="reports", slug="q3", content="FIRST-BODY")
    path = await _public_document(api, vault, original)
    before = await api.get(path)
    assert before.status_code == 200, before.text
    assert "FIRST-BODY" in before.json()["content"]

    removed = await api.delete(
        f"/api/v1/collections/{name}/reports", headers=headers, params={"recursive": "true"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["ok"] is True
    assert (await api.get(path)).status_code == 404

    recreated = await _create_document(api, vault, collection="reports", slug="q3", content="SECOND-BODY")
    assert recreated["path"] == original["path"]
    after = await api.get(path)
    assert after.status_code == 404, after.text
    assert "SECOND-BODY" not in after.text


async def test_local_session_jwt_is_rejected_by_mcp_transport(api):
    owner = await register_user(api, label="publication-mcp")
    response = await api.post(
        "/mcp/",
        headers={"Authorization": f"Bearer {owner.jwt}", "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "local-session-boundary", "version": "0.1"},
            },
        },
    )
    assert response.status_code == 401, response.text
