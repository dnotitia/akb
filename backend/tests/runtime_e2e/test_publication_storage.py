"""Publication behavior that writes to the runtime's object store."""

from base64 import b64decode

import httpx
import pytest

JSON_CONTENT = b'{"hello":"world","arr":[1,2,3],"nested":{"a":true}}'


@pytest.fixture
async def publication_vault(create_actor, create_vault):
    owner = await create_actor("publication")
    headers = {"Authorization": f"Bearer {owner.jwt}"}
    return await create_vault(headers, "publication"), httpx.Headers(headers)


async def _reserve(api, vault, filename, mime_type, *, collection="assets"):
    name, headers = vault
    response = await api.post(
        f"/api/v1/files/{name}/upload",
        headers=headers,
        params={"filename": filename, "collection": collection, "mime_type": mime_type},
    )
    assert response.status_code == 200, response.text
    reserved = response.json()
    assert reserved["uri"] and reserved["upload_url"]
    return reserved


async def _put(api, reserved, content, mime_type):
    response = await api.put(reserved["upload_url"], content=content, headers={"Content-Type": mime_type})
    assert response.status_code == 200


async def _upload(api, vault, filename, mime_type, content):
    name, headers = vault
    reserved = await _reserve(api, vault, filename, mime_type)
    await _put(api, reserved, content, mime_type)
    file_id = reserved["uri"].rsplit("/", 1)[-1]
    confirmed = await api.post(f"/api/v1/files/{name}/{file_id}/confirm", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    return reserved["uri"]


async def _publish(api, vault, resource_type, **options):
    name, headers = vault
    response = await api.post(
        f"/api/v1/publications/{name}/create",
        headers=headers,
        json={"resource_type": resource_type, **options},
    )
    assert response.status_code == 200, response.text
    slug = response.json()["slug"]
    assert slug
    return slug


async def _listed_slugs(api, vault):
    name, headers = vault
    response = await api.get(f"/api/v1/publications/{name}", headers=headers)
    assert response.status_code == 200, response.text
    return {publication["slug"] for publication in response.json()["publications"]}


async def test_public_file_streams_metadata_raw_bytes_and_download(api, publication_vault):
    uri = await _upload(api, publication_vault, "data.json", "application/json", JSON_CONTENT)
    slug = await _publish(api, publication_vault, "file", uri=uri)
    path = f"/api/v1/public/{slug}"

    meta = await api.get(f"{path}/meta")
    assert meta.status_code == 200, meta.text
    assert meta.json()["mime_type"] == "application/json"
    resolved = await api.get(path)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resource_type"] == "file"
    assert resolved.json()["mime_type"] == "application/json"
    assert "download_url" not in resolved.json()

    raw = await api.get(f"{path}/raw")
    assert raw.status_code == 200, raw.text
    assert raw.content == JSON_CONTENT
    assert raw.headers["x-content-type-options"] == "nosniff"
    downloaded = await api.get(f"{path}/download")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == JSON_CONTENT


async def test_file_publication_creation_is_scoped_to_the_route_vault(api, publication_vault, create_vault):
    name, headers = publication_vault
    source_name = await create_vault(dict(headers), "file-source")
    source_uri = await _upload(api, (source_name, headers), "source.txt", "text/plain", b"source-vault bytes")
    source_file_id = source_uri.rsplit("/", 1)[-1]

    # Control proves the referenced file exists and is publishable in its own vault.
    source_slug = await _publish(api, (source_name, headers), "file", uri=source_uri)
    assert (await api.get(f"/api/v1/public/{source_slug}")).status_code == 200

    rejected = await api.post(
        f"/api/v1/publications/{name}/create",
        headers=headers,
        json={"resource_type": "file", "uri": f"akb://{name}/file/{source_file_id}"},
    )
    assert rejected.status_code == 400, rejected.text
    assert "slug" not in rejected.json()


async def test_file_view_grants_reuse_one_view_for_inline_and_attachment_requests(api, publication_vault):
    uri = await _upload(api, publication_vault, "data.json", "application/json", JSON_CONTENT)
    slug = await _publish(api, publication_vault, "file", uri=uri, max_views=2)
    path = f"/api/v1/public/{slug}"
    opened = await api.get(path)
    assert opened.status_code == 200, opened.text
    grant = opened.json()["view_grant"]
    assert grant

    for endpoint in ("raw", "raw", "download"):
        response = await api.get(f"{path}/{endpoint}", params={"grant": grant})
        assert response.status_code == 200, response.text

    assert (await api.get(f"{path}/raw")).status_code == 200
    assert (await api.get(f"{path}/download")).status_code == 410
    assert (await api.get(path)).status_code == 410


@pytest.mark.parametrize(
    ("filename", "mime_type", "content", "status", "sandboxed"),
    [
        pytest.param(
            "inline.png",
            "image/png",
            b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9eMyf2QAAAAASUVORK5CYII="
            ),
            200,
            False,
            id="png",
        ),
        pytest.param("opaque.bin", "application/octet-stream", bytes(range(16)), 415, False, id="binary"),
        pytest.param("inline.html", "text/html", b"<h1>Example</h1>", 200, True, id="html"),
        pytest.param(
            "inline.svg", "image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"/>', 200, True, id="svg",
        ),
    ],
)
async def test_file_raw_enforces_media_policy(api, publication_vault, filename, mime_type, content, status, sandboxed):
    uri = await _upload(api, publication_vault, filename, mime_type, content)
    slug = await _publish(api, publication_vault, "file", uri=uri)
    raw = await api.get(f"/api/v1/public/{slug}/raw")
    assert raw.status_code == status, raw.text
    if status == 200:
        assert raw.headers["content-type"].split(";")[0] == mime_type
        assert raw.content == content
    if sandboxed:
        assert raw.headers["content-security-policy"].startswith("sandbox")


@pytest.mark.parametrize("recursive", [False, True], ids=["file", "collection"])
async def test_file_or_collection_delete_removes_publication(api, publication_vault, recursive):
    name, headers = publication_vault
    uri = await _upload(api, publication_vault, "quarterly.json", "application/json", JSON_CONTENT)
    slug = await _publish(api, publication_vault, "file", uri=uri)
    path = f"/api/v1/public/{slug}"
    assert (await api.get(path)).status_code == 200
    assert slug in await _listed_slugs(api, publication_vault)

    if recursive:
        removed = await api.delete(
            f"/api/v1/collections/{name}/assets", headers=headers, params={"recursive": "true"},
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["ok"] is True
    else:
        file_id = uri.rsplit("/", 1)[-1]
        removed = await api.delete(f"/api/v1/files/{name}/{file_id}", headers=headers)
        assert removed.status_code == 200, removed.text

    assert (await api.get(path)).status_code == 404
    assert slug not in await _listed_slugs(api, publication_vault)


@pytest.mark.parametrize("failure", ["abandoned", "hash-mismatch"])
async def test_unconfirmed_file_is_unpublishable_and_failed_confirmation_cleans_up(api, publication_vault, failure):
    name, headers = publication_vault
    reserved = await _reserve(api, publication_vault, "pending.json", "application/json")
    file_id = reserved["uri"].rsplit("/", 1)[-1]
    path = f"/api/v1/files/{name}/{file_id}"
    params = {}
    status = 404
    if failure == "hash-mismatch":
        await _put(api, reserved, JSON_CONTENT, "application/json")
        params["content_hash"] = "a" * 64
        status = 409

    rejected = await api.post(
        f"/api/v1/publications/{name}/create",
        headers=headers,
        json={"resource_type": "file", "uri": reserved["uri"]},
    )
    assert rejected.status_code == 400, rejected.text
    assert "slug" not in rejected.json()
    assert await _listed_slugs(api, publication_vault) == set()

    confirmed = await api.post(f"{path}/confirm", headers=headers, params=params)
    assert confirmed.status_code == status, confirmed.text
    cancelled = await api.post(f"{path}/cancel-upload", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {"cancelled": False, "state": "absent"}
    assert await _listed_slugs(api, publication_vault) == set()


async def test_successful_repeat_confirmation_keeps_existing_file_publication(api, publication_vault):
    name, headers = publication_vault
    uri = await _upload(api, publication_vault, "data.json", "application/json", JSON_CONTENT)
    slug = await _publish(api, publication_vault, "file", uri=uri)
    file_id = uri.rsplit("/", 1)[-1]

    confirmed = await api.post(f"/api/v1/files/{name}/{file_id}/confirm", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert slug in await _listed_slugs(api, publication_vault)
    assert (await api.get(f"/api/v1/public/{slug}")).status_code == 200


async def test_table_snapshot_freezes_rows_and_can_be_refreshed(api, publication_vault):
    name, headers = publication_vault
    table = await api.post(
        f"/api/v1/tables/{name}",
        headers=headers,
        json={"name": "products", "columns": [{"name": "name", "type": "text", "required": True}]},
    )
    assert table.status_code == 200, table.text
    assert table.json()["uri"]
    sql_path = f"/api/v1/tables/{name}/sql"
    seeded = await api.post(sql_path, headers=headers, json={"sql": "INSERT INTO products (name) VALUES ('Apple')"})
    assert seeded.status_code == 200, seeded.text
    assert seeded.json()["affected_rows"] == 1

    slug = await _publish(api, publication_vault, "table_query", query_sql="SELECT name FROM products ORDER BY name")
    snapshot_path = f"/api/v1/publications/{name}/{slug}/snapshot"
    first = await api.post(snapshot_path, headers=headers)
    assert first.status_code == 200, first.text
    first_snapshot = first.json()
    assert first_snapshot["mode"] == "snapshot"
    assert first_snapshot["snapshot_at"]
    assert "snapshot_s3_key" not in first_snapshot

    path = f"/api/v1/public/{slug}"
    before = await api.get(path)
    assert before.status_code == 200, before.text
    assert before.json()["mode"] == "snapshot"
    assert before.json()["rows"] == [{"name": "Apple"}]

    inserted = await api.post(
        sql_path, headers=headers, json={"sql": "INSERT INTO products (name) VALUES ('SnapTest')"},
    )
    assert inserted.status_code == 200, inserted.text
    assert inserted.json()["affected_rows"] == 1

    frozen = await api.get(path)
    assert frozen.status_code == 200, frozen.text
    assert frozen.json()["mode"] == "snapshot"
    assert frozen.json()["rows"] == [{"name": "Apple"}]
    assert (await api.get(f"{path}/embed")).status_code == 200

    refreshed = await api.post(snapshot_path, headers=headers)
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["snapshot_at"]
    latest = await api.get(path)
    assert latest.status_code == 200, latest.text
    assert latest.json()["rows"] == [{"name": "Apple"}, {"name": "SnapTest"}]


async def test_empty_table_snapshot_remains_resolvable(api, publication_vault):
    name, headers = publication_vault
    slug = await _publish(api, publication_vault, "table_query", query_sql="SELECT 1 AS value WHERE false")
    snapshot = await api.post(f"/api/v1/publications/{name}/{slug}/snapshot", headers=headers)
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["mode"] == "snapshot"
    assert snapshot.json()["snapshot_at"]

    response = await api.get(f"/api/v1/public/{slug}")
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


async def test_delete_vault_acknowledges_removal_with_table_file_and_publication(api, publication_vault):
    name, headers = publication_vault
    table = await api.post(
        f"/api/v1/tables/{name}",
        headers=headers,
        json={"name": "products", "columns": [{"name": "name", "type": "text"}]},
    )
    assert table.status_code == 200, table.text
    uri = await _upload(api, publication_vault, "data.json", "application/json", JSON_CONTENT)
    await _publish(api, publication_vault, "file", uri=uri)

    deleted = await api.delete(f"/api/v1/vaults/{name}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True
