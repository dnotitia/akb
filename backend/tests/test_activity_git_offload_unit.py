"""Regression: every request-path git read in the activity/diff/get family must
run OFF the single asyncio event loop (via ``asyncio.to_thread``).

``git.vault_log`` spawns ``git rev-list`` + one ``git diff`` subprocess per
commit, ``git.file_diff`` spawns ``git diff``, and the versioned ``git.read_file``
spawns ``git cat-file`` — all synchronous subprocesses. Called inline on the
single event loop they starve it under concurrency → ``/livez`` probe timeout →
503 (2026-07-20 audit). Each site must ``await asyncio.to_thread(git.<call>, ...)``.

Rather than a timing/heartbeat assertion (flaky on loaded CI runners), each test
replaces the git call with a recorder that captures ``threading.get_ident()`` and
asserts it differs from the event-loop thread — a deterministic proof the call
ran on a worker thread. A revert that inlines any one site flips its recorded
thread to the loop thread and fails that test.

No DB / no real git: the routes' and handlers' other deps are monkeypatched.
Runs in ``pytest -k 'not _e2e'``.

The process-selected legacy revision backend owns ``GitService()``. Its
construction mkdirs ``git_storage_path`` — the prod default ``/data/vaults`` is
unwritable in CI — so the fixture redirects it to ``tmp_path`` before import.
"""

import threading
from unittest.mock import AsyncMock

import pytest


class _User:
    user_id = "00000000-0000-0000-0000-000000000000"
    username = "t"


def _offload_probe(ret):
    """Return a stand-in git call that records the thread it runs on (returning
    ``ret``), plus an ``assert_offloaded(why)`` that checks it ran on a worker
    thread rather than this — the event-loop — thread. Call this from inside the
    test coroutine so the captured ``loop_tid`` is the event-loop thread. The
    fake accepts any args, so it works whether patched onto an instance or a
    class (where it also receives ``self``)."""
    loop_tid = threading.get_ident()
    box: dict = {}

    def _rec(*args, **kwargs):
        box["tid"] = threading.get_ident()
        return ret

    def assert_offloaded(why):
        assert box.get("tid") is not None, "the git call was never made"
        assert box["tid"] != loop_tid, why

    return _rec, assert_offloaded


@pytest.fixture
def mods(monkeypatch, tmp_path):
    """Redirect the module-level GitService storage to tmp, THEN import the
    route + MCP handler modules (their construction mkdirs git_storage_path)."""
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.api.routes import activity
    from app.services.git_service import GitService
    from mcp_server import server

    return activity, server, GitService


async def _anoop(*a, **k):
    return {"role": "reader"}


async def _aidentity(entries):
    return entries


# ── REST routes ──────────────────────────────────────────────────


async def test_rest_vault_activity_offloads_vault_log(mods, monkeypatch):
    activity, _server, _gs = mods
    rec, assert_offloaded = _offload_probe([])
    monkeypatch.setattr(activity, "check_vault_access", _anoop)
    monkeypatch.setattr(activity, "_resolve_activity_authors", _aidentity)
    monkeypatch.setattr(activity.revision_backend._git, "vault_log", rec)

    await activity.vault_activity(
        vault="v", collection=None, author=None, since=None,
        limit=50, user=_User(),
    )

    assert_offloaded("REST /activity ran git.vault_log ON the event loop (not offloaded)")


async def test_rest_document_diff_offloads_file_diff_and_keeps_envelope(mods, monkeypatch):
    activity, _server, _gs = mods
    rec, assert_offloaded = _offload_probe({"diff": "…", "type": "modified"})

    monkeypatch.setattr(activity, "check_vault_access", _anoop)
    monkeypatch.setattr(
        activity.revision_backend,
        "_find_document",
        AsyncMock(return_value={"path": "docs/p.md"}),
    )
    monkeypatch.setattr(activity.revision_backend._git, "file_diff", rec)

    out = await activity.document_diff(
        vault="v", doc_id="docs/p.md", commit="abc1234", user=_User(),
    )

    assert_offloaded("REST /diff ran git.file_diff ON the event loop (not offloaded)")
    # the rebase must preserve the response envelope (main added {"kind": ...})
    assert out["kind"] == "document_diff"
    assert out["diff"] == "…"


# ── MCP handlers ─────────────────────────────────────────────────


async def test_mcp_activity_offloads_vault_log(mods, monkeypatch):
    _activity, server, _GitService = mods
    rec, assert_offloaded = _offload_probe([])
    monkeypatch.setattr(server, "check_vault_access", _anoop)
    monkeypatch.setattr(server.revision_backend._git, "vault_log", rec)

    await server._handle_activity({"vault": "v", "limit": 5}, "uid", _User())

    assert_offloaded("MCP akb_activity ran git.vault_log ON the event loop (not offloaded)")


async def test_mcp_diff_offloads_file_diff(mods, monkeypatch):
    _activity, server, _GitService = mods
    rec, assert_offloaded = _offload_probe({"diff": "…"})

    monkeypatch.setattr(server, "split_uri", lambda uri, expected_type=None: ("v", "docs/p.md"))
    monkeypatch.setattr(server, "check_vault_access", _anoop)
    monkeypatch.setattr(
        server.revision_backend,
        "_find_document",
        AsyncMock(return_value={"path": "docs/p.md"}),
    )
    monkeypatch.setattr(server.revision_backend._git, "file_diff", rec)

    await server._handle_diff({"uri": "akb://v/doc/docs/p.md", "commit": "abc1234"}, "uid", _User())

    assert_offloaded("MCP akb_diff ran git.file_diff ON the event loop (not offloaded)")


async def test_mcp_versioned_get_offloads_read_file(mods, monkeypatch):
    _activity, server, _GitService = mods
    rec, assert_offloaded = _offload_probe("body content")

    monkeypatch.setattr(server, "split_uri", lambda uri, expected_type=None: ("v", "docs/p.md"))
    monkeypatch.setattr(server, "check_vault_access", _anoop)
    monkeypatch.setattr(
        server.revision_backend,
        "_find_document",
        AsyncMock(return_value={"path": "docs/p.md", "title": "P"}),
    )
    monkeypatch.setattr(server.revision_backend._git, "read_file", rec)

    await server._handle_get(
        {"uri": "akb://v/doc/docs/p.md", "version": "abc1234"}, "uid", _User(),
    )

    assert_offloaded("MCP versioned akb_get ran git.read_file ON the event loop (not offloaded)")


async def test_mcp_versioned_get_preserves_missing_document_distinction(mods, monkeypatch):
    _activity, server, _GitService = mods
    monkeypatch.setattr(server, "split_uri", lambda uri, expected_type=None: ("v", "missing.md"))
    monkeypatch.setattr(server, "check_vault_access", _anoop)
    monkeypatch.setattr(
        server.revision_backend,
        "_find_document",
        AsyncMock(return_value=None),
    )

    result = await server._handle_get(
        {"uri": "akb://v/doc/missing.md", "version": "abc1234"},
        "uid",
        _User(),
    )

    assert result == {"error": "Document not found", "code": "not_found"}
