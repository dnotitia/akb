"""Detailed publication and OKF regressions through the official Python SDK."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx
from mcp import Client

from .conftest import SecondaryMcpSession
from .runtime import RuntimeContext
from .test_product_e2e import _call_json, _create_vault


async def _public_response(
    runtime_session: RuntimeContext,
    slug: str,
) -> httpx.Response:
    url = urljoin(
        f"{runtime_session.descriptor.app_origin}/",
        f"api/v1/public/{quote(slug, safe='')}",
    )
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        return await client.get(url)


async def _rest_json(
    runtime_session: RuntimeContext,
    pat: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    url = urljoin(f"{runtime_session.descriptor.app_origin}/", path.lstrip("/"))
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        response = await client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {pat}"},
            json=body,
        )
    assert response.status_code == expected_status
    result = response.json()
    assert isinstance(result, dict)
    return result


async def _upload_file(
    runtime_session: RuntimeContext,
    vault: str,
    *,
    filename: str,
    collection: str,
    content: bytes,
    mime_type: str,
) -> str:
    encoded_vault = quote(vault, safe="")
    upload_path = f"api/v1/files/{encoded_vault}/upload"
    upload_url = urljoin(f"{runtime_session.descriptor.app_origin}/", upload_path)
    headers = {"Authorization": f"Bearer {runtime_session.pat}"}
    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        initiated = await client.post(
            upload_url,
            params={
                "filename": filename,
                "collection": collection,
                "mime_type": mime_type,
            },
            headers=headers,
        )
        assert initiated.status_code == 200
        payload = initiated.json()
        assert isinstance(payload, dict)
        file_uri = payload.get("uri")
        presigned_url = payload.get("upload_url")
        assert isinstance(file_uri, str) and file_uri
        assert isinstance(presigned_url, str) and presigned_url

        uploaded = await client.put(
            presigned_url,
            content=content,
            headers={"Content-Type": mime_type},
        )
        assert uploaded.status_code in {200, 201}

        file_id = file_uri.rsplit("/", 1)[-1]
        confirmed = await client.post(
            urljoin(
                f"{runtime_session.descriptor.app_origin}/",
                f"api/v1/files/{encoded_vault}/{quote(file_id, safe='')}/confirm",
            ),
            headers=headers,
        )
        assert confirmed.status_code == 200
    return file_uri


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


async def test_publication_sdk_lifecycle_and_rest_oracle(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    runtime_session: RuntimeContext,
) -> None:
    """Cover publication CRUD, filters, compatibility, ACLs, and public reads."""

    vault = await _create_vault(mcp_client, runtime_session, "publication-detail")
    document = await _call_json(
        mcp_client,
        runtime_session,
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

    document_publication = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publish",
        {"uri": document_uri},
    )
    _assert_canonical_publication(document_publication)
    document_slug = document_publication["slug"]
    public_document = await _public_response(runtime_session, document_slug)
    assert public_document.status_code == 200
    public_document_body = public_document.json()
    assert public_document_body.get("title") == "Live Document"
    assert "PUBLICATION_DOCUMENT_MARKER" in public_document_body.get("content", "")

    uri_document = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "docs",
            "slug": "uri-unpublish-document",
            "title": "URI Unpublish Document",
            "content": "# URI Unpublish\n\nURI_UNPUBLISH_MARKER",
        },
    )
    uri_publication = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publish",
        {"uri": uri_document["uri"]},
    )
    _assert_canonical_publication(uri_publication)
    uri_slug = uri_publication["slug"]
    assert (await _public_response(runtime_session, uri_slug)).status_code == 200

    file_uri = await _upload_file(
        runtime_session,
        vault,
        filename="publication.json",
        collection="assets",
        content=b'{"publication": true}',
        mime_type="application/json",
    )
    file_publication = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publish",
        {"uri": file_uri, "resource_type": "file"},
    )
    _assert_canonical_publication(file_publication)
    file_slug = file_publication["slug"]
    public_file = await _public_response(runtime_session, file_slug)
    assert public_file.status_code == 200
    assert public_file.json().get("name") == "publication.json"

    table = await _call_json(
        mcp_client,
        runtime_session,
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
    seeded = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {
            "vault": vault,
            "sql": ("INSERT INTO publication_products (name, price) VALUES ('Apple', 1), ('Bagel', 2)"),
        },
    )
    assert "INSERT" in str(seeded.get("result"))
    query_publication = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publish",
        {
            "vault": vault,
            "resource_type": "table_query",
            "query_sql": "SELECT name, price FROM publication_products ORDER BY name",
        },
    )
    _assert_canonical_publication(query_publication)
    query_slug = query_publication["slug"]
    public_query = await _public_response(runtime_session, query_slug)
    assert public_query.status_code == 200
    assert [row["name"] for row in public_query.json()["rows"]] == ["Apple", "Bagel"]

    all_publications = await _call_json(
        mcp_client,
        runtime_session,
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

    document_publications = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publications",
        {"vault": vault, "resource_type": "document"},
    )
    assert document_publications.get("total") == len(document_publications["publications"])
    assert all(item.get("resource_type") == "document" for item in document_publications["publications"])

    file_publications = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publications",
        {"vault": vault, "resource_type": "file"},
    )
    assert file_publications.get("total") == 1
    assert file_publications["publications"][0]["slug"] == file_slug

    query_publications = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publications",
        {"vault": vault, "resource_type": "table_query"},
    )
    assert query_publications.get("total") == 1
    assert query_publications["publications"][0]["slug"] == query_slug

    snapshot = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publication_snapshot",
        {"slug": query_slug},
    )
    assert snapshot.get("mode") == "snapshot"
    assert isinstance(snapshot.get("snapshot_at"), str) and snapshot["snapshot_at"]
    public_snapshot = await _public_response(runtime_session, query_slug)
    assert public_snapshot.status_code == 200
    assert [row["name"] for row in public_snapshot.json()["rows"]] == ["Apple", "Bagel"]

    missing_uri = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publish",
        {"resource_type": "document"},
        expect_error=True,
    )
    assert missing_uri.get("code") == "invalid_argument"
    legacy_mode = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publish",
        {"uri": document_uri, "mode": "snapshot"},
        expect_error=True,
    )
    assert "unknown argument" in str(legacy_mode.get("error", "")).lower()

    no_access_list = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_publications",
        {"vault": vault},
        expect_error=True,
    )
    assert "error" in no_access_list
    no_access_publish = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_publish",
        {"uri": document_uri},
        expect_error=True,
    )
    assert "error" in no_access_publish
    no_access_snapshot = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_publication_snapshot",
        {"slug": query_slug},
        expect_error=True,
    )
    assert "error" in no_access_snapshot
    no_access_unpublish = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_unpublish",
        {"slug": document_slug},
        expect_error=True,
    )
    assert "error" in no_access_unpublish

    by_slug = await _call_json(
        mcp_client,
        runtime_session,
        "akb_unpublish",
        {"slug": document_slug},
    )
    assert by_slug.get("deleted") == 1
    assert (await _public_response(runtime_session, document_slug)).status_code == 404

    by_document_uri = await _call_json(
        mcp_client,
        runtime_session,
        "akb_unpublish",
        {"uri": uri_document["uri"]},
    )
    assert by_document_uri.get("deleted") == 1
    assert (await _public_response(runtime_session, uri_slug)).status_code == 404

    by_file_uri = await _call_json(
        mcp_client,
        runtime_session,
        "akb_unpublish",
        {"uri": file_uri},
    )
    assert by_file_uri.get("deleted") == 1
    assert (await _public_response(runtime_session, file_slug)).status_code == 404


async def test_publication_resolution_after_sdk_move_keeps_rest_observation(
    mcp_client: Client,
    runtime_session: RuntimeContext,
) -> None:
    """Move is MCP-only; collection deletion and public resolution stay REST."""

    vault = await _create_vault(mcp_client, runtime_session, "publication-resolution")
    original = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "resolution",
            "slug": "minutes",
            "title": "Original Minutes",
            "content": "# Original\n\nRESOLUTION_ORIGINAL_MARKER",
        },
    )
    publication = await _call_json(
        mcp_client,
        runtime_session,
        "akb_publish",
        {"uri": original["uri"]},
    )
    slug = publication["slug"]
    assert (await _public_response(runtime_session, slug)).status_code == 200

    await _rest_json(
        runtime_session,
        runtime_session.pat,
        "DELETE",
        f"api/v1/collections/{quote(vault, safe='')}/resolution?recursive=true",
    )
    assert (await _public_response(runtime_session, slug)).status_code == 404

    mover = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "other",
            "slug": "second",
            "title": "Recreated Minutes",
            "content": "# Recreated\n\nRESOLUTION_RECREATED_MARKER",
        },
    )
    moved = await _call_json(
        mcp_client,
        runtime_session,
        "akb_move",
        {
            "uri": mover["uri"],
            "collection": "resolution",
            "slug": "minutes",
        },
    )
    assert moved.get("path") == "resolution/minutes.md"

    resolved = await _public_response(runtime_session, slug)
    assert resolved.status_code == 404
    assert "RESOLUTION_RECREATED_MARKER" not in resolved.text


async def test_okf_sdk_round_trip_and_import_acl(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    runtime_session: RuntimeContext,
) -> None:
    """Keep OKF structure, concept types, round-trip results, and writer ACL."""

    source_vault = await _create_vault(mcp_client, runtime_session, "okf-source")
    await _call_json(
        mcp_client,
        runtime_session,
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
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": source_vault,
            "slug": "readme",
            "title": "Readme",
            "content": "# Readme\n\nOKF_README_MARKER",
            "type": "note",
        },
    )
    table = await _call_json(
        mcp_client,
        runtime_session,
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

    exported = await _call_json(
        mcp_client,
        runtime_session,
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

    target_vault = await _create_vault(mcp_client, runtime_session, "okf-target")
    imported = await _call_json(
        mcp_client,
        runtime_session,
        "akb_import",
        {"vault": target_vault, "files": files, "status": "active"},
    )
    assert imported.get("created", 0) >= 2
    assert imported.get("failed") == 0
    assert imported.get("errors") == []

    browsed = await _call_json(
        mcp_client,
        runtime_session,
        "akb_browse",
        {"vault": target_vault, "depth": -1, "content_type": "documents"},
    )
    imported_items = {item.get("path"): item for item in browsed.get("items", []) if item.get("type") == "document"}
    assert "specs/api-v2.md" in imported_items
    assert "readme.md" in imported_items
    imported_spec = await _call_json(
        mcp_client,
        runtime_session,
        "akb_get",
        {"uri": imported_items["specs/api-v2.md"]["uri"]},
    )
    assert imported_spec.get("type") == "spec"
    assert "OKF_SPEC_MARKER" in imported_spec.get("content", "")

    denied = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_import",
        {
            "vault": target_vault,
            "files": {"unauthorized.md": "---\ntype: note\n---\nnope"},
        },
        expect_error=True,
    )
    assert "error" in denied
