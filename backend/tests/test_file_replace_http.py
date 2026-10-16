"""Opt-in File replacement HTTP regressions against real PG/object storage.

Set AKB_FILE_REPLACE_TEST_URL and AKB_E2E_PAT, or the repository runtime's
AKB_E2E_USERNAME / AKB_E2E_PASSWORD. AKB_E2E_CA_FILE optionally supplies a test
CA without disabling TLS verification. Unit collection never makes calls.
Only this module's randomly named Vault is deleted during cleanup.
"""

from __future__ import annotations

import hashlib
import os
import ssl
import uuid

import httpx
import pytest


def _hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _tls_verify():
    ca_file = os.getenv("AKB_E2E_CA_FILE")
    return ssl.create_default_context(cafile=ca_file) if ca_file else True


def _transfer(method, url, **kwargs):
    # The capability is the authorization; never forward the API bearer.
    with httpx.Client(verify=_tls_verify(), timeout=30) as client:
        return client.request(method, url, **kwargs)


@pytest.fixture(scope="module")
def files():
    origin = os.getenv("AKB_FILE_REPLACE_TEST_URL")
    if not origin:
        pytest.skip("set AKB_FILE_REPLACE_TEST_URL for the real-storage HTTP lane")
    with httpx.Client(base_url=origin, verify=_tls_verify(), timeout=30) as client:
        token = os.getenv("AKB_E2E_PAT")
        if not token:
            username, password = os.getenv("AKB_E2E_USERNAME"), os.getenv("AKB_E2E_PASSWORD")
            if not username or not password:
                pytest.fail("provide a fixture PAT or the runtime's fixture credentials")
            login = client.post("/api/v1/auth/login", json={"username": username, "password": password})
            assert login.status_code == 200
            token = login.json()["token"]
        client.headers["Authorization"] = "Bearer " + token
        vault = "file-replace-test-" + uuid.uuid4().hex[:12]
        created = client.post("/api/v1/vaults", params={"name": vault})
        assert created.status_code == 200, created.text
        try:
            yield client, vault
        finally:
            deleted = client.delete("/api/v1/vaults/" + vault)
            assert deleted.status_code == 200, deleted.text


def _upload(files, body: bytes):
    client, vault = files
    prepared = client.post(
        f"/api/v1/files/{vault}/upload",
        params={"filename": uuid.uuid4().hex + ".bin", "content_hash": _hash(body)},
    )
    assert prepared.status_code == 200, prepared.text
    data = prepared.json()
    assert _transfer("PUT", data["upload_url"], content=body).status_code == 200
    file_id = data["uri"].rsplit("/file/", 1)[1]
    confirmed = client.post(
        f"/api/v1/files/{vault}/{file_id}/confirm", params={"content_hash": _hash(body)},
    )
    assert confirmed.status_code == 200, confirmed.text
    return file_id, data, confirmed.json()


def _bytes(files, file_id):
    client, vault = files
    metadata = client.get(f"/api/v1/files/{vault}/{file_id}/download")
    assert metadata.status_code == 200, metadata.text
    response = _transfer("GET", metadata.json()["download_url"])
    assert response.status_code == 200
    assert _hash(response.content) == metadata.json()["content_hash"]
    return response.content


def _replace(files, file_id, body, **preconditions):
    client, vault = files
    prepared = client.post(
        f"/api/v1/files/{vault}/{file_id}/replace",
        params={"content_hash": _hash(body), **preconditions},
    )
    assert prepared.status_code == 200, prepared.text
    data = prepared.json()
    assert _transfer("PUT", data["upload_url"], content=body).status_code == 200
    return data


def _confirm(files, file_id, replacement, body, **preconditions):
    client, vault = files
    return client.post(
        f"/api/v1/files/{vault}/{file_id}/replace/{replacement['replacement_id']}/confirm",
        params={"content_hash": _hash(body), **preconditions},
    )


@pytest.mark.parametrize("size", [127, 17 * 1024 * 1024])
def test_actual_replacement_round_trips_and_rejects_obsolete_original_grant(files, size):
    original, updated = b"original bytes", b"b" * size
    fid, initial, old = _upload(files, original)
    pins = {"expected_content_hash": old["content_hash"], "expected_version": old["version"]}
    replacement = _replace(files, fid, updated, **pins)
    confirmed = _confirm(files, fid, replacement, updated, **pins)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["uri"] == initial["uri"]
    assert confirmed.json()["content_hash"] == _hash(updated)
    assert _bytes(files, fid) == updated

    stale = _transfer("PUT", initial["upload_url"], content=b"stale original capability")
    assert stale.status_code == 404
    assert _bytes(files, fid) == updated


def test_confirmed_original_upload_replay_cannot_overwrite_the_file(files):
    original = b"normal confirmed upload"
    fid, initial, _old = _upload(files, original)
    replay = _transfer("PUT", initial["upload_url"], content=b"wrong replay bytes")
    assert replay.status_code == 200
    assert _bytes(files, fid) == original


def test_replacement_replay_failure_cannot_destroy_published_bytes(files):
    updated = b"published replacement"
    fid, _initial, _old = _upload(files, b"original for replay")
    replacement = _replace(files, fid, updated)
    assert _confirm(files, fid, replacement, updated).status_code == 200
    assert _bytes(files, fid) == updated

    # Reuse the still-valid staging capability, not a capability to live bytes.
    assert _transfer("PUT", replacement["upload_url"], content=b"wrong replay").status_code == 200
    mismatch = _confirm(files, fid, replacement, updated)
    assert mismatch.status_code in (409, 502)  # hash mismatch, or async staging cleanup won
    assert _bytes(files, fid) == updated

    # A failed copy after staging cleanup must not delete a prior live candidate.
    repeated = _confirm(files, fid, replacement, updated)
    assert repeated.status_code == 502
    assert _bytes(files, fid) == updated


def test_same_bytes_are_unchanged_and_stale_replace_is_rejected(files):
    client, vault = files
    original = b"same bytes are not a write"
    fid, _initial, old = _upload(files, original)
    same = client.post(
        f"/api/v1/files/{vault}/{fid}/replace", params={"content_hash": _hash(original)},
    )
    assert same.status_code == 200
    assert same.json()["unchanged"] is True
    assert same.json()["version"] == old["version"]
    stale = client.post(
        f"/api/v1/files/{vault}/{fid}/replace",
        params={"content_hash": _hash(b"new bytes"), "expected_content_hash": "0" * 64},
    )
    assert stale.status_code == 409
    assert _bytes(files, fid) == original
