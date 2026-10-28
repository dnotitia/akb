"""Shared public document-write predicates; tokens are not human text."""
from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from app.exceptions import ValidationError
from app.util.text import NFCModel

REVISION_PATTERN = r"^[0-9a-f]{40}$"
CONTENT_HASH_PATTERN = r"^[0-9a-f]{64}$"
RevisionToken = Annotated[str, Field(pattern=REVISION_PATTERN, min_length=40, max_length=40)]
ContentHashToken = Annotated[str, Field(pattern=CONTENT_HASH_PATTERN, min_length=64, max_length=64)]
BODY_UPDATE_CONDITION: dict[str, Any] = {
    "if": {"required": ["content"], "properties": {"content": {"type": "string"}}},
    "then": {"anyOf": [
        {"required": ["expected_commit"], "properties": {"expected_commit": {"type": "string"}}},
        {"required": ["expected_content_hash"], "properties": {"expected_content_hash": {"type": "string"}}},
    ]},
}


def validate_token(value, *, name: str, pattern: str = REVISION_PATTERN) -> None:
    if value is not None and (not isinstance(value, str) or re.fullmatch(pattern, value) is None):
        raise ValidationError(f"{name} must be a full lowercase hexadecimal token returned by the original read")


class VersionedDocumentRequest(NFCModel):
    @model_validator(mode="before")
    @classmethod
    def _normalize_nfc(cls, data):
        # Preserve the original token meaning: NFCModel removes NUL from text.
        # Reject malformed authority tokens before any such normalization.
        if isinstance(data, dict):
            for name in ("expected_commit", "base_commit", "expected_content_hash"):
                if name in cls.model_fields:
                    try:
                        validate_token(
                            data.get(name), name=name,
                            pattern=CONTENT_HASH_PATTERN if name == "expected_content_hash" else REVISION_PATTERN,
                        )
                    except ValidationError as exc:
                        raise PydanticCustomError("document_precondition", str(exc)) from exc
        return super()._normalize_nfc(data)


def validate_update_preconditions(*, content, expected_commit, expected_content_hash) -> None:
    validate_token(expected_commit, name="expected_commit")
    validate_token(expected_content_hash, name="expected_content_hash", pattern=CONTENT_HASH_PATTERN)
    if content is not None and expected_commit is None and expected_content_hash is None:
        raise ValidationError(
            "Full-body updates require expected_commit or expected_content_hash from the original read. "
            "Read the document, review/merge current content, then retry with that read's token."
        )
