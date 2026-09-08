"""`strip_chunk_metadata_header` must remove the whole indexing header,
including the case where `SUMMARY:` itself spans several lines.

The header is built by `build_doc_metadata_header`, which interpolates
the document summary verbatim. A summary carrying its own newlines
therefore emits continuation lines that are not `KEY:`-prefixed; the
original strip regex required every line between `TITLE:` and the blank
separator to be a recognised key, so it matched nothing at all and the
entire block (title, summary, tags, path, type) leaked into
`drill_down` / `search` / `grep` output.

These tests pin both directions: the header always goes, and content
that merely looks like a header never does.
"""

from __future__ import annotations

from app.services.index_service import (
    build_doc_metadata_header,
    build_file_metadata_header,
    build_table_chunk,
)
from app.services.search_service import strip_chunk_metadata_header


BODY = "[# Guide > ## Section]\nThe body an agent actually asked for.\n\nSecond paragraph."


def test_multiline_summary_header_is_stripped_completely():
    header = build_doc_metadata_header(
        vault_name="project-akb",
        path="meta/agent-onboarding.md",
        title="Agent onboarding",
        summary=(
            "How an agent bootstraps against this vault.\n"
            "Covers the cold-start sequence, the routing table, and\n"
            "the precedence rule between repo code and vault records."
        ),
        tags=["topic:onboarding", "audience:agent"],
        doc_type="guide",
    )
    stripped = strip_chunk_metadata_header(header + BODY)

    assert "TITLE:" not in stripped
    assert "SUMMARY:" not in stripped
    assert "PATH:" not in stripped
    assert "TYPE:" not in stripped
    assert "cold-start sequence" not in stripped
    assert stripped == BODY


def test_single_line_header_still_strips_unchanged():
    header = build_doc_metadata_header(
        vault_name="project-akb",
        path="meta/welcome.md",
        title="Welcome",
        summary="One line of summary.",
        tags=["topic:meta"],
        doc_type="guide",
    )
    assert strip_chunk_metadata_header(header + BODY) == BODY


def test_header_without_optional_keys_still_strips():
    # `summary`, `tags` and `doc_type` are all optional — TITLE + PATH is
    # the minimum block the builder can emit.
    header = build_doc_metadata_header(
        vault_name="project-akb",
        path="meta/welcome.md",
        title="Welcome",
    )
    assert strip_chunk_metadata_header(header + BODY) == BODY


def test_searchable_file_header_still_strips():
    header = build_file_metadata_header(
        vault_name="project-akb",
        path="raw/report.txt",
        uri="akb://project-akb/file/11111111-2222-3333-4444-555555555555",
        size_bytes=4096,
    )
    assert strip_chunk_metadata_header(header + BODY) == BODY


def test_pure_metadata_table_chunk_is_left_alone():
    # Table/file catalogue chunks are all metadata and carry no body
    # separator, so there is nothing to strip — stripping them would
    # empty the chunk.
    chunk = build_table_chunk(
        vault_name="project-akb",
        name="runbooks",
        description="Operational runbooks",
        columns=[{"name": "id", "type": "text"}],
    )
    assert strip_chunk_metadata_header(chunk.content) == chunk.content


def test_body_paragraph_that_merely_starts_with_title_is_not_stripped():
    body = (
        "TITLE: is the first key the indexer writes.\n"
        "The line above is prose about the header format, not a header.\n"
        "\n"
        "It must survive verbatim."
    )
    assert strip_chunk_metadata_header(body) == body


def test_prose_run_never_swallows_a_body_without_a_key_line():
    # A long run of non-blank lines under a `TITLE:`-looking first line
    # still needs a recognised KEY: line immediately before the blank
    # separator; otherwise nothing is removed.
    body = "TITLE: x\n" + "".join(f"line {i}\n" for i in range(30)) + "\nbody"
    assert strip_chunk_metadata_header(body) == body


def test_empty_and_none_pass_through():
    assert strip_chunk_metadata_header(None) is None
    assert strip_chunk_metadata_header("") == ""
