"""Public file transfers through the running app and real S3-backed storage."""

import asyncio
import hashlib
from uuid import UUID


async def test_upload_confirmation_and_download_report_the_stored_sha256(api, create_actor, create_vault):
    owner = await create_actor("byte-hash")
    vault = await create_vault(owner.headers, "byte-hash")
    body = b"AKB file hash e2e\n"
    sha256 = hashlib.sha256(body).hexdigest()

    reserved = await api.post(
        f"/api/v1/files/{vault}/upload",
        headers=owner.headers,
        params={
            "filename": "hash.txt",
            "collection": "files",
            "description": "hash-e2e",
            "mime_type": "text/plain",
        },
    )
    assert reserved.status_code == 200
    upload = reserved.json()
    file_id = UUID(upload["uri"].rsplit("/file/", 1)[1])

    # Only the opaque URL authorizes this transfer. No PAT goes to the byte endpoint.
    written = await api.put(upload["upload_url"], headers={"Content-Type": "text/plain"}, content=body)
    assert written.status_code == 200
    confirmed = await api.post(
        f"/api/v1/files/{vault}/{file_id}/confirm",
        headers=owner.headers,
        params={"content_hash": sha256, "hash_algorithm": "sha256"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["content_hash"] == sha256
    assert confirmed.json()["hash_algorithm"] == "sha256"

    response = await api.get(f"/api/v1/files/{vault}/{file_id}/download", headers=owner.headers)
    assert response.status_code == 200
    download = response.json()
    assert download["content_hash"] == sha256
    assert download["hash_algorithm"] == "sha256"
    transferred = await api.get(download["download_url"])
    assert transferred.status_code == 200
    assert transferred.content == body


async def test_file_delete_commits_an_outbox_row_and_worker_handles_an_absent_object(
    api, create_actor, create_vault, pg_connection,
):
    owner = await create_actor("s3-outbox")
    vault = await create_vault(owner.headers, "s3-outbox")
    key = f"{vault}/missing.txt"
    db = pg_connection

    # Leave the key absent from S3: the real worker must handle an idempotent delete.
    file_id = await db.fetchval(
        """
        INSERT INTO vault_files
            (vault_id, name, s3_key, mime_type, size_bytes, description, created_by)
        SELECT id, 'missing.txt', $2, 'text/plain', 12, '', $3
          FROM vaults WHERE name = $1
        RETURNING id
        """,
        vault, key, owner.username,
    )
    assert file_id is not None
    assert not await db.fetchval("SELECT EXISTS(SELECT 1 FROM s3_delete_outbox WHERE s3_key = $1)", key)

    deleted = await api.delete(f"/api/v1/files/{vault}/{file_id}", headers=owner.headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    # Read both sides in one snapshot. The worker can already have processed
    # the intent, but the completed row survives for the outbox grace period.
    after = await db.fetchrow(
        """
        SELECT EXISTS(SELECT 1 FROM vault_files WHERE id = $1) AS file_present,
               (SELECT count(*) FROM s3_delete_outbox WHERE s3_key = $2) AS outbox_count
        """,
        file_id, key,
    )
    assert after["file_present"] is False
    assert after["outbox_count"] == 1

    async with asyncio.timeout(30):
        while not await db.fetchval(
            "SELECT processed_at IS NOT NULL FROM s3_delete_outbox WHERE s3_key = $1", key,
        ):
            await asyncio.sleep(0.25)
    row = await db.fetchrow("SELECT retry_count, last_error FROM s3_delete_outbox WHERE s3_key = $1", key)
    assert row["retry_count"] == 0
    assert row["last_error"] is None
