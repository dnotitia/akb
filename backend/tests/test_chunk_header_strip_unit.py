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

import pytest

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


def test_a_block_without_a_path_line_is_not_a_header():
    # Every builder that emits a body separator also emits `PATH:`, so a
    # `TITLE:`-looking opening followed by other key-shaped lines and a blank
    # line is prose, not a header — and prose is never removed.
    body = (
        "TITLE: how the indexer enriches a chunk\n"
        "SUMMARY: the block is written at index time\n"
        "TAGS: notes\n"
        "\n"
        "It must survive verbatim."
    )
    assert strip_chunk_metadata_header(body) == body


def test_a_summary_with_a_blank_line_inside_it_still_strips():
    header = build_doc_metadata_header(
        vault_name="project-akb",
        path="meta/agent-onboarding.md",
        title="Agent onboarding",
        summary=(
            "First paragraph of the summary.\n"
            "\n"
            "A second paragraph, after a blank line."
        ),
        tags=["topic:onboarding"],
        doc_type="guide",
    )
    assert strip_chunk_metadata_header(header + BODY) == BODY


@pytest.mark.parametrize("tags", [None, ["topic:meta"]])
@pytest.mark.parametrize("doc_type", [None, "guide"])
def test_every_combination_of_the_optional_keys_strips(tags, doc_type):
    header = build_doc_metadata_header(
        vault_name="project-akb",
        path="meta/welcome.md",
        title="Welcome",
        summary="A summary.\nSpanning two lines.",
        tags=tags,
        doc_type=doc_type,
    )
    assert strip_chunk_metadata_header(header + BODY) == BODY


def test_keys_out_of_builder_order_are_not_a_header():
    # The builders emit one order. A block that merely uses the same words
    # in another order was not written by them.
    body = (
        "TITLE: x\n"
        "PATH: v/p.md\n"
        "SUMMARY: this came after PATH, which no builder does\n"
        "TAGS: a\n"
        "\n"
        "Body that must survive."
    )
    assert strip_chunk_metadata_header(body) == body


def test_a_multiline_summary_run_still_needs_the_path_line():
    body = (
        "TITLE: x\n"
        "SUMMARY: first line of a long summary\n"
        "a continuation line\n"
        "another continuation line\n"
        "TYPE: guide\n"
        "\n"
        "Body that must survive."
    )
    assert strip_chunk_metadata_header(body) == body


def test_continuation_lines_are_only_allowed_after_summary():
    # Without a SUMMARY line there is no interpolated value that can carry
    # newlines, so free-form lines between TITLE and PATH are prose.
    body = (
        "TITLE: x\n"
        "a line that is not a key\n"
        "PATH: v/p.md\n"
        "\n"
        "Body that must survive."
    )
    assert strip_chunk_metadata_header(body) == body


def test_stripping_is_idempotent():
    header = build_doc_metadata_header(
        vault_name="project-akb",
        path="meta/agent-onboarding.md",
        title="Agent onboarding",
        summary="Line one of the summary.\nLine two of the summary.",
        tags=["topic:onboarding"],
        doc_type="guide",
    )
    once = strip_chunk_metadata_header(header + BODY)
    assert strip_chunk_metadata_header(once) == once == BODY


def test_empty_and_none_pass_through():
    assert strip_chunk_metadata_header(None) is None
    assert strip_chunk_metadata_header("") == ""
