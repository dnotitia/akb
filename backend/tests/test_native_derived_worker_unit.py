from __future__ import annotations

import hashlib

import pytest

from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_payload_verification import (
    NativePayloadPlacementError,
    verify_native_head_body,
)
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


@pytest.mark.parametrize(
    "placement",
    (M1ReferencePayloadStore.selected_placement, M1PgBodyStore.selected_placement),
)
def test_native_head_body_verification_dispatches_to_the_manifest_placement(placement):
    canonical = b"verified native body\n"
    assert verify_native_head_body(
        {
            "canonical_bytes": canonical,
            "digest": hashlib.sha256(canonical).hexdigest(),
            "byte_size": len(canonical),
            "encoding": "utf-8",
            "selected_placement": placement,
            "verification_profile": "sha256-size-utf8-v1",
        }
    ) == canonical


def test_native_head_body_verification_rejects_an_unknown_manifest_placement():
    with pytest.raises(NativePayloadPlacementError, match="Unsupported native payload placement"):
        verify_native_head_body(
            {
                "selected_placement": "unknown-placement-v1",
            }
        )


@pytest.mark.parametrize(
    "placement",
    (M1ReferencePayloadStore.selected_placement, M1PgBodyStore.selected_placement),
)
def test_native_head_body_verification_rejects_a_mismatched_placement_profile(placement):
    canonical = b"verified native body\n"
    with pytest.raises(RuntimeError, match="verification profile mismatch"):
        verify_native_head_body(
            {
                "canonical_bytes": canonical,
                "digest": hashlib.sha256(canonical).hexdigest(),
                "byte_size": len(canonical),
                "encoding": "utf-8",
                "selected_placement": placement,
                "verification_profile": "mismatched-profile-v1",
            }
        )
