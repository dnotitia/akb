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


# ── Wikilink extraction ([[target]] / [[target|alias]]) ──────────────
#
# Regression: an Obsidian wikilink with an alias used to leak the alias's
# first word onto the target — `[[akb://…/x.md|PWC Long Title]]` produced
# the target `akb://…/x.md|PWC` (the greedy bare-URI scan didn't stop at
# `|`). That target matched no document node, so the graph drew no edge and
# the relations panel rendered a `…%7CPWC` broken link.

def test_wikilink_akb_uri_with_alias_strips_alias():
    content = (
        "## References\n"
        "- [[akb://v/coll/decisions/doc/x.md|PWC Query Performance Optimization]]"
    )
    out = extract_markdown_links(content)
    assert out == ["akb://v/coll/decisions/doc/x.md"]
    # The alias (or any fragment of it) must never appear in a target.
    assert all("|" not in t and "PWC" not in t for t in out)


def test_wikilink_path_with_alias_strips_alias():
    out = extract_markdown_links("see [[decisions/x.md|Some Decision]] here")
    assert out == ["decisions/x.md"]


def test_wikilink_without_alias():
    out = extract_markdown_links(
        "[[akb://v/coll/notes/doc/a.md]] and [[guides/b.md]]"
    )
    assert "akb://v/coll/notes/doc/a.md" in out
    assert "guides/b.md" in out
    assert all("[" not in t and "]" not in t for t in out)


def test_wikilink_alias_with_trailing_space_before_close():
    # The historical failure: matching stopped at the first space, leaving
    # `…x.md|PWC` (alias's first word glued on). Assert it does NOT happen.
    out = extract_markdown_links("[[akb://v/coll/d/doc/x.md|PWC Foo]]")
    assert "akb://v/coll/d/doc/x.md|PWC" not in out
    assert out == ["akb://v/coll/d/doc/x.md"]


def test_wikilink_dedups_with_bare_uri_scan():
    # The same akb:// inside a wikilink must not be double-counted by the
    # bare-URI fallback scan (which now also stops at `|`).
    out = extract_markdown_links("[[akb://v/coll/d/doc/x.md|Label]]")
    assert out == ["akb://v/coll/d/doc/x.md"]


def test_wikilink_inside_code_span_is_not_extracted():
    assert extract_markdown_links("`[[akb://v/coll/d/doc/x.md|L]]`") == []
