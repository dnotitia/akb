"""A section-scoped publication never widens to the document around it.

The two revision backends resolve a published body through different branches
of `_resolve_document_body`, and the e2e suite can only ever exercise the one
the deployment under test is configured for. That is how these two drifted
apart without anything going red: the PostgreSQL-authoritative branch blanked
the body when the heading stopped matching, the Git branch returned the whole
document, and the only check on the behaviour asserted the `section_not_found`
flag -- which both branches set.

So the assertions here run BOTH branches in one process and compare them, and
they assert the body and the image manifest rather than the flag.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import publication_service
from app.services.publication_service import _apply_section_filter

BODY = (
    "# Shared\n\nThe section this link was cut for.\n\n"
    "![shared](/api/assets/11111111-1111-4111-8111-111111111111)\n\n"
    "# Private\n\nSENTINEL must never reach a section-scoped viewer.\n\n"
    "![private](/api/assets/22222222-2222-4222-8222-222222222222)\n"
)
SHARED_ASSET = "11111111-1111-4111-8111-111111111111"
PRIVATE_ASSET = "22222222-2222-4222-8222-222222222222"


# ── the rule itself ──────────────────────────────────────────────────

def test_exact_heading_wins_and_narrows_to_its_subtree():
    body, missing = _apply_section_filter(BODY, "Shared")
    assert missing is False
    assert "SENTINEL" not in body
    assert "The section this link was cut for." in body


def test_a_heading_that_stopped_matching_yields_nothing():
    body, missing = _apply_section_filter(BODY, "Gone")
    assert missing is True
    # Not "the whole document with a flag beside it".
    assert body == ""


def test_no_filter_is_a_passthrough():
    assert _apply_section_filter(BODY, None) == (BODY, False)
    assert _apply_section_filter(BODY, "") == (BODY, False)


def test_an_empty_body_is_not_reported_as_a_missing_section():
    # An unreadable document is `content_unavailable`, a different signal.
    assert _apply_section_filter("", "Shared") == ("", False)


# ── both branches of the resolver, side by side ──────────────────────

def _native_row():
    return {"_native_body": BODY}


def _git_row():
    # The shape `_resolve_document_commit` reads: a pinned commit means the body
    # is fetched at that exact representation rather than at floating HEAD.
    return {
        "vault_name": "v",
        "path": "docs/guide.md",
        "current_commit": "c0ffee",
    }


@pytest.fixture
def git_service(monkeypatch):
    """Make the Git branch resolve `BODY` without a repository behind it."""
    service = SimpleNamespace(
        get_at_commit=AsyncMock(return_value=SimpleNamespace(content=BODY)),
        get=AsyncMock(return_value=SimpleNamespace(content=BODY, current_commit="c0ffee")),
    )
    monkeypatch.setattr(publication_service, "_get_doc_service", lambda: service)
    return service


@pytest.mark.asyncio
async def test_both_branches_serve_the_same_section(git_service):
    native = await publication_service._resolve_document_body(_native_row(), "Shared")
    git = await publication_service._resolve_document_body(_git_row(), "Shared")

    for arm, resolved in (("native", native), ("git", git)):
        assert resolved.section_not_found is False, arm
        assert "SENTINEL" not in resolved.content, arm
    assert native.content == git.content


@pytest.mark.asyncio
async def test_neither_branch_widens_when_the_section_is_gone(git_service):
    native = await publication_service._resolve_document_body(_native_row(), "Gone")
    git = await publication_service._resolve_document_body(_git_row(), "Gone")

    for arm, resolved in (("native", native), ("git", git)):
        assert resolved.section_not_found is True, arm
        assert resolved.content == "", arm
        assert "SENTINEL" not in resolved.content, arm
    assert native.content == git.content


@pytest.mark.asyncio
async def test_the_image_manifest_narrows_with_the_body(git_service):
    """`asset_ids` is derived from the resolved body, so an image outside the
    published section is not in the manifest the public asset route checks."""
    for row in (_native_row(), _git_row()):
        scoped = await publication_service._resolve_document_body(row, "Shared")
        ids = {str(i) for i in scoped.asset_ids}
        assert SHARED_ASSET in ids
        assert PRIVATE_ASSET not in ids

        gone = await publication_service._resolve_document_body(row, "Gone")
        assert gone.asset_ids == frozenset()
