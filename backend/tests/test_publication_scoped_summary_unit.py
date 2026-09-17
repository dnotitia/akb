"""A section-scoped link does not carry the whole document's summary.

`content` and the image manifest are narrowed by the section filter. The
summary was not: it is stored on the document, describes the whole of it, and
went out unchanged — so a link cut for one section handed the viewer prose
from sections it never received.

Three producers can write that stored value (the author, the create-time
derivation, the LLM metadata worker on an imported document) and nothing
records which one did. That is why the scoped path derives instead of trying
to tell them apart.
"""

import datetime
from unittest.mock import AsyncMock

import pytest

from app.models.document import DocumentPutRequest
from app.services import publication_service
from app.services.document_service import _build_frontmatter, derive_summary
from app.services.publication_service import _ResolvedDocumentBody, _scoped_summary

BODY = (
    "# Shared\n\nThe paragraph this link was cut for.\n\n"
    "# Private\n\nSENTINEL belongs to a section nobody published.\n"
)
STORED = "SENTINEL belongs to a section nobody published."


def _resolved(content: str, *, missing: bool = False, unavailable: bool = False):
    return _ResolvedDocumentBody(
        content=content,
        content_unavailable=unavailable,
        section_not_found=missing,
        asset_ids=frozenset(),
    )


# ── the extracted derivation, unchanged from the write path ──────────

def test_first_non_heading_paragraph_capped_at_200():
    assert derive_summary("# H\n\nThe lead paragraph.\n\nSecond.") == "The lead paragraph."
    assert len(derive_summary("x" * 500)) == 200


@pytest.mark.parametrize(
    "markdown",
    ["", "# Only\n## Headings\n", "| a | b |\n", "---\n"],
)
def test_nothing_to_summarise_is_none(markdown):
    assert derive_summary(markdown) is None


def test_a_fence_marker_is_skipped_but_its_contents_are_not():
    """Pins the behaviour the extraction inherited, rather than improving it.

    Only the line that STARTS with a fence is skipped, so the code inside one
    is still eligible to become the summary. That has been true since the
    derivation was written; changing it here would move create-time behaviour
    under cover of a publication fix, and every document already stores a
    summary produced by the old rule.
    """
    assert derive_summary("```py\ncode\n```\n") == "code"


def test_the_write_path_still_asks_the_same_question():
    """Extraction must not move the create-time behaviour."""
    req = DocumentPutRequest(
        vault="v", collection="c", title="t", content="# H\n\nDerived from the body.\n",
    )
    fm = _build_frontmatter(req, datetime.datetime.now(datetime.UTC))
    assert fm["summary"] == "Derived from the body."

    authored = DocumentPutRequest(
        vault="v", collection="c", title="t", content="# H\n\nDerived from the body.\n",
        summary="What the author wrote",
    )
    fm = _build_frontmatter(authored, datetime.datetime.now(datetime.UTC))
    assert fm["summary"] == "What the author wrote"


# ── what a scoped link is allowed to carry ───────────────────────────

def test_without_a_filter_the_stored_summary_is_untouched():
    row = {"summary": STORED}
    assert _scoped_summary(row, None, _resolved(BODY)) == STORED
    assert _scoped_summary({"summary": None}, None, _resolved(BODY)) is None


def test_a_scoped_link_summarises_only_its_own_slice():
    row = {"summary": STORED}
    slice_ = "# Shared\n\nThe paragraph this link was cut for.\n"
    got = _scoped_summary(row, "Shared", _resolved(slice_))
    assert got == "The paragraph this link was cut for."
    assert "SENTINEL" not in got


def test_a_section_that_is_gone_summarises_nothing():
    assert _scoped_summary({"summary": STORED}, "Gone", _resolved("", missing=True)) is None


def test_an_unreadable_document_summarises_nothing():
    row = {"summary": STORED}
    body = _resolved("*Document content is no longer available.*", unavailable=True)
    assert _scoped_summary(row, "Shared", body) is None


def test_a_slice_with_no_prose_summarises_nothing():
    assert _scoped_summary({"summary": STORED}, "Shared", _resolved("# Shared\n")) is None


# ── the wiring, not just the helper ──────────────────────────────────

@pytest.mark.asyncio
async def test_the_response_carries_the_scoped_summary(monkeypatch):
    """Prove it through `resolve_document_publication`, where the leak was."""
    row = {
        "_native_body": BODY,
        "title": "Doc title",
        "doc_type": "note",
        "summary": STORED,
        "domain": None,
        "created_by_name": "someone",
        "updated_at": datetime.datetime(2026, 9, 17, tzinfo=datetime.UTC),
        "tags": [],
    }
    monkeypatch.setattr(
        publication_service, "_find_published_document", AsyncMock(return_value=row),
    )

    scoped = await publication_service.resolve_document_publication(
        {"resource_type": publication_service.ResourceType.DOCUMENT, "section_filter": "Shared"},
    )
    assert scoped["summary"] == "The paragraph this link was cut for."
    assert "SENTINEL" not in scoped["summary"]
    assert "SENTINEL" not in scoped["content"]

    whole = await publication_service.resolve_document_publication(
        {"resource_type": publication_service.ResourceType.DOCUMENT, "section_filter": None},
    )
    # Nothing is withheld from a publication of the whole document.
    assert whole["summary"] == STORED


@pytest.mark.asyncio
async def test_the_response_withholds_a_summary_when_the_section_is_gone(monkeypatch):
    row = {
        "_native_body": BODY,
        "title": "Doc title",
        "doc_type": "note",
        "summary": STORED,
        "domain": None,
        "created_by_name": "someone",
        "updated_at": datetime.datetime(2026, 9, 17, tzinfo=datetime.UTC),
        "tags": [],
    }
    monkeypatch.setattr(
        publication_service, "_find_published_document", AsyncMock(return_value=row),
    )
    resolved = await publication_service.resolve_document_publication(
        {"resource_type": publication_service.ResourceType.DOCUMENT, "section_filter": "Gone"},
    )
    assert resolved["section_not_found"] is True
    assert resolved["content"] == ""
    assert resolved["summary"] is None
