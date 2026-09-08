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

import random

from app.services.index_service import (
    OVERLAP,
    build_doc_metadata_header,
    chunk_markdown,
)
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


# ── the indexing overlap window ─────────────────────────────────────


def test_exact_overlap_between_consecutive_chunks_is_removed():
    tail = "the last sentence of the previous chunk."
    rows = [
        _row("# A", "First chunk body. " + tail, 0),
        _row("# A", tail + " And the continuation.", 1),
    ]
    first, second = clean_section_rows(rows)

    assert first["content"] == "First chunk body. " + tail
    assert second["content"] == " And the continuation."
    # What was dropped is exactly what the previous chunk already ends with,
    # so a caller reading the section end to end loses nothing.
    dropped = rows[1]["content"][: len(rows[1]["content"]) - len(second["content"])]
    assert dropped == tail
    assert first["content"].endswith(dropped)


def test_chunks_without_an_overlap_are_left_alone():
    rows = [
        _row("# A", "First chunk body.", 0),
        _row("# A", "Wholly different continuation.", 1),
    ]
    first, second = clean_section_rows(rows)

    assert first["content"] == "First chunk body."
    assert second["content"] == "Wholly different continuation."


def test_overlap_strip_is_capped_at_the_writer_window():
    # A pathological pair whose common run is longer than OVERLAP: at most
    # OVERLAP characters may ever be removed, because that is all the
    # indexer can have duplicated.
    shared = "z" * (OVERLAP + 50)
    rows = [_row("# A", shared, 0), _row("# A", shared, 1)]
    _, second = clean_section_rows(rows)

    assert len(second["content"]) == len(shared) - OVERLAP


def test_non_consecutive_chunks_are_not_compared():
    tail = "shared trailing text"
    rows = [
        _row("# A", "body " + tail, 0),
        _row("# A", tail + " more", 5),
    ]
    _, second = clean_section_rows(rows)

    assert second["content"] == tail + " more"


def test_a_new_section_is_never_nibbled_at_the_boundary():
    # The previous section ends with "." and the next section's body
    # starts with "." — an exact 1-character match that is not an overlap.
    header = build_doc_metadata_header(
        vault_name="v", path="a/b.md", title="T", doc_type="note",
    )
    rows = [
        _row("# A", "End of the first section.", 0),
        _row("# B", header + "[# B]\n. Opening of the second section.", 1),
    ]
    _, second = clean_section_rows(rows)

    assert second["content"] == ". Opening of the second section."


def test_chunks_of_different_documents_are_not_compared():
    tail = "shared trailing text"
    rows = [
        _row("# A", "body " + tail, 0, doc_id="doc-1"),
        _row("# A", tail + " more", 1, doc_id="doc-2"),
    ]
    _, second = clean_section_rows(rows)

    assert second["content"] == tail + " more"


def test_cleaned_chunks_of_a_real_split_section_rebuild_the_source_text():
    random.seed(7)
    words = [
        "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
        "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho",
    ]

    def paragraph(index: int) -> str:
        filler = " ".join(
            f"{random.choice(words)}{random.randint(0, 9999)}" for _ in range(90)
        )
        return f"Paragraph {index}: {filler}"

    body = "\n\n".join(paragraph(i) for i in range(4))
    header = build_doc_metadata_header(
        vault_name="project-akb",
        path="direction/plan.md",
        title="Plan",
        summary="A plan long enough to be split.",
        tags=["topic:plan"],
        doc_type="note",
    )
    chunks = chunk_markdown(f"# Plan\n\n{body}\n", header)
    assert len(chunks) > 2, "fixture must actually be split by the chunker"

    rows = [_row(c.section_path, c.content, c.chunk_index) for c in chunks]
    sections = clean_section_rows(rows)

    assert "".join(s["content"] for s in sections) == body
    assert all("TITLE:" not in s["content"] for s in sections)
    assert all(s["section_path"] == "# Plan" for s in sections)
    stored_chars = sum(len(c.content) for c in chunks)
    returned_chars = sum(len(s["content"]) for s in sections)
    assert returned_chars < stored_chars
