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

The module-level ``git = GitService()`` in activity.py (and ``GitService()`` in the
MCP handlers) mkdir ``git_storage_path`` at construction — the prod default
``/data/vaults`` is unwritable in CI — so the fixture redirects it to ``tmp_path``
BEFORE importing the modules (the same pattern as test_activity_routes_unit.py).
"""

import threading

import pytest


class _User:
    user_id = "00000000-0000-0000-0000-000000000000"
    username = "t"


def _thread_recorder(ret):
    """A stand-in git call that records the thread it ran on and returns ``ret``.
    Accepts any args (as an instance attr it gets none; as a class attr it gets
    ``self``) so it works whether we patch the instance or the class."""
    box: dict = {}

    def _rec(*args, **kwargs):
        box["tid"] = threading.get_ident()
        return ret

    return _rec, box


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
    loop_tid = threading.get_ident()
    rec, box = _thread_recorder([])
    monkeypatch.setattr(activity, "check_vault_access", _anoop)
    monkeypatch.setattr(activity, "_resolve_activity_authors", _aidentity)
    monkeypatch.setattr(activity.git, "vault_log", rec)

    await activity.vault_activity(
        vault="v", collection=None, author=None, since=None,
        limit=50, user=_User(),
    )

    assert box.get("tid"), "git.vault_log was never called"
    assert box["tid"] != loop_tid, (
        "REST /activity ran git.vault_log ON the event loop (not offloaded)"
    )


async def test_rest_document_diff_offloads_file_diff_and_keeps_envelope(mods, monkeypatch):
    activity, _server, _gs = mods
    loop_tid = threading.get_ident()
    rec, box = _thread_recorder({"diff": "…", "type": "modified"})

    class _Conn:
        async def fetchrow(self, *a, **k):
            return {"id": "vault-id"}

    class _AcquireCtx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _AcquireCtx()

    async def _get_pool():
        return _Pool()

    class _DocRepo:
        def __init__(self, pool):
            pass

        async def find_by_ref_with_conn(self, conn, vid, doc_id):
            return {"path": "docs/p.md"}

    monkeypatch.setattr(activity, "check_vault_access", _anoop)
    monkeypatch.setattr(activity, "get_pool", _get_pool)
    monkeypatch.setattr(activity, "DocumentRepository", _DocRepo)
    monkeypatch.setattr(activity.git, "file_diff", rec)

    out = await activity.document_diff(
        vault="v", doc_id="docs/p.md", commit="abc1234", user=_User(),
    )

    assert box.get("tid"), "git.file_diff was never called"
    assert box["tid"] != loop_tid, (
        "REST /diff ran git.file_diff ON the event loop (not offloaded)"
    )
    # the rebase must preserve the response envelope (main added {"kind": ...})
    assert out["kind"] == "document_diff"
    assert out["diff"] == "…"


# ── MCP handlers ─────────────────────────────────────────────────


async def test_mcp_activity_offloads_vault_log(mods, monkeypatch):
    _activity, server, GitService = mods
    loop_tid = threading.get_ident()
    rec, box = _thread_recorder([])
    monkeypatch.setattr(server, "check_vault_access", _anoop)
    monkeypatch.setattr(GitService, "vault_log", rec)

    await server._handle_activity({"vault": "v", "limit": 5}, "uid", _User())

    assert box.get("tid"), "git.vault_log was never called"
    assert box["tid"] != loop_tid, (
        "MCP akb_activity ran git.vault_log ON the event loop (not offloaded)"
    )


async def test_mcp_diff_offloads_file_diff(mods, monkeypatch):
    _activity, server, GitService = mods
    loop_tid = threading.get_ident()
    rec, box = _thread_recorder({"diff": "…"})

    async def _find(vault, path):
        return {"path": "docs/p.md"}

    monkeypatch.setattr(server, "split_uri", lambda uri, expected_type=None: ("v", "docs/p.md"))
    monkeypatch.setattr(server, "check_vault_access", _anoop)
    monkeypatch.setattr(server, "_find_doc", _find)
    monkeypatch.setattr(GitService, "file_diff", rec)

    await server._handle_diff({"uri": "akb://v/doc/docs/p.md", "commit": "abc1234"}, "uid", _User())

    assert box.get("tid"), "git.file_diff was never called"
    assert box["tid"] != loop_tid, (
        "MCP akb_diff ran git.file_diff ON the event loop (not offloaded)"
    )


async def test_mcp_versioned_get_offloads_read_file(mods, monkeypatch):
    _activity, server, GitService = mods
    loop_tid = threading.get_ident()
    rec, box = _thread_recorder("body content")

    async def _find(vault, path):
        return {"path": "docs/p.md", "title": "P"}

    monkeypatch.setattr(server, "split_uri", lambda uri, expected_type=None: ("v", "docs/p.md"))
    monkeypatch.setattr(server, "check_vault_access", _anoop)
    monkeypatch.setattr(server, "_find_doc", _find)
    monkeypatch.setattr(GitService, "read_file", rec)

    await server._handle_get(
        {"uri": "akb://v/doc/docs/p.md", "version": "abc1234"}, "uid", _User(),
    )

    assert box.get("tid"), "git.read_file was never called"
    assert box["tid"] != loop_tid, (
        "MCP versioned akb_get ran git.read_file ON the event loop (not offloaded)"
    )
