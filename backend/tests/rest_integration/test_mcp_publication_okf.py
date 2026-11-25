"""Publication and OKF contracts across REST and the MCP SDK."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import pytest
from mcp import Client

from .file_fixtures import object_store as object_store
from .file_fixtures import upload_file
from .helpers import Actor
from .mcp_client import (
    SecondaryMcpSession,
    call_json,
    create_vault,
    mcp_actor as mcp_actor,
    mcp_client as mcp_client,
    secondary_mcp_client as secondary_mcp_client,
)

pytestmark = pytest.mark.live_services


async def _public_response(api: httpx.AsyncClient, slug: str) -> httpx.Response:
    return await api.get(f"/api/v1/public/{quote(slug, safe='')}")


def _assert_canonical_publication(publication: dict[str, Any]) -> None:
    slug = publication.get("slug")
    share_url = publication.get("share_url")
    assert isinstance(slug, str) and slug
    assert isinstance(share_url, str) and share_url
    parsed = urlsplit(share_url)
    assert parsed.scheme in {"http", "https"}
    assert parsed.netloc
    assert parsed.path.rstrip("/").endswith(f"/p/{slug}")
    assert not {
        "publication_id",
        "public_url",
        "public_url_full",
        "public_base",
    }.intersection(publication)


def _okf_type(markdown: str) -> str:
    match = re.match(r"^---\n(.*?)\n---\n", markdown, re.DOTALL)
    assert match is not None
    type_match = re.search(r"^type:\s*(\S+)", match.group(1), re.MULTILINE)
    assert type_match is not None
    return type_match.group(1)


@pytest.mark.usefixtures("object_store")
async def test_publication_lifecycle_through_mcp_and_rest(
    api: httpx.AsyncClient,
    mcp_actor: Actor,
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
) -> None:
    """Cover publication CRUD, filters, compatibility, ACLs, and public reads."""

    vault = await create_vault(mcp_client, "publication-detail")
    document = await call_json(
        mcp_client,
        "akb_put",
        {
            "vault": vault,
            "collection": "docs",
            "slug": "live-document",
            "title": "Live Document",
            "content": "# Live Document\n\nPUBLICATION_DOCUMENT_MARKER",
            "type": "note",
        },
    )
    document_uri = document["uri"]

    document_publication = await call_json(
        mcp_client,
        "akb_publish",
        {"uri": document_uri},
    )
    _assert_canonical_publication(document_publication)
    document_slug = document_publication["slug"]
    public_document = await _public_response(api, document_slug)
    assert public_document.status_code == 200
    public_document_body = public_document.json()
    assert public_document_body.get("title") == "Live Document"
    assert "PUBLICATION_DOCUMENT_MARKER" in public_document_body.get("content", "")

    uri_document = await call_json(
        mcp_client,
        "akb_put",
        {
            "vault": vault,
            "collection": "docs",
            "slug": "uri-unpublish-document",
            "title": "URI Unpublish Document",
            "content": "# URI Unpublish\n\nURI_UNPUBLISH_MARKER",
        },
    )
    uri_publication = await call_json(
        mcp_client,
        "akb_publish",
        {"uri": uri_document["uri"]},
    )
    _assert_canonical_publication(uri_publication)
    uri_slug = uri_publication["slug"]
    assert (await _public_response(api, uri_slug)).status_code == 200

    file_uri = await upload_file(
        api,
        mcp_actor,
        vault,
        filename="publication.json",
        collection="assets",
        content=b'{"publication": true}',
        mime_type="application/json",
    )
    file_publication = await call_json(
        mcp_client,
        "akb_publish",
        {"uri": file_uri, "resource_type": "file"},
    )
    _assert_canonical_publication(file_publication)
    file_slug = file_publication["slug"]
    public_file = await _public_response(api, file_slug)
    assert public_file.status_code == 200
    assert public_file.json().get("name") == "publication.json"

    table = await call_json(
        mcp_client,
        "akb_create_table",
        {
            "vault": vault,
            "name": "publication_products",
            "columns": [
                {"name": "name", "type": "text"},
                {"name": "price", "type": "number"},
            ],
        },
    )
    assert table.get("uri")
    seeded = await call_json(
        mcp_client,
        "akb_sql",
        {
            "vault": vault,
            "sql": ("INSERT INTO publication_products (name, price) VALUES ('Apple', 1), ('Bagel', 2)"),
        },
    )
    assert "INSERT" in str(seeded.get("result"))
    query_publication = await call_json(
        mcp_client,
        "akb_publish",
        {
            "vault": vault,
            "resource_type": "table_query",
            "query_sql": "SELECT name, price FROM publication_products ORDER BY name",
        },
    )
    _assert_canonical_publication(query_publication)
    query_slug = query_publication["slug"]
    public_query = await _public_response(api, query_slug)
    assert public_query.status_code == 200
    assert [row["name"] for row in public_query.json()["rows"]] == ["Apple", "Bagel"]

    all_publications = await call_json(
        mcp_client,
        "akb_publications",
        {"vault": vault},
    )
    listed = all_publications.get("publications")
    assert isinstance(listed, list)
    assert all_publications.get("total") == len(listed)
    assert {item.get("resource_type") for item in listed} >= {
        "document",
        "file",
        "table_query",
    }

    document_publications = await call_json(
        mcp_client,
        "akb_publications",
        {"vault": vault, "resource_type": "document"},
    )
    assert document_publications.get("total") == len(document_publications["publications"])
    assert all(item.get("resource_type") == "document" for item in document_publications["publications"])

    file_publications = await call_json(
        mcp_client,
        "akb_publications",
        {"vault": vault, "resource_type": "file"},
    )
    assert file_publications.get("total") == 1
    assert file_publications["publications"][0]["slug"] == file_slug

    query_publications = await call_json(
        mcp_client,
        "akb_publications",
        {"vault": vault, "resource_type": "table_query"},
    )
    assert query_publications.get("total") == 1
    assert query_publications["publications"][0]["slug"] == query_slug

    snapshot = await call_json(
        mcp_client,
        "akb_publication_snapshot",
        {"slug": query_slug},
    )
    assert snapshot.get("mode") == "snapshot"
    assert isinstance(snapshot.get("snapshot_at"), str) and snapshot["snapshot_at"]
    public_snapshot = await _public_response(api, query_slug)
    assert public_snapshot.status_code == 200
    assert [row["name"] for row in public_snapshot.json()["rows"]] == ["Apple", "Bagel"]

    missing_uri = await call_json(
        mcp_client,
        "akb_publish",
        {"resource_type": "document"},
        expect_error=True,
    )
    assert missing_uri.get("code") == "invalid_argument"
    legacy_mode = await call_json(
        mcp_client,
        "akb_publish",
        {"uri": document_uri, "mode": "snapshot"},
        expect_error=True,
    )
    assert "unknown argument" in str(legacy_mode.get("error", "")).lower()

    no_access_list = await call_json(
        secondary_mcp_client.client,
        "akb_publications",
        {"vault": vault},
        expect_error=True,
    )
    assert "error" in no_access_list
    no_access_publish = await call_json(
        secondary_mcp_client.client,
        "akb_publish",
        {"uri": document_uri},
        expect_error=True,
    )
    assert "error" in no_access_publish
    no_access_snapshot = await call_json(
        secondary_mcp_client.client,
        "akb_publication_snapshot",
        {"slug": query_slug},
        expect_error=True,
    )
    assert "error" in no_access_snapshot
    no_access_unpublish = await call_json(
        secondary_mcp_client.client,
        "akb_unpublish",
        {"slug": document_slug},
        expect_error=True,
    )
    assert "error" in no_access_unpublish

    by_slug = await call_json(
        mcp_client,
        "akb_unpublish",
        {"slug": document_slug},
    )
    assert by_slug.get("deleted") == 1
    assert (await _public_response(api, document_slug)).status_code == 404

    by_document_uri = await call_json(
        mcp_client,
        "akb_unpublish",
        {"uri": uri_document["uri"]},
    )
    assert by_document_uri.get("deleted") == 1
    assert (await _public_response(api, uri_slug)).status_code == 404

    by_file_uri = await call_json(
        mcp_client,
        "akb_unpublish",
        {"uri": file_uri},
    )
    assert by_file_uri.get("deleted") == 1
    assert (await _public_response(api, file_slug)).status_code == 404


async def test_publication_stays_missing_after_mcp_move_recreates_path(
    api: httpx.AsyncClient,
    mcp_actor: Actor,
    mcp_client: Client,
) -> None:
    """Move is MCP-only; collection deletion and public resolution stay REST."""

    vault = await create_vault(mcp_client, "publication-resolution")
    original = await call_json(
        mcp_client,
        "akb_put",
        {
            "vault": vault,
            "collection": "resolution",
            "slug": "minutes",
            "title": "Original Minutes",
            "content": "# Original\n\nRESOLUTION_ORIGINAL_MARKER",
        },
    )
    publication = await call_json(
        mcp_client,
        "akb_publish",
        {"uri": original["uri"]},
    )
    slug = publication["slug"]
    assert (await _public_response(api, slug)).status_code == 200

    deleted = await api.delete(
        f"/api/v1/collections/{quote(vault, safe='')}/resolution",
        params={"recursive": "true"},
        headers=mcp_actor.headers,
    )
    assert deleted.status_code == 200, deleted.text
    assert isinstance(deleted.json(), dict)
    assert (await _public_response(api, slug)).status_code == 404

    mover = await call_json(
        mcp_client,
        "akb_put",
        {
            "vault": vault,
            "collection": "other",
            "slug": "second",
            "title": "Recreated Minutes",
            "content": "# Recreated\n\nRESOLUTION_RECREATED_MARKER",
        },
    )
    moved = await call_json(
        mcp_client,
        "akb_move",
        {
            "uri": mover["uri"],
            "collection": "resolution",
            "slug": "minutes",
        },
    )
    assert moved.get("path") == "resolution/minutes.md"

    resolved = await _public_response(api, slug)
    assert resolved.status_code == 404
    assert "RESOLUTION_RECREATED_MARKER" not in resolved.text


async def test_okf_sdk_round_trip_and_import_acl(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
) -> None:
    """Keep OKF structure, concept types, round-trip results, and writer ACL."""

    source_vault = await create_vault(mcp_client, "okf-source")
    await call_json(
        mcp_client,
        "akb_put",
        {
            "vault": source_vault,
            "collection": "specs",
            "slug": "api-v2",
            "title": "API v2",
            "content": "# API v2\n\nOKF_SPEC_MARKER",
            "type": "spec",
            "status": "active",
            "tags": ["api"],
        },
    )
    await call_json(
        mcp_client,
        "akb_put",
        {
            "vault": source_vault,
            "slug": "readme",
            "title": "Readme",
            "content": "# Readme\n\nOKF_README_MARKER",
            "type": "note",
        },
    )
    table = await call_json(
        mcp_client,
        "akb_create_table",
        {
            "vault": source_vault,
            "name": "metrics",
            "columns": [
                {"name": "region", "type": "text"},
                {"name": "hits", "type": "number"},
            ],
        },
    )
    assert table.get("uri")

    exported = await call_json(
        mcp_client,
        "akb_export",
        {"vault": source_vault, "format": "okf"},
    )
    files = exported.get("files")
    assert isinstance(files, dict)
    assert exported.get("file_count") == len(files) and exported["file_count"] > 0
    assert "index.md" in files and "okf_version" in files["index.md"]
    assert "log.md" in files
    assert "specs/api-v2.md" in files
    concept_files = {path: content for path, content in files.items() if path not in {"index.md", "log.md"}}
    assert concept_files
    assert all(_okf_type(content) for content in concept_files.values())
    assert _okf_type(files["specs/api-v2.md"]) == "spec"
    assert any(_okf_type(content) == "table" for content in concept_files.values())

    target_vault = await create_vault(mcp_client, "okf-target")
    imported = await call_json(
        mcp_client,
        "akb_import",
        {"vault": target_vault, "files": files, "status": "active"},
    )
    assert imported.get("created", 0) >= 2
    assert imported.get("failed") == 0
    assert imported.get("errors") == []

    browsed = await call_json(
        mcp_client,
        "akb_browse",
        {"vault": target_vault, "depth": -1, "content_type": "documents"},
    )
    imported_items = {item.get("path"): item for item in browsed.get("items", []) if item.get("type") == "document"}
    assert "specs/api-v2.md" in imported_items
    assert "readme.md" in imported_items
    imported_spec = await call_json(
        mcp_client,
        "akb_get",
        {"uri": imported_items["specs/api-v2.md"]["uri"]},
    )
    assert imported_spec.get("type") == "spec"
    assert "OKF_SPEC_MARKER" in imported_spec.get("content", "")

    denied = await call_json(
        secondary_mcp_client.client,
        "akb_import",
        {
            "vault": target_vault,
            "files": {"unauthorized.md": "---\ntype: note\n---\nnope"},
        },
        expect_error=True,
    )
    assert "error" in denied
