"""Serialization contracts for activity, recent, history, and diff routes."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import base64
import json

import pytest
from fastapi import Response

from app.exceptions import AKBError
from app.services.recent_cursor import decode_cursor, encode_cursor

from app.models.activity import (
    AkbActivityEnvelope,
    AkbDocumentDiffEnvelope,
    AkbDocumentHistoryEnvelope,
    AkbRecentChangesEnvelope,
)


@pytest.fixture
def routes(monkeypatch, tmp_path):
    """Import after redirecting the selected legacy backend's Git storage."""
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.api.routes import activity

    return activity


@pytest.mark.asyncio
async def test_activity_adds_kind_and_preserves_unresolved_author_absence(monkeypatch, routes):
    entry = {
        "hash": "abc123",
        "subject": "[update] notes/a.md",
        "author": "external-author",
        "date": "2026-07-21T00:00:00+00:00",
        "action": "update",
        "summary": "changed",
        "agent": "external-author",
        "files": [{"path": "notes/a.md", "change": "modified"}],
    }
    monkeypatch.setattr(routes, "check_vault_access", AsyncMock())
    monkeypatch.setattr(
        routes.revision_backend, "vault_activity", AsyncMock(return_value=[entry]),
    )
    monkeypatch.setattr(routes, "_resolve_activity_authors", AsyncMock(return_value=[entry]))

    out = await routes.vault_activity(
        "v", collection=None, author=None, since=None, limit=20, user=MagicMock(),
    )

    assert out == {"kind": "activity", "vault": "v", "total": 1, "activity": [entry]}
    dumped = AkbActivityEnvelope.model_validate(out).model_dump(exclude_unset=True)
    assert "author_name" not in dumped["activity"][0]


@pytest.mark.asyncio
async def test_recent_adds_kind_and_preserves_explicit_nulls(monkeypatch, routes):
    changes = [{
        "doc_id": "d-nullable",
        "vault": "v",
        "path": "notes/nullable.md",
        "title": "Nullable",
        "type": "note",
        "commit": None,
        "changed_at": None,
    }]
    monkeypatch.setattr(
        routes.revision_backend, "recent_changes", AsyncMock(return_value=changes),
    )

    out = await routes.recent_changes(vault=None, limit=20, user=MagicMock(user_id="u"))

    assert out["kind"] == "recent_changes"
    assert out["changes"][0]["commit"] is None
    assert out["changes"][0]["changed_at"] is None
    dumped = AkbRecentChangesEnvelope.model_validate(out).model_dump(exclude_unset=True)
    assert dumped["changes"][0]["commit"] is None
    assert dumped["changes"][0]["changed_at"] is None


@pytest.mark.asyncio
async def test_watching_pages_emit_scope_and_private_cache_header(monkeypatch, routes):
    from app.api.routes import notifications
    monkeypatch.setattr(notifications, "human_user", AsyncMock())
    changes = [{"resource_id": f"00000000-0000-0000-0000-{i:012d}",
                "changed_at": "2026-09-01T00:00:00+00:00"} for i in [2, 1]]
    backend = AsyncMock(return_value=changes)
    monkeypatch.setattr(routes.revision_backend, "recent_changes", backend)
    response = Response()
    page = await routes.recent_changes(vault=None, limit=1, user=MagicMock(user_id="u"),
                                      scope="watching", response=response)
    assert page["changes"] == changes[:1]
    assert page["scope"] == "watching"
    assert str(decode_cursor(page["next_cursor"], "watching", None)[1]) == changes[0]["resource_id"]
    assert response.headers["Cache-Control"] == "private, no-store"
    backend.assert_awaited_once_with("u", vault=None, limit=2, watching=True, before=None)


@pytest.mark.asyncio
async def test_watching_preserves_notification_session_and_feature_guards(monkeypatch, routes):
    from app.config import settings
    from app.services.auth_service import AuthenticatedUser
    user = AuthenticatedUser(user_id="u", username="u", email="u@example.invalid",
                             display_name=None, is_admin=False, auth_method="jwt")
    backend = AsyncMock()
    monkeypatch.setattr(routes.revision_backend, "recent_changes", backend)
    monkeypatch.setattr(settings, "notifications_enabled", False)
    with pytest.raises(AKBError) as disabled:
        await routes.recent_changes(vault=None, limit=20, user=user, scope="watching")
    assert disabled.value.code == "notifications_disabled"
    monkeypatch.setattr(settings, "notifications_enabled", True)
    user.auth_method = "pat"
    with pytest.raises(AKBError) as session:
        await routes.recent_changes(vault=None, limit=20, user=user, scope="watching")
    assert session.value.code == "notifications_session_required"
    backend.assert_not_awaited()


@pytest.mark.parametrize("value", ["bad", "", "a" * 1025, *[
    base64.urlsafe_b64encode(json.dumps(v).encode()).decode() for v in [None, [], {},
        {"at": "2026-09-01", "id": "00000000-0000-0000-0000-000000000001", "scope": "watching", "vault": None}]
]])
def test_invalid_recent_cursor_is_client_error(value):
    with pytest.raises(AKBError) as error:
        decode_cursor(value, "watching", None)
    assert error.value.status_code == 400


def test_recent_cursor_cannot_cross_scope_or_vault():
    cursor = encode_cursor({"changed_at": "2026-09-01T00:00:00Z",
                            "resource_id": "00000000-0000-0000-0000-000000000001"}, "watching", "v")
    for scope, vault in [("all", "v"), ("watching", None)]:
        with pytest.raises(AKBError):
            decode_cursor(cursor, scope, vault)


@pytest.mark.parametrize("identifier", [123, {}, [], None])
def test_recent_cursor_rejects_non_string_identifier(identifier):
    cursor = base64.urlsafe_b64encode(json.dumps({
        "at": "2026-09-01T00:00:00Z", "id": identifier,
        "scope": "watching", "vault": None,
    }).encode()).decode()
    with pytest.raises(AKBError) as error:
        decode_cursor(cursor, "watching", None)
    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("git_result", "has_error"),
    [
        ({"file": "a.md", "commit": "good", "type": "modified", "diff": "@@"}, False),
        ({
            "file": "a.md", "commit": "bad", "type": "unknown", "diff": "",
            "error": "commit not found",
        }, True),
    ],
)
async def test_diff_adds_kind_and_preserves_optional_error(
    monkeypatch, routes, git_result, has_error,
):
    monkeypatch.setattr(routes, "check_vault_access", AsyncMock())
    monkeypatch.setattr(
        routes.revision_backend,
        "document_diff",
        AsyncMock(return_value=git_result),
    )

    out = await routes.document_diff("v", "a.md", commit=git_result["commit"], user=MagicMock())

    assert out["kind"] == "document_diff"
    dumped = AkbDocumentDiffEnvelope.model_validate(out).model_dump(exclude_unset=True)
    assert ("error" in dumped) is has_error
    assert dumped["type"] == git_result["type"]


@pytest.mark.asyncio
async def test_history_adds_kind_without_changing_entries(monkeypatch, routes):
    entry = {
        "hash": "abc123",
        "message": "update",
        "author": "writer",
        "date": datetime(2026, 7, 21, tzinfo=timezone.utc),
    }
    monkeypatch.setattr(routes, "check_vault_access", AsyncMock())
    monkeypatch.setattr(
        routes.revision_backend,
        "document_history",
        AsyncMock(return_value={"uri": "akb://v/doc/a.md", "history": [entry]}),
    )

    out = await routes.document_history("v", "a.md", limit=20, user=MagicMock())

    assert out == {
        "kind": "document_history",
        "uri": "akb://v/doc/a.md",
        "history": [entry],
    }
    AkbDocumentHistoryEnvelope.model_validate(out)
