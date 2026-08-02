from __future__ import annotations

from app.services.native_derived_worker import (
    NATIVE_DOCUMENT_SOURCE,
    build_native_document_chunks,
)


def test_native_document_chunks_are_real_body_chunks_with_search_metadata():
    raw = """---
title: Native title
type: runbook
summary: Derived summary
tags:
  - native
---
# Recovery
exact searchable body
"""

    chunks = build_native_document_chunks(
        vault_name="measure",
        path="ops/recovery.md",
        canonical_text=raw,
    )

    assert NATIVE_DOCUMENT_SOURCE == "native_document"
    assert len(chunks) == 1
    assert chunks[0].section_path == "# Recovery"
    assert "TITLE: Native title" in chunks[0].content
    assert "PATH: measure/ops/recovery.md" in chunks[0].content
    assert "exact searchable body" in chunks[0].content
    assert "title: Native title" not in chunks[0].content


def test_native_document_chunks_do_not_create_a_synthetic_empty_projection():
    raw = """---
title: Empty
---
"""

    assert build_native_document_chunks(
        vault_name="measure",
        path="empty.md",
        canonical_text=raw,
    ) == []
