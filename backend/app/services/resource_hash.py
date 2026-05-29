"""Resource content hashing helpers.

AKB exposes ``content_hash`` as a resource-level integrity field. The
algorithm name is explicit so future migrations can add other algorithms
without changing the response contract.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

HASH_ALGORITHM = "sha256"
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def compute_text_content_hash(content: str) -> str:
    """Return the sha256 digest for a canonical document body string."""
    return compute_bytes_content_hash(content.encode("utf-8"))


def compute_bytes_content_hash(content: bytes) -> str:
    """Return the sha256 digest for raw resource bytes."""
    return hashlib.sha256(content).hexdigest()


def compute_stream_content_hash(chunks: Iterable[bytes]) -> str:
    """Return the sha256 digest for an iterable byte stream."""
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def is_sha256_hex(value: str | None) -> bool:
    return bool(value and _SHA256_HEX_RE.fullmatch(value))
