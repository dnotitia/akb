"""MCP document help must describe recovery without authorizing stale writes."""

from mcp_server.help import HELP


def test_document_read_help_pairs_revision_with_original_base_and_draft():
    body = HELP["akb_get"]
    assert "`current_commit` is the revision for this read" in body
    assert "original base" in body
    assert "draft composed from that base" in body
    assert "`expected_commit`" in body
    assert "`base_commit`" in body


def test_update_help_requires_recomposing_against_a_complete_latest_read():
    body = HELP["akb_update"]
    assert "original base, local draft, and latest" in body
    assert "Call `akb_get(uri)` again for the complete latest document" in body
    assert "alone does not merge the old draft" in body
    assert "preserving concurrent" in body
    assert "reviewed composed body" in body
    assert "expected_commit=latest.current_commit" in body
    assert "Never replace only the token" in body
    assert "blind retry" in body
    assert "global URI-to-revision cache" in body


def test_exact_edit_help_requires_context_review_after_a_conflict():
    body = HELP["akb_edit"]
    assert "retain the original base and intended replacement" in body
    assert "read latest with `akb_get`" in body
    assert "review the surrounding context" in body
    assert "Never replace only the" in body
    assert "token to retry the same edit blindly" in body
    assert "fetch fresh content and retry" not in body


def test_update_tool_description_points_callers_to_reviewed_conflict_recovery():
    from mcp_server.tools import TOOLS

    description = next(tool.description for tool in TOOLS if tool.name == "akb_update")
    assert "retain the original base and draft" in description
    assert "read latest with akb_get" in description
    assert "preserving concurrent changes" in description
    assert "reviewed composed draft with that read's token" in description
    assert "never replace only the token or retry blindly" in description
