"""A NUL byte must not cost a document its place in ranked search (akb#527).

A body carrying NUL is accepted, because bodies live in the payload store and
it accepts the byte. PostgreSQL `text` does not, so every attempt to index that
document raises `CharacterNotInRepertoireError`, retries to the ceiling, and is
abandoned. The document stays readable and greppable, which is exactly why the
loss is easy to miss: only ranked search is missing it.

Measured on a deployment before this change: one document, 135,537 characters
with eight NULs, updated on the 15th and last indexed on the 11th, its intent
abandoned after eight attempts.

Two boundaries, because there are two populations. New text is cleaned where
every request model already normalizes it. Text already stored is cleaned where
chunks are built, so those documents become indexable without anyone finding
and rewriting them.
"""

import pytest

from app.services.native_derived_worker import (
    build_native_document_chunks,
    build_native_file_chunks,
)
from app.util.text import NFCModel, to_nfc, to_nfc_any

BODY = "# Heading\n\nA paragraph\x00 with the byte in it.\n"


# ── the write boundary ───────────────────────────────────────────────

def test_normalization_drops_nul_and_keeps_everything_else():
    assert to_nfc("a\x00b") == "ab"
    assert to_nfc("\x00\x00") == ""
    # Composition still happens, and nothing else is touched.
    assert to_nfc("é\x00") == "é"
    assert to_nfc("tab\tnewline\nok") == "tab\tnewline\nok"


def test_normalization_stays_idempotent():
    once = to_nfc(BODY)
    assert to_nfc(once) == once
    assert "\x00" not in once


def test_every_request_model_inherits_it():
    class _Doc(NFCModel):
        title: str
        content: str

    doc = _Doc(title="t\x00itle", content=BODY)
    assert doc.title == "title"
    assert "\x00" not in doc.content
    assert "A paragraph with the byte in it." in doc.content


def test_nested_payloads_are_covered():
    cleaned = to_nfc_any({"tags": ["a\x00", "b"], "meta": {"k\x00": "v\x00"}})
    assert cleaned == {"tags": ["a", "b"], "meta": {"k": "v"}}


# ── the boundary for text already stored ─────────────────────────────

def test_a_stored_document_with_nul_still_chunks():
    """Without this, the same body raises at INSERT and the intent is lost."""
    chunks = build_native_document_chunks(
        vault_name="v", path="docs/guide.md", canonical_text=BODY,
    )
    assert chunks
    joined = "".join(c.content for c in chunks)
    assert "\x00" not in joined
    assert "A paragraph with the byte in it." in joined


def test_a_stored_text_file_with_nul_still_chunks():
    import uuid

    chunks = build_native_file_chunks(
        vault_name="v",
        path="notes/raw.txt",
        resource_id=uuid.uuid4(),
        canonical_text="plain\x00 text body\n",
    )
    assert chunks
    joined = "".join(c.content for c in chunks)
    assert "\x00" not in joined
    assert "plain text body" in joined


@pytest.mark.parametrize("body", ["\x00", "\x00\x00\n", "   \x00  "])
def test_a_body_that_is_only_nul_indexes_as_empty_not_as_an_error(body):
    """Nothing to index is a correct answer; raising at INSERT is not."""
    assert build_native_document_chunks(
        vault_name="v", path="docs/x.md", canonical_text=body,
    ) == []
