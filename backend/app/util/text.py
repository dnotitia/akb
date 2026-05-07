"""Unicode normalization helpers.

Why this exists: macOS filesystems (HFS+, APFS) store Hangul filenames
as NFD (decomposed jamo). When such filenames are read by a Python
script on macOS and forwarded to the backend verbatim, titles, paths,
and sometimes body text enter the database as NFD. BM25 tokenizers and
embedding models treat the NFD form as different tokens from NFC, so
queries typed in normal NFC never match the NFD-indexed content —
documents exist but are invisible to search.

Single source of truth for "make this NFC before we persist it" is
`to_nfc()`. The recursive variant normalizes arbitrarily nested
str/list/tuple/dict structures. A Pydantic mixin (`NFCModel`) applies
`to_nfc_any` to every field on instantiation, which is cheap (NFC is
idempotent) and catches anything caller-side normalization missed.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from pydantic import BaseModel, model_validator


def to_nfc(s: str) -> str:
    """Return NFC-normalized form. Safe to call on already-NFC text."""
    return unicodedata.normalize("NFC", s)


def to_nfc_any(value: Any) -> Any:
    """Recursively NFC-normalize every string inside value.

    Leaves non-string leaves untouched. Dict keys are normalized too.
    """
    if isinstance(value, str):
        return to_nfc(value)
    if isinstance(value, list):
        return [to_nfc_any(v) for v in value]
    if isinstance(value, tuple):
        return tuple(to_nfc_any(v) for v in value)
    if isinstance(value, dict):
        return {
            (to_nfc(k) if isinstance(k, str) else k): to_nfc_any(v)
            for k, v in value.items()
        }
    return value


class NFCModel(BaseModel):
    """Pydantic base that NFC-normalizes every string field on input.

    Use as the base class for every request model that accepts
    user-supplied text (title, path, content, tags, metadata, …).
    Idempotent — applying to an already-NFC payload is a no-op.
    """

    @model_validator(mode="before")
    @classmethod
    def _normalize_nfc(cls, data: Any) -> Any:
        return to_nfc_any(data)
