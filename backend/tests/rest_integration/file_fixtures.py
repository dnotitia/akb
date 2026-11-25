"""Upload through the app's capability URL with real S3-compatible storage."""

import os
from collections.abc import Iterator
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.services.adapters import s3_adapter

from .helpers import Actor


@pytest.fixture
def object_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    endpoint = os.environ.get("AKB_TEST_S3_ENDPOINT")
    assert endpoint, "Set AKB_TEST_S3_ENDPOINT to the isolated test object store"
    bucket = f"akb-rest-{uuid4().hex}"
    monkeypatch.setattr(settings, "s3_auth_mode", "static")
    monkeypatch.setattr(settings, "s3_endpoint_url", endpoint)
    monkeypatch.setattr(settings, "s3_bucket", bucket)
    monkeypatch.setattr(settings, "s3_access_key", os.environ.get("AKB_TEST_S3_ACCESS_KEY", "akb-ci"))
    monkeypatch.setattr(
        settings, "s3_secret_key",
        os.environ.get("AKB_TEST_S3_SECRET_KEY", "akb-ci-secret"),  # pragma: allowlist secret
    )
    monkeypatch.setattr(s3_adapter, "_session", None)
    monkeypatch.setattr(s3_adapter, "_internal_client", None)
    monkeypatch.setattr(s3_adapter, "_bucket_verified", set())

    from app.api.routes import files

    # The File service captures its bucket during construction.
    monkeypatch.setattr(files, "file_service", files.FileService())
    client = s3_adapter.client()
    try:
        s3_adapter.ensure_bucket(bucket)
        try:
            yield
        finally:
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                if objects := [{"Key": item["Key"]} for item in page.get("Contents", [])]:
                    result = client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
                    assert not result.get("Errors"), result
            client.delete_bucket(Bucket=bucket)
    finally:
        client.close()


async def upload_file(
    api: httpx.AsyncClient,
    actor: Actor,
    vault: str,
    *,
    filename: str,
    collection: str,
    content: bytes,
    mime_type: str,
) -> str:
    files_path = f"/api/v1/files/{quote(vault, safe='')}"
    initiated = await api.post(
        f"{files_path}/upload",
        params={"filename": filename, "collection": collection, "mime_type": mime_type},
        headers=actor.headers,
    )
    assert initiated.status_code == 200, initiated.text
    payload = initiated.json()
    assert isinstance(payload, dict)
    file_uri = payload.get("uri")
    upload_url = payload.get("upload_url")
    assert isinstance(file_uri, str) and file_uri
    assert isinstance(upload_url, str) and upload_url

    uploaded = await api.put(upload_url, content=content, headers={"Content-Type": mime_type})
    assert uploaded.status_code in {200, 201}, uploaded.text

    file_id = quote(file_uri.rsplit("/", 1)[-1], safe="")
    confirmed = await api.post(f"{files_path}/{file_id}/confirm", headers=actor.headers)
    assert confirmed.status_code == 200, confirmed.text
    return file_uri
