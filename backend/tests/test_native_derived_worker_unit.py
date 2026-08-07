from __future__ import annotations

import hashlib
import uuid

import pytest

from app.services.index_service import MAX_CHUNK_SIZE, SOURCE_TYPES
from app.services.m1_pg_body_store import M1PgBodyStore
from app.services.m1_reference_payload_store import M1ReferencePayloadStore
from app.services.native_payload_verification import (
    NativePayloadPlacementError,
    verify_native_head_body,
)
from app.services.native_derived_worker import (
    DIRECT_GREP_DELIVERY,
    NATIVE_DOCUMENT_SOURCE,
    NATIVE_FILE_SOURCE,
    SELECTED_DELIVERY,
    build_native_document_chunks,
    build_native_file_chunks,
    source_type_for_surface,
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


def test_native_file_chunks_carry_the_body_and_file_addressing():
    resource_id = uuid.UUID("11111111-2222-3333-4444-555555555555")

    chunks = build_native_file_chunks(
        vault_name="measure",
        path="src/app/main.py",
        resource_id=resource_id,
        canonical_text="def handler():\n    return 'exact file body'\n",
    )

    assert NATIVE_FILE_SOURCE == "native_file"
    assert len(chunks) == 1
    assert chunks[0].section_path == ""
    assert "TITLE: main.py" in chunks[0].content
    assert "TYPE: file" in chunks[0].content
    assert "VAULT: measure" in chunks[0].content
    # File addressing: the vault-relative path, and the canonical File URI —
    # never `PATH: measure/src/app/main.py` and never a doc:// locator.
    assert "PATH: src/app/main.py" in chunks[0].content
    assert (
        f"URI: akb://measure/coll/src/app/file/{resource_id}" in chunks[0].content
    )
    assert "akb://measure/doc/" not in chunks[0].content
    assert "return 'exact file body'" in chunks[0].content


def test_native_file_chunks_do_not_lose_text_before_a_comment_that_looks_like_a_heading():
    """A text File is not markdown.

    `chunk_markdown` treats any `# ` line as a section boundary and emits only
    the spans it recognizes, so a Python comment would silently swallow every
    preceding line. The File body must survive chunking intact.
    """
    body = "import os\n\n# TODO: replace the shim\n\nvalue = os.environ['A']\n"

    chunks = build_native_file_chunks(
        vault_name="measure",
        path="shim.py",
        resource_id=uuid.uuid4(),
        canonical_text=body,
    )

    assert len(chunks) == 1
    assert "import os" in chunks[0].content
    assert "# TODO: replace the shim" in chunks[0].content
    assert "value = os.environ['A']" in chunks[0].content
    assert chunks[0].section_path == ""


def test_native_file_chunks_split_a_large_body_within_the_embedding_bound():
    resource_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    body = "\n\n".join(f"paragraph {index} of the file body" for index in range(400))

    chunks = build_native_file_chunks(
        vault_name="measure",
        path="big.txt",
        resource_id=resource_id,
        canonical_text=body,
    )

    assert len(chunks) > 1
    assert all(len(chunk.content) <= MAX_CHUNK_SIZE for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    # Every chunk carries the resource-level signal, not just chunk 0 — a chunk
    # from deep inside a large file is otherwise anonymous to both retrieval
    # legs.
    assert all(chunk.content.startswith("TITLE: big.txt\n") for chunk in chunks)
    assert all(f"URI: akb://measure/file/{resource_id}" in chunk.content for chunk in chunks)
    # And no body line is lost to the split.
    joined = "\n".join(chunk.content for chunk in chunks)
    assert all(f"paragraph {index} of the file body" in joined for index in range(400))


def test_native_file_chunks_do_not_create_a_synthetic_empty_projection():
    assert build_native_file_chunks(
        vault_name="measure",
        path="blank.txt",
        resource_id=uuid.uuid4(),
        canonical_text="\n  \n",
    ) == []


def test_derived_discriminators_are_distinct_and_schema_admitted():
    # A native text File's Resource id IS the public `vault_files.id`; sharing
    # the legacy `file` discriminator would make the two authorities collide on
    # one (source_type, source_id) key.
    assert source_type_for_surface("document") == NATIVE_DOCUMENT_SOURCE
    assert source_type_for_surface("file") == NATIVE_FILE_SOURCE
    assert NATIVE_FILE_SOURCE not in {"file", NATIVE_DOCUMENT_SOURCE}
    assert {NATIVE_DOCUMENT_SOURCE, NATIVE_FILE_SOURCE} <= set(SOURCE_TYPES)
    with pytest.raises(ValueError, match="unsupported native derived surface"):
        source_type_for_surface("table")


def test_both_surfaces_select_one_delivery_and_direct_grep_stays_readable():
    # `selected_delivery` names the delivery mechanism, not the surface. After
    # parity there is exactly one mechanism; the pre-parity File delivery stays
    # defined so historical rows can still be read rather than rewritten.
    assert SELECTED_DELIVERY == "native-searchable-derived-v1"
    assert DIRECT_GREP_DELIVERY == "native-direct-pg-grep-v1"
    assert SELECTED_DELIVERY != DIRECT_GREP_DELIVERY


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
