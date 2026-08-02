from __future__ import annotations

import uuid

import pytest

from app.models.document import GrepResponse
from app.services.m1_native_grep_service import HeadBody, M1NativeGrepService
from app.services.search_service import active_document_source_type


def test_document_source_selection_is_exactly_guarded():
    assert active_document_source_type(
        backend="bare_git_current",
        measurement_only=False,
        database="akb",
    ) == "document"
    assert active_document_source_type(
        backend="native_ledger_m1",
        measurement_only=True,
        database="akb_revision_m1_measurement",
    ) == "native_document"

    with pytest.raises(RuntimeError, match="dedicated measurement database"):
        active_document_source_type(
            backend="native_ledger_m1",
            measurement_only=True,
            database="akb",
        )


def test_native_public_grep_shape_preserves_document_contract():
    body = HeadBody(
        namespace_id=uuid.uuid4(),
        vault="measure",
        resource_id=uuid.uuid4(),
        surface="document",
        path="guide.md",
        revision_id="a" * 40,
        digest="b" * 64,
        byte_size=74,
        canonical_bytes=b"---\ntitle: Guide\ntags:\n  - secret-frontmatter-token\n---\nneedle body\n",
    )

    assert body.title == "Guide"
    assert body.search_text == "needle body"
    result = M1NativeGrepService._public_response(
        pattern="needle",
        regex=False,
        native={
            "total_resources": 1,
            "total_matches": 1,
            "returned_resources": 1,
            "returned_matches": 1,
            "truncated": False,
            "results": [{
                "uri": body.uri,
                "vault": body.vault,
                "path": body.path,
                "title": body.title,
                "matches": [{"line": 1, "text": "needle body"}],
            }],
        },
    )
    assert result == {
        "pattern": "needle",
        "regex": False,
        "returned_docs": 1,
        "returned_matches": 1,
        "total_docs": 1,
        "total_matches": 1,
        "truncated": False,
        "results": [{
            "uri": "akb://measure/doc/guide.md",
            "vault": "measure",
            "path": "guide.md",
            "title": "Guide",
            "resource_type": None,
            "revision": None,
            "content_hash": None,
            "matches": [{"section": None, "text": "needle body"}],
        }],
        "measurement_resources": [{"uri": "akb://measure/doc/guide.md"}],
    }


def test_rest_grep_model_preserves_additive_text_file_head_identity():
    response = GrepResponse.model_validate(
        {
            "kind": "grep",
            "pattern": "needle",
            "regex": False,
            "total_docs": 1,
            "total_matches": 1,
            "results": [{
                "uri": "akb://measure/file/00000000-0000-0000-0000-000000000001",
                "vault": "measure",
                "path": "src/main.py",
                "title": "main.py",
                "resource_type": "file",
                "revision": "a" * 40,
                "content_hash": "b" * 64,
                "matches": [{"section": None, "text": "needle"}],
            }],
        }
    )

    item = response.model_dump(exclude_none=True)["results"][0]
    assert item["resource_type"] == "file"
    assert item["revision"] == "a" * 40
    assert item["content_hash"] == "b" * 64
