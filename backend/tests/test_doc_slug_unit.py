"""Unit tests for the document slug normalizer.

Covers the hardened `slugify` (docs/designs/doc-identity-slug/00-overview.md):
case/whitespace normalization, dangling-hyphen trim, truncation, and the
empty/symbol-only → "untitled" fallback. The collision-suffix logic
(`{slug}-{shortid}` only when the base path is taken) lives in
`document_service._put_locked` and is covered by the e2e suite, since it needs a
DB to detect the collision.

Imports from `app.util.text` only — no DB / heavy deps, runs as a pure unit test.
"""
from __future__ import annotations

from app.util.text import slugify


class TestSlugify:
    def test_basic(self):
        assert slugify("Hello World") == "hello-world"

    def test_case_and_whitespace_normalized(self):
        # Distinct human titles that normalize to the same base — the case the
        # old create flow wrongly rejected as a duplicate.
        assert slugify("API Guide") == "api-guide"
        assert slugify("Api  Guide") == "api-guide"
        assert slugify("api-guide") == "api-guide"

    def test_strips_punctuation(self):
        assert slugify("Payment API v2!! (Final)") == "payment-api-v2-final"

    def test_empty_falls_back_to_untitled(self):
        # Symbol-only / empty titles must never yield a bare ".md" dotfile path.
        assert slugify("") == "untitled"
        assert slugify("!!!") == "untitled"
        assert slugify("---") == "untitled"
        assert slugify("   ") == "untitled"

    def test_no_leading_or_trailing_hyphen(self):
        assert slugify("  -hello-  ") == "hello"
        assert slugify("!hello!") == "hello"

    def test_truncation_trims_dangling_hyphen(self):
        # 79 chars + a separator that would land a hyphen at the cut boundary.
        title = ("a" * 79) + " b"
        s = slugify(title)
        assert len(s) <= 80
        assert not s.endswith("-")
        assert not s.startswith("-")

    def test_collision_suffix_shape(self):
        # The service appends `-{shortid}` only on collision; verify the shape
        # that pairing produces (base slug + 8-hex id) stays path-safe.
        base = slugify("Meeting Notes")
        assert base == "meeting-notes"
        assert f"{base}-3f9a2c1b" == "meeting-notes-3f9a2c1b"
