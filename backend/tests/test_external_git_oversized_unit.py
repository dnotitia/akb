"""Reconcile oversized-blob gate.

No network, no DB: ``ExternalGitService.reconcile`` is driven with a fake git
(controlling ``blob_exceeds_max``), fake repositories, and stubbed
``ensure_local_bare`` / ``_reindex_file`` / ``_delete_external_path``, so the
gate's skip / tombstone / cursor-advance behaviour is asserted in isolation.
The DB-heavy per-file machinery (``_reindex_file`` chunking + ``_delete_external_path``
cascade) is stubbed to recorders — this gate lives entirely in ``reconcile``.
"""

from __future__ import annotations

import pytest

from app.services import external_git_service as egs


def _make_service(monkeypatch, *, tree, sizes, local, mark_ok=True):
    """Wire a reconcile over the given upstream ``tree`` ({path: blob_sha}),
    ``sizes`` ({blob_sha: (bytes, oversized)}), and ``local`` external-blob docs.

    Returns ``(svc, rec)`` where ``rec`` captures the reindex / tombstone /
    mark_success side effects. ``last_synced_sha`` is None so ``ensure_local_bare``
    (stubbed) takes the non-``unchanged`` branch and the reconcile loop runs.
    """
    cfg = {
        "remote_url": "https://github.com/o/r.git",
        "remote_branch": "main",
        "auth_token": None,
        "poll_interval_secs": 300,
        "last_synced_sha": None,
    }
    rec: dict[str, list] = {"reindex": [], "deleted": [], "mark_success": []}

    class _Repo:
        def __init__(self, pool):
            pass

        async def get(self, vault_id):
            return cfg

        async def mark_success(self, vault_id, poll_interval_secs, new_sha=None,
                               *, validated_url=None, validated_token=None):
            rec["mark_success"].append(new_sha)
            return mark_ok

    class _DocRepo:
        def __init__(self, pool):
            pass

        async def list_external_blobs(self, vault_id):
            return local

    class _Git:
        def vault_exists(self, name):
            return False

        def ls_remote_head(self, url, branch, token):
            return "hint_sha"

        def ls_tree(self, name, sha):
            return tree

        def blob_exceeds_max(self, name, blob_sha):
            return sizes[blob_sha]

    async def _fake_pool():
        return object()

    monkeypatch.setattr(egs, "get_pool", _fake_pool)
    monkeypatch.setattr(egs, "VaultExternalGitRepository", _Repo)
    monkeypatch.setattr(egs, "DocumentRepository", _DocRepo)

    svc = egs.ExternalGitService(git=_Git())
    svc.ensure_local_bare = lambda *a, **k: ("fetched", "mat_sha")

    async def _fake_reindex(*, vault_id, vault_name, path, blob_sha, remote_url, tip_sha):
        rec["reindex"].append(path)

    async def _fake_delete(*, vault_id, vault_name, path, expected_blob=None):
        rec["deleted"].append(path)
        return "deleted"

    svc._reindex_file = _fake_reindex
    svc._delete_external_path = _fake_delete
    return svc, rec


@pytest.mark.asyncio
async def test_reconcile_oversized_new_blob_skipped_and_cursor_advances(monkeypatch):
    """A brand-new oversized path is skipped (never materialized/indexed) and the
    cursor STILL advances — an oversized blob is a deterministic skip, not an
    error, so the poll is never wedged on one big file."""
    svc, rec = _make_service(
        monkeypatch,
        tree={"big.md": "b" * 40},
        sizes={"b" * 40: (20_000_000, True)},
        local={},
    )
    result = await svc.reconcile("v-1", "m-1")

    assert result["status"] == "synced"  # NOT 'partial' — errors == 0
    assert result["skipped"] == 1
    assert result["added"] == 0
    assert result["errors"] == 0
    assert rec["reindex"] == []  # blob never read / indexed
    assert rec["deleted"] == []  # nothing to tombstone (brand-new path)
    assert rec["mark_success"] == ["mat_sha"]  # cursor advanced past the big blob


@pytest.mark.asyncio
async def test_reconcile_existing_doc_grown_oversized_is_tombstoned(monkeypatch):
    """A previously small + indexed doc whose upstream blob grew past the cap is
    TOMBSTONED, not left in place — a plain skip would keep the prior (smaller)
    content exposed. The cursor still advances."""
    svc, rec = _make_service(
        monkeypatch,
        tree={"big.md": "n" * 40},  # blob changed vs local → not the unchanged fast-path
        sizes={"n" * 40: (20_000_000, True)},
        local={"big.md": {"external_blob": "o" * 40}},
    )
    result = await svc.reconcile("v-1", "m-1")

    assert result["status"] == "synced"
    assert result["skipped"] == 1
    assert result["updated"] == 0
    assert result["errors"] == 0
    assert rec["deleted"] == ["big.md"]  # prior content tombstoned (not exposed)
    assert rec["reindex"] == []  # the oversized blob is never re-indexed
    assert rec["mark_success"] == ["mat_sha"]  # cursor still advances


@pytest.mark.asyncio
async def test_reconcile_normal_blob_is_indexed_no_regression(monkeypatch):
    """A normal-size blob is unaffected by the gate — indexed as before."""
    svc, rec = _make_service(
        monkeypatch,
        tree={"ok.md": "s" * 40},
        sizes={"s" * 40: (42, False)},
        local={},
    )
    result = await svc.reconcile("v-1", "m-1")

    assert result["status"] == "synced"
    assert result["added"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == 0
    assert rec["reindex"] == ["ok.md"]  # indexed normally
    assert rec["deleted"] == []


@pytest.mark.asyncio
async def test_reconcile_oversized_tombstone_conflict_holds_cursor(monkeypatch):
    """A tombstone CAS conflict (a concurrent reconcile moved the blob after our
    snapshot) is RETRYABLE, not a clean skip: reconcile raises (partial) and the
    cursor is HELD, so the next poll reprocesses rather than advancing over stale
    content."""
    svc, rec = _make_service(
        monkeypatch,
        tree={"big.md": "n" * 40},
        sizes={"n" * 40: (20_000_000, True)},
        local={"big.md": {"external_blob": "o" * 40}},
    )

    async def _conflict_delete(*, vault_id, vault_name, path, expected_blob=None):
        rec["deleted"].append(path)
        return "conflict"

    svc._delete_external_path = _conflict_delete

    with pytest.raises(RuntimeError):
        await svc.reconcile("v-1", "m-1")
    assert rec["mark_success"] == []  # cursor NOT advanced over a CAS conflict


@pytest.mark.asyncio
async def test_reconcile_delete_conflict_holds_cursor(monkeypatch):
    """The upstream-deletion path holds the cursor on a CAS conflict too: a path
    a concurrent reconcile re-indexed after our snapshot is NOT gone from truth,
    so counting a delete + advancing would drop a live document."""
    svc, rec = _make_service(
        monkeypatch,
        tree={},  # upstream is empty → local doc hits the deletion loop
        sizes={},
        local={"gone.md": {"external_blob": "o" * 40}},
    )

    async def _conflict_delete(*, vault_id, vault_name, path, expected_blob=None):
        rec["deleted"].append(path)
        return "conflict"

    svc._delete_external_path = _conflict_delete

    with pytest.raises(RuntimeError):
        await svc.reconcile("v-1", "m-1")
    assert rec["mark_success"] == []  # cursor held; next poll reprocesses


@pytest.mark.asyncio
async def test_reconcile_failed_tombstone_holds_cursor(monkeypatch):
    """If the oversized tombstone itself fails, it is counted as an error (cursor
    HELD via the reconcile RuntimeError) — the stale prior content is never left
    behind on a silent failure."""
    svc, rec = _make_service(
        monkeypatch,
        tree={"big.md": "n" * 40},
        sizes={"n" * 40: (20_000_000, True)},
        local={"big.md": {"external_blob": "o" * 40}},
    )

    async def _boom_delete(*, vault_id, vault_name, path, expected_blob=None):
        raise RuntimeError("db down")

    svc._delete_external_path = _boom_delete

    # errors > 0 → reconcile raises to hold the cursor (partial), never advances.
    with pytest.raises(RuntimeError):
        await svc.reconcile("v-1", "m-1")
    assert rec["mark_success"] == []  # cursor not advanced while the tombstone failed
