"""Unit tests for kg_service markdown link extraction.

Regression guard: example links inside code spans (fenced blocks + inline
`code`) must NOT be extracted as relations. Session reports quote URIs like
`akb://project-akb/...`, regex examples `akb://\\1/coll/\\2/doc/\\3`, and JSON
fragments `"incidents/a.md"` — none of which are real links, and all of which
previously became dangling `links_to` edges (ghost graph nodes).
"""
from __future__ import annotations

from app.services.kg_service import extract_markdown_links, strip_code_spans


def test_inline_code_uri_is_not_extracted():
    content = "후속 doc: `akb://project-akb/coll/designs/doc/x.md` 참고."
    assert extract_markdown_links(content) == []


def test_fenced_code_block_is_not_extracted():
    content = (
        "Example:\n\n```\n"
        'akb://V/coll/incidents/doc/a.md\n'
        "[link](path.md)\n"
        "```\n\nend."
    )
    assert extract_markdown_links(content) == []


def test_regex_example_in_code_is_not_extracted():
    content = "Migration 026 replacement is `akb://\\1/coll/\\2/doc/\\3`."
    assert extract_markdown_links(content) == []


def test_real_prose_links_are_still_extracted():
    content = (
        "See [the spec](./specs/api.md) and "
        "akb://v/coll/notes/doc/intro.md for context."
    )
    out = extract_markdown_links(content)
    assert "specs/api.md" in out
    assert "akb://v/coll/notes/doc/intro.md" in out


def test_strip_code_spans_removes_code_keeps_prose():
    stripped = strip_code_spans("`akb://x/coll/y/doc/z.md` keep-this")
    assert "akb://" not in stripped
    assert "keep-this" in stripped
