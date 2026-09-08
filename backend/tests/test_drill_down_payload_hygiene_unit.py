"""`drill_down` returns chunk bodies, not the index-side scaffolding
that was written into them.

A stored chunk is `metadata header + "[<section_path>]\\n" + body`, and a
section longer than `MAX_CHUNK_SIZE` repeats the previous chunk's last
`OVERLAP` characters at the head of the next one. All three are
retrieval-side devices; on the read path they are duplication the caller
pays tokens for. These tests pin what is removed and — more importantly —
what is not: no key ever disappears from a section row, and the cleaned
bodies still concatenate back to the stored text.
"""

from __future__ import annotations

from app.services.index_service import chunk_markdown, build_doc_metadata_header
from app.services.search_service import (
    clean_section_rows,
    strip_chunk_context_line,
)


def _row(section_path, content, chunk_index, doc_id="doc-1"):
    return {
        "section_path": section_path,
        "content": content,
        "chunk_index": chunk_index,
        "doc_id": doc_id,
    }


# ── the context line ────────────────────────────────────────────────


def test_context_line_matching_section_path_is_removed():
    rows = [_row("# Guide > ## Section", "[# Guide > ## Section]\nBody text.", 0)]
    (section,) = clean_section_rows(rows)

    assert section["content"] == "Body text."
    # The value itself is not lost — it is the field it always was.
    assert section["section_path"] == "# Guide > ## Section"
    assert set(section) == {"section_path", "content", "chunk_index"}


def test_context_line_that_disagrees_with_section_path_is_kept():
    rows = [_row("# Guide > ## Section", "[# Other > ## Heading]\nBody text.", 0)]
    (section,) = clean_section_rows(rows)

    assert section["content"] == "[# Other > ## Heading]\nBody text."


def test_bracketed_body_line_is_not_mistaken_for_the_context_line():
    rows = [_row("# Guide", "[# Guide] and then more prose on the same line.", 0)]
    (section,) = clean_section_rows(rows)

    assert section["content"] == "[# Guide] and then more prose on the same line."


def test_context_strip_is_a_no_op_without_a_section_path():
    assert strip_chunk_context_line("[x]\nbody", None) == "[x]\nbody"
    assert strip_chunk_context_line("[x]\nbody", "") == "[x]\nbody"
    assert strip_chunk_context_line(None, "# A") is None


def test_header_and_context_line_are_both_removed_from_a_real_chunk():
    header = build_doc_metadata_header(
        vault_name="project-akb",
        path="product/family/architecture.md",
        title="Family architecture",
        summary="How the five repositories fit together.",
        tags=["topic:architecture"],
        doc_type="reference",
    )
    body = "The control plane provisions one AKB instance per tenant."
    chunks = chunk_markdown(f"# Family architecture\n\n{body}\n", header)
    rows = [
        _row(c.section_path, c.content, c.chunk_index) for c in chunks
    ]

    (section,) = clean_section_rows(rows)
    assert section["content"] == body
    assert section["section_path"] == "# Family architecture"
