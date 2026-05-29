from __future__ import annotations

from hashlib import sha256

from app.services.resource_hash import (
    HASH_ALGORITHM,
    compute_bytes_content_hash,
    compute_text_content_hash,
    is_sha256_hex,
)


def test_text_content_hash_is_sha256_over_utf8_body() -> None:
    body = "## Title\n\n본문 with ascii\n"

    digest = compute_text_content_hash(body)

    assert HASH_ALGORITHM == "sha256"
    assert digest == sha256(body.encode("utf-8")).hexdigest()
    assert is_sha256_hex(digest)


def test_bytes_content_hash_is_sha256_over_raw_file_bytes() -> None:
    payload = b"\x00akb file bytes\n\xff"

    digest = compute_bytes_content_hash(payload)

    assert digest == sha256(payload).hexdigest()
    assert is_sha256_hex(digest)


def test_sha256_validator_rejects_non_sha256_values() -> None:
    assert not is_sha256_hex("")
    assert not is_sha256_hex("abc")
    assert not is_sha256_hex("g" * 64)
