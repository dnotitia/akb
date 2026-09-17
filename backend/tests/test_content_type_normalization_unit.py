"""Declared media types converge on one form at the upload boundary.

`mime_type` arrives as a free-form query parameter and is stored verbatim, so
the same bytes could be declared in several spellings of the same type. Every
set that classifies a type downstream is keyed on the bare `type/subtype`, so
the spellings do not classify alike — the stored value decides, and it was
never reduced to a single form on the way in.

These tests pin the reduction and the fallback.
"""

from __future__ import annotations

import pytest

from app.util.text import GENERIC_CONTENT_TYPE, normalize_content_type


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("image/png", "image/png"),
        ("IMAGE/PNG", "image/png"),
        ("  image/png  ", "image/png"),
        # Parameters are dropped: they are not read downstream, and carrying
        # them means the value no longer matches the sets that classify it.
        ("text/html; charset=utf-8", "text/html"),
        ("text/html;charset=utf-8", "text/html"),
        ("image/svg+xml; x=1", "image/svg+xml"),
        ("application/vnd.oasis.opendocument.text", "application/vnd.oasis.opendocument.text"),
        ("application/x-hwp", "application/x-hwp"),
    ],
)
def test_declared_types_reduce_to_one_form(declared: str, expected: str) -> None:
    assert normalize_content_type(declared) == expected


@pytest.mark.parametrize(
    "declared",
    [
        None,
        "",
        "   ",
        ";",
        "not-a-media-type",
        "image/",
        "/png",
        "image/png/extra",
        "image png",
        "../../etc/passwd",
    ],
)
def test_values_that_are_not_media_types_fall_back(declared) -> None:
    """Falling back keeps a non-type from travelling onward as itself."""
    assert normalize_content_type(declared) == GENERIC_CONTENT_TYPE


def test_normalization_is_idempotent() -> None:
    for value in ("image/png", "text/html; charset=utf-8", "nonsense"):
        once = normalize_content_type(value)
        assert normalize_content_type(once) == once
