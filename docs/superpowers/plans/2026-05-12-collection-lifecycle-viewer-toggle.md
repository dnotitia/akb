# Collection Lifecycle, Refresh, and Viewer Toggle Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `akb_create_collection` / `akb_delete_collection` MCP tools and matching REST endpoints, expose them in the sidebar with create/delete dialogs, add manual refresh buttons that pair with post-mutation cache invalidation, and add a rendered ↔ raw toggle (with copy) to the document viewer.

**Architecture:** Three thin slices on top of existing infrastructure. Backend reuses the existing `collections` DB table (`init.sql:77`) and the per-vault git worktree pattern; new `CollectionService` orchestrates repo + git + DB-txn. Frontend reuses dialog patterns from `delete-vault-dialog.tsx`. Refresh is plumbed via a small `VaultRefreshContext` so mutation success handlers can invalidate. Viewer toggle is a single-file change in `document.tsx` with view mode in URL query.

**Tech Stack:** Python 3.11 / FastAPI / asyncpg, Postgres 16, GitPython (persistent linked worktrees), Anthropic MCP SDK (Streamable HTTP), React 19 + TypeScript + Vite, Tailwind v4, Vitest.

**Spec:** `docs/superpowers/specs/2026-05-12-collection-lifecycle-viewer-toggle-design.md`

---

## File Map

**Backend — repositories / services / git**
- Modify `backend/app/repositories/document_repo.py` — `CollectionRepository` gains `create_empty`, `delete_by_id`, `list_docs_under`, `list_files_under` helpers.
- Modify `backend/app/services/git_service.py` — `delete_paths_bulk(vault_name, file_paths, message)` method.
- Create `backend/app/services/collection_service.py` — `CollectionService.create` and `CollectionService.delete`.

**Backend — API + MCP**
- Modify `backend/app/api/routes/collections.py` — add `POST /collections/{vault}` and `DELETE /collections/{vault}/{path:path}` (router file already exists with only `/browse/{vault}`).
- Modify `backend/mcp_server/tools.py` — append `akb_create_collection` and `akb_delete_collection` schemas.
- Modify `backend/mcp_server/server.py` — register `_handle_create_collection` and `_handle_delete_collection` via `@_h(...)`.
- Modify `backend/mcp_server/help.py` — extend the verb table near `akb_delete` and add two entries to `_TOOL_DOCS`.

**Backend — tests**
- Create `backend/tests/test_collection_lifecycle_e2e.sh` — full lifecycle, recursive cascade, race, ACL.
- Modify `backend/tests/test_mcp_e2e.sh` — empty-is-valid invariant after deleting last doc in a collection.

**Frontend — collection UX**
- Modify `frontend/src/lib/api.ts` — `createCollection`, `deleteCollection`.
- Create `frontend/src/components/create-collection-dialog.tsx`.
- Create `frontend/src/components/delete-collection-dialog.tsx`.
- Modify `frontend/src/components/vault-explorer.tsx` — `+ Collection` header button, per-row delete on hover (writer+).
- Create `frontend/src/components/__tests__/create-collection-dialog.test.tsx`.
- Create `frontend/src/components/__tests__/delete-collection-dialog.test.tsx`.

**Frontend — refresh**
- Create `frontend/src/hooks/use-vaults.ts` — extracted from `vault-nav.tsx`, exposes `{vaults, refetch}`.
- Modify `frontend/src/hooks/use-vault-tree.ts` — expose `refetch`.
- Create `frontend/src/contexts/vault-refresh-context.tsx` — `{refetchTree, refetchVaults}` provider + hook.
- Modify `frontend/src/components/vault-nav.tsx` — consume `use-vaults`, add `⟳` header button, provide `refetchVaults`.
- Modify `frontend/src/components/vault-explorer.tsx` — add `⟳` button next to existing section headers; consume `refetchTree`.
- Modify `frontend/src/components/vault-shell.tsx` (or wherever VaultNav + VaultExplorer mount together) — install `<VaultRefreshContext.Provider>`.
- Modify mutation call sites (collection create/delete dialogs, `delete-vault-dialog.tsx`, document delete/edit on `document.tsx`, file upload/delete in `file-viewer.tsx`) — call refetch on success.

**Frontend — viewer toggle**
- Modify `frontend/src/pages/document.tsx` — read `view` from URL, render rendered or raw branch, copy button.
- Create `frontend/src/pages/__tests__/document-view-toggle.test.tsx`.

---

## Task 1 — `CollectionRepository`: new helpers

**Files:**
- Modify: `backend/app/repositories/document_repo.py`

**Context:** `CollectionRepository` already has `get_or_create`, `list_by_vault`, `increment_count`, `decrement_count` (`document_repo.py:286-332`). We add three new helpers used by the service layer. All take an optional `conn` for transaction participation, matching the existing pattern.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_collection_repo.py` (create if absent):

```python
"""Unit tests for new CollectionRepository helpers."""
from __future__ import annotations

import uuid
import pytest

from app.db.postgres import get_pool
from app.repositories.document_repo import CollectionRepository, VaultRepository


pytestmark = pytest.mark.asyncio


async def _make_vault(name: str) -> uuid.UUID:
    pool = await get_pool()
    vr = VaultRepository(pool)
    return await vr.create(name=name, owner_id=None)


async def test_create_empty_inserts_new_row():
    pool = await get_pool()
    repo = CollectionRepository(pool)
    vault_id = await _make_vault(f"test-create-empty-{uuid.uuid4().hex[:6]}")
    cid, created = await repo.create_empty(vault_id, "specs", summary="api specs")
    assert created is True
    assert isinstance(cid, uuid.UUID)


async def test_create_empty_is_idempotent():
    pool = await get_pool()
    repo = CollectionRepository(pool)
    vault_id = await _make_vault(f"test-idem-{uuid.uuid4().hex[:6]}")
    cid1, c1 = await repo.create_empty(vault_id, "specs")
    cid2, c2 = await repo.create_empty(vault_id, "specs")
    assert c1 is True and c2 is False
    assert cid1 == cid2


async def test_delete_by_id_removes_row():
    pool = await get_pool()
    repo = CollectionRepository(pool)
    vault_id = await _make_vault(f"test-del-{uuid.uuid4().hex[:6]}")
    cid, _ = await repo.create_empty(vault_id, "trash")
    await repo.delete_by_id(cid)
    rows = await repo.list_by_vault(vault_id)
    assert all(r["path"] != "trash" for r in rows)


async def test_list_docs_under_returns_only_prefix_matches():
    # Setup: put docs in vault directly via repo
    pool = await get_pool()
    repo = CollectionRepository(pool)
    vault_id = await _make_vault(f"test-list-{uuid.uuid4().hex[:6]}")
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO documents (id, vault_id, path, title) VALUES "
            "($1, $2, 'a/x.md', 'X'),"
            "($3, $2, 'a/y.md', 'Y'),"
            "($4, $2, 'b/z.md', 'Z')",
            uuid.uuid4(), vault_id, uuid.uuid4(), uuid.uuid4(),
        )
    docs = await repo.list_docs_under(vault_id, "a")
    paths = sorted(d["path"] for d in docs)
    assert paths == ["a/x.md", "a/y.md"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && pytest tests/test_collection_repo.py -v
```
Expected: `AttributeError: 'CollectionRepository' object has no attribute 'create_empty'` (or similar).

- [ ] **Step 3: Implement the three helpers**

Append to `backend/app/repositories/document_repo.py` inside `class CollectionRepository`:

```python
    async def create_empty(
        self,
        vault_id: uuid.UUID,
        path: str,
        summary: str | None = None,
        conn=None,
    ) -> tuple[uuid.UUID, bool]:
        """Insert a collection row idempotently.

        Returns (collection_id, created). `created` is False when the
        row already existed (`ON CONFLICT DO NOTHING`).
        """
        async def _do(c):
            cid = uuid.uuid4()
            name = path.rstrip("/").split("/")[-1]
            row = await c.fetchrow(
                "INSERT INTO collections (id, vault_id, path, name, summary, doc_count) "
                "VALUES ($1, $2, $3, $4, $5, 0) "
                "ON CONFLICT (vault_id, path) DO NOTHING "
                "RETURNING id",
                cid, vault_id, path, name, summary,
            )
            if row is not None:
                return row["id"], True
            existing = await c.fetchrow(
                "SELECT id FROM collections WHERE vault_id = $1 AND path = $2",
                vault_id, path,
            )
            return existing["id"], False

        if conn is not None:
            return await _do(conn)
        async with self.pool.acquire() as acq:
            return await _do(acq)

    async def delete_by_id(self, collection_id: uuid.UUID, conn=None) -> None:
        sql = "DELETE FROM collections WHERE id = $1"
        if conn is not None:
            await conn.execute(sql, collection_id)
            return
        async with self.pool.acquire() as acq:
            await acq.execute(sql, collection_id)

    async def list_docs_under(
        self,
        vault_id: uuid.UUID,
        path: str,
        conn=None,
    ) -> list[dict]:
        """Documents whose path starts with `path/`.

        Used by the cascade-delete path. Returns full document rows so
        the caller can drive per-doc chunk/relation cleanup.
        """
        prefix = path.rstrip("/") + "/"
        sql = (
            "SELECT id, path, metadata, collection_id "
            "FROM documents "
            "WHERE vault_id = $1 AND path LIKE $2"
        )
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        async def _do(c):
            rows = await c.fetch(sql, vault_id, like)
            return [dict(r) for r in rows]

        if conn is not None:
            return await _do(conn)
        async with self.pool.acquire() as acq:
            return await _do(acq)

    async def list_files_under(
        self,
        vault_id: uuid.UUID,
        path: str,
        conn=None,
    ) -> list[dict]:
        """Files whose collection is `path` or a descendant of it."""
        prefix = path.rstrip("/")
        sql = (
            "SELECT id, name, collection, s3_key "
            "FROM vault_files "
            "WHERE vault_id = $1 AND (collection = $2 OR collection LIKE $3)"
        )
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"

        async def _do(c):
            rows = await c.fetch(sql, vault_id, prefix, like)
            return [dict(r) for r in rows]

        if conn is not None:
            return await _do(conn)
        async with self.pool.acquire() as acq:
            return await _do(acq)
```

- [ ] **Step 4: Verify the `vault_files` table name**

Search for the actual files table name:
```bash
grep -E "CREATE TABLE (IF NOT EXISTS )?(vault_files|files)" backend/app/db/init.sql backend/app/db/migrations/*.py
```
If the table is called something other than `vault_files`, update `list_files_under` SQL accordingly. (This step is informational — adjust before re-running tests.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd backend && pytest tests/test_collection_repo.py -v
```
Expected: 4 passes.

- [ ] **Step 6: Commit**

```bash
git add backend/app/repositories/document_repo.py backend/tests/test_collection_repo.py
git commit -m "feat(repo): CollectionRepository helpers for create-empty, delete, list-under"
```

---

## Task 2 — `GitService.delete_paths_bulk`

**Files:**
- Modify: `backend/app/services/git_service.py`

**Context:** Existing `delete_file(vault_name, file_path, message)` removes a single path via the persistent worktree under a per-vault lock. The bulk variant runs `git rm` for many paths and a single commit. Idempotent on missing files — same contract as the singular method.

- [ ] **Step 1: Locate the existing `delete_file` for reference**

```bash
grep -n "def delete_file" backend/app/services/git_service.py
```
Read 30 lines around the match to mirror the lock + worktree + commit pattern.

- [ ] **Step 2: Write the failing test**

Append to `backend/tests/test_git_service.py` (create if absent):

```python
"""Unit tests for GitService bulk operations."""
from __future__ import annotations

import os
import tempfile
import pytest

from app.services.git_service import GitService


@pytest.fixture
def git_root(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("GIT_STORAGE_PATH", d)
        yield d


def test_delete_paths_bulk_removes_files_and_creates_one_commit(git_root):
    svc = GitService()
    svc.init_vault("v")
    svc.write_file(vault_name="v", file_path="a/x.md", content="x", message="add x")
    svc.write_file(vault_name="v", file_path="a/y.md", content="y", message="add y")
    svc.write_file(vault_name="v", file_path="b/z.md", content="z", message="add z")

    before = svc.vault_log(vault_name="v")
    svc.delete_paths_bulk(
        vault_name="v",
        file_paths=["a/x.md", "a/y.md"],
        message="[delete-collection] a\n\n2 docs, 0 files",
    )
    after = svc.vault_log(vault_name="v")
    assert len(after) == len(before) + 1  # exactly one new commit
    assert not svc.exists(vault_name="v", file_path="a/x.md")
    assert not svc.exists(vault_name="v", file_path="a/y.md")
    assert svc.exists(vault_name="v", file_path="b/z.md")  # untouched


def test_delete_paths_bulk_is_idempotent_on_missing(git_root):
    svc = GitService()
    svc.init_vault("v2")
    svc.write_file(vault_name="v2", file_path="a/x.md", content="x", message="add x")

    # File already gone; bulk call should not raise
    svc.delete_paths_bulk(
        vault_name="v2",
        file_paths=["a/x.md", "ghost.md"],  # one real, one missing
        message="cleanup",
    )
    assert not svc.exists(vault_name="v2", file_path="a/x.md")
```

(Adjust to the project's actual fixture / method names — check existing `tests/test_git_service.py` if present for the established style.)

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd backend && pytest tests/test_git_service.py::test_delete_paths_bulk_removes_files_and_creates_one_commit -v
```
Expected: `AttributeError: 'GitService' object has no attribute 'delete_paths_bulk'`.

- [ ] **Step 4: Implement the method**

Append to `GitService` in `backend/app/services/git_service.py`, mirroring the existing `delete_file`:

```python
    def delete_paths_bulk(
        self,
        *,
        vault_name: str,
        file_paths: list[str],
        message: str,
    ) -> None:
        """Remove many paths from the vault worktree under a single commit.

        Idempotent on missing files — paths that aren't in the index are
        skipped silently. The commit is omitted entirely if every path
        was already absent (no-op).
        """
        if not file_paths:
            return
        with self._vault_lock(vault_name):
            repo = self._worktree_repo(vault_name)
            staged_any = False
            for p in file_paths:
                abs_path = os.path.join(repo.working_tree_dir, p)
                if not os.path.exists(abs_path):
                    continue
                try:
                    repo.index.remove([p], working_tree=True)
                    staged_any = True
                except Exception as exc:  # noqa: BLE001 — mirror delete_file
                    logger.warning("bulk delete: %s skipped — %s", p, exc)
            if not staged_any:
                logger.info("bulk delete on %s: all paths already absent", vault_name)
                return
            self._commit(repo, message)
```

(Adjust to use the actual private helpers — `self._vault_lock`, `self._worktree_repo`, `self._commit` — names in the existing `GitService`. Inspect `delete_file` source first and match exactly.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd backend && pytest tests/test_git_service.py -v -k delete_paths_bulk
```
Expected: 2 passes.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/git_service.py backend/tests/test_git_service.py
git commit -m "feat(git): delete_paths_bulk for one-commit cascade deletes"
```

---

## Task 3 — `CollectionService.create`

**Files:**
- Create: `backend/app/services/collection_service.py`

**Context:** Stateless service like `DocumentService` (`document_service.py:1-30` for the constructor pattern). Holds references to repos lazily; entry point method takes raw args and returns a dict. Path validation is centralized here, not in the route.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_collection_service.py`:

```python
"""Unit tests for CollectionService.create."""
from __future__ import annotations

import uuid
import pytest

from app.services.collection_service import CollectionService, InvalidPathError


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def svc():
    return CollectionService()


async def test_create_normalizes_and_returns_created_true(svc, monkeypatch):
    vault = f"svc-create-{uuid.uuid4().hex[:6]}"
    # assume helper bootstraps a vault for the test user
    await _bootstrap_vault(vault)
    out = await svc.create(vault=vault, path="  /specs/  ", summary="s", agent_id="t")
    assert out["created"] is True
    assert out["collection"]["path"] == "specs"
    assert out["collection"]["doc_count"] == 0


async def test_create_idempotent(svc):
    vault = f"svc-idem-{uuid.uuid4().hex[:6]}"
    await _bootstrap_vault(vault)
    a = await svc.create(vault=vault, path="x", summary=None, agent_id="t")
    b = await svc.create(vault=vault, path="x", summary=None, agent_id="t")
    assert a["created"] is True
    assert b["created"] is False


@pytest.mark.parametrize("bad", ["", "   ", "/", "../etc", "a/../b", "a/./b", "x\x00y"])
async def test_create_rejects_invalid_path(svc, bad):
    vault = f"svc-bad-{uuid.uuid4().hex[:6]}"
    await _bootstrap_vault(vault)
    with pytest.raises(InvalidPathError):
        await svc.create(vault=vault, path=bad, summary=None, agent_id="t")
```

(`_bootstrap_vault` — reuse whatever helper the existing service tests use to create a vault for the current test user. If none exists in this project, the e2e shell tests cover the integration anyway; in that case mark these tests `@pytest.mark.skip` and rely on the e2e for service coverage. Recommended: copy the bootstrap pattern from `test_document_service.py` if it exists.)

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && pytest tests/test_collection_service.py::test_create_normalizes_and_returns_created_true -v
```
Expected: `ImportError: cannot import name 'CollectionService' from 'app.services.collection_service'`.

- [ ] **Step 3: Create the service file**

Create `backend/app/services/collection_service.py`:

```python
"""Collection lifecycle: create empty, delete (empty or recursive cascade).

Reuses existing infrastructure:
  - CollectionRepository for the metadata row
  - GitService.delete_paths_bulk for the single-commit cascade
  - DocumentService primitives (chunk/relation cleanup) for per-doc work
  - PG transactions for atomic post-git cleanup
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.db.postgres import get_pool
from app.repositories.document_repo import (
    CollectionRepository, DocumentRepository, VaultRepository,
)
from app.services.events_publisher import emit_event
from app.services.git_service import GitService
from app.services.kg_service import delete_document_relations
from app.services.index_service import delete_document_chunks
from app.utils.errors import NotFoundError, PermissionError as AccessError

logger = logging.getLogger(__name__)


class InvalidPathError(ValueError):
    """Raised when a collection path fails validation."""


class CollectionNotEmptyError(Exception):
    """Raised when delete is called on a non-empty collection without recursive=True."""
    def __init__(self, doc_count: int, file_count: int):
        super().__init__(f"Collection has {doc_count} documents and {file_count} files")
        self.doc_count = doc_count
        self.file_count = file_count


_MAX_PATH_BYTES = 1024


def _normalize_path(path: str) -> str:
    if not isinstance(path, str):
        raise InvalidPathError("path must be a string")
    s = path.strip().strip("/")
    if not s:
        raise InvalidPathError("path is empty")
    if len(s.encode("utf-8")) > _MAX_PATH_BYTES:
        raise InvalidPathError("path is too long")
    for seg in s.split("/"):
        if seg in ("", ".", ".."):
            raise InvalidPathError(f"invalid path segment: {seg!r}")
        if any(ord(ch) < 32 for ch in seg):
            raise InvalidPathError("control characters not allowed")
    return s


class CollectionService:
    def __init__(self):
        self.git = GitService()

    async def _repos(self):
        pool = await get_pool()
        return (
            VaultRepository(pool),
            DocumentRepository(pool),
            CollectionRepository(pool),
        )

    async def create(
        self,
        *,
        vault: str,
        path: str,
        summary: str | None,
        agent_id: str | None,
    ) -> dict:
        norm = _normalize_path(path)
        vault_repo, _, coll_repo = await self._repos()
        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        cid, created = await coll_repo.create_empty(vault_id, norm, summary=summary)

        pool = await get_pool()
        async with pool.acquire() as conn:
            await emit_event(
                conn, "collection.create",
                vault_id=vault_id, ref_type="collection", ref_id=norm,
                actor_id=agent_id,
                payload={"vault": vault, "path": norm, "created": created},
            )

        return {
            "ok": True,
            "created": created,
            "collection": {
                "path": norm,
                "name": norm.split("/")[-1],
                "summary": summary,
                "doc_count": 0,
            },
        }
```

(The `delete` method comes in Task 4. We're staying small per task.)

- [ ] **Step 4: Run create tests**

```bash
cd backend && pytest tests/test_collection_service.py -v -k create
```
Expected: all create tests pass; delete tests still failing (the method doesn't exist yet — that's OK).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/collection_service.py backend/tests/test_collection_service.py
git commit -m "feat(service): CollectionService.create with path normalization"
```

---

## Task 4 — `CollectionService.delete` (empty + recursive cascade)

**Files:**
- Modify: `backend/app/services/collection_service.py`

**Context:** Two paths through one method, controlled by `recursive`. Empty path skips git and only removes the row. Cascade enumerates docs+files via the new repo helpers, calls `git.delete_paths_bulk` first (outside PG txn), then runs a single PG transaction to clean chunks/relations/rows + emit event. Race safety via `SELECT ... FOR UPDATE` on the collection row.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_collection_service.py`:

```python
async def test_delete_empty(svc):
    vault = f"svc-del-empty-{uuid.uuid4().hex[:6]}"
    await _bootstrap_vault(vault)
    await svc.create(vault=vault, path="x", summary=None, agent_id="t")
    out = await svc.delete(vault=vault, path="x", recursive=False, agent_id="t")
    assert out["deleted_docs"] == 0 and out["deleted_files"] == 0
    # cannot delete again — gone
    with pytest.raises(NotFoundError):
        await svc.delete(vault=vault, path="x", recursive=False, agent_id="t")


async def test_delete_non_empty_without_recursive_raises(svc):
    vault = f"svc-del-conflict-{uuid.uuid4().hex[:6]}"
    await _bootstrap_vault(vault)
    await svc.create(vault=vault, path="x", summary=None, agent_id="t")
    await _put_doc(vault, "x/a.md", body="hello")
    with pytest.raises(CollectionNotEmptyError) as ei:
        await svc.delete(vault=vault, path="x", recursive=False, agent_id="t")
    assert ei.value.doc_count >= 1


async def test_delete_cascade_removes_docs_files_row(svc):
    vault = f"svc-del-cascade-{uuid.uuid4().hex[:6]}"
    await _bootstrap_vault(vault)
    await svc.create(vault=vault, path="x", summary=None, agent_id="t")
    await _put_doc(vault, "x/a.md", body="hello")
    await _put_doc(vault, "x/b.md", body="world")
    out = await svc.delete(vault=vault, path="x", recursive=True, agent_id="t")
    assert out["deleted_docs"] == 2
    # verify single commit
    log = await _git_log(vault)
    assert "[delete-collection] x" in log[0]["message"]
```

(`_put_doc` / `_git_log` — reuse whatever the project's existing service-test helpers provide. If none, gate these behind `@pytest.mark.skip` and rely on the e2e suite for cascade coverage.)

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && pytest tests/test_collection_service.py -v -k delete
```
Expected: 3 fails with `AttributeError: 'CollectionService' object has no attribute 'delete'`.

- [ ] **Step 3: Implement `delete`**

Append to `class CollectionService` in `backend/app/services/collection_service.py`:

```python
    async def delete(
        self,
        *,
        vault: str,
        path: str,
        recursive: bool,
        agent_id: str | None,
    ) -> dict:
        norm = _normalize_path(path)
        vault_repo, doc_repo, coll_repo = await self._repos()
        vault_id = await vault_repo.get_id_by_name(vault)
        if not vault_id:
            raise NotFoundError("Vault", vault)

        pool = await get_pool()
        # Snapshot under FOR UPDATE so concurrent puts block until we're done.
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id, doc_count FROM collections "
                    "WHERE vault_id = $1 AND path = $2 FOR UPDATE",
                    vault_id, norm,
                )
                if row is None:
                    raise NotFoundError("Collection", norm)
                collection_id = row["id"]

                docs = await coll_repo.list_docs_under(vault_id, norm, conn=conn)
                files = await coll_repo.list_files_under(vault_id, norm, conn=conn)

                if (docs or files) and not recursive:
                    # Release the txn before raising
                    raise CollectionNotEmptyError(len(docs), len(files))

        # Git first (outside the PG txn — slow + crash-prone). One commit.
        if docs or files:
            paths_to_remove = [d["path"] for d in docs] + [
                f"_files/{f['id']}" for f in files  # adjust to project's file path convention
            ]
            commit_msg = (
                f"[delete-collection] {norm}\n\n"
                f"{len(docs)} docs, {len(files)} files\n"
                f"agent: {agent_id or 'unknown'}\n"
                f"action: delete-collection"
            )
            import asyncio
            await asyncio.to_thread(
                self.git.delete_paths_bulk,
                vault_name=vault, file_paths=paths_to_remove, message=commit_msg,
            )

        # Now the single PG cleanup transaction.
        async with pool.acquire() as conn:
            async with conn.transaction():
                for d in docs:
                    await delete_document_chunks(conn, str(d["id"]))
                    await delete_document_relations(conn, vault, d["path"])
                    await conn.execute("DELETE FROM documents WHERE id = $1", d["id"])
                for f in files:
                    # Writes s3_delete_outbox per project convention (see migration 019).
                    await conn.execute(
                        "INSERT INTO s3_delete_outbox (s3_key) VALUES ($1)",
                        f["s3_key"],
                    )
                    await conn.execute("DELETE FROM vault_files WHERE id = $1", f["id"])
                await conn.execute("DELETE FROM collections WHERE id = $1", collection_id)
                await emit_event(
                    conn, "collection.delete",
                    vault_id=vault_id, ref_type="collection", ref_id=norm,
                    actor_id=agent_id,
                    payload={
                        "vault": vault, "path": norm,
                        "deleted_docs": len(docs),
                        "deleted_files": len(files),
                    },
                )

        logger.info("Collection deleted: %s/%s (%d docs, %d files)",
                    vault, norm, len(docs), len(files))
        return {
            "ok": True,
            "collection": norm,
            "deleted_docs": len(docs),
            "deleted_files": len(files),
        }
```

(Adjust the file-path convention for `delete_paths_bulk` to match how files are actually stored in git. If files live in S3 only and not in git, drop them from the `paths_to_remove` list and only write the `s3_delete_outbox` entries — re-verify by reading `file_service.py`.)

- [ ] **Step 4: Verify file-path convention before running tests**

```bash
grep -n "delete_file\|delete_paths\|_files/" backend/app/services/file_service.py | head -20
```
If files aren't tracked in git, remove `_files/{f['id']}` from `paths_to_remove`.

- [ ] **Step 5: Run delete tests**

```bash
cd backend && pytest tests/test_collection_service.py -v -k delete
```
Expected: 3 passes.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/collection_service.py backend/tests/test_collection_service.py
git commit -m "feat(service): CollectionService.delete with empty + recursive cascade"
```

---

## Task 5 — REST routes for collections

**Files:**
- Modify: `backend/app/api/routes/collections.py`

**Context:** Router file already exists with `GET /browse/{vault}`. We add `POST /collections/{vault}` and `DELETE /collections/{vault}/{path:path}`. ACL via `check_vault_access(..., required_role="writer")` for both. The router is already included in `main.py:72` with prefix `/api/v1`.

- [ ] **Step 1: Add route handlers**

Append to `backend/app/api/routes/collections.py`:

```python
from fastapi import HTTPException, status
from pydantic import BaseModel

from app.services.collection_service import (
    CollectionService, CollectionNotEmptyError, InvalidPathError,
)
from app.utils.errors import NotFoundError

collection_service = CollectionService()


class CreateCollectionRequest(BaseModel):
    path: str
    summary: str | None = None


@router.post("/collections/{vault}", summary="Create an empty collection")
async def create_collection(
    vault: str,
    body: CreateCollectionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        return await collection_service.create(
            vault=vault, path=body.path, summary=body.summary,
            agent_id=user.user_id,
        )
    except InvalidPathError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.delete("/collections/{vault}/{path:path}", summary="Delete a collection")
async def delete_collection(
    vault: str,
    path: str,
    recursive: bool = Query(False),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        return await collection_service.delete(
            vault=vault, path=path, recursive=recursive,
            agent_id=user.user_id,
        )
    except InvalidPathError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    except CollectionNotEmptyError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "doc_count": exc.doc_count,
                "file_count": exc.file_count,
            },
        )
```

- [ ] **Step 2: Smoke-check the routes register**

```bash
cd backend && python -c "from app.main import app; \
print([r.path for r in app.routes if 'collection' in r.path.lower()])"
```
Expected output includes `'/api/v1/collections/{vault}'` and `'/api/v1/collections/{vault}/{path}'`.

- [ ] **Step 3: Commit**

```bash
git add backend/app/api/routes/collections.py
git commit -m "feat(api): POST/DELETE /collections endpoints"
```

---

## Task 6 — MCP tool schemas + handlers

**Files:**
- Modify: `backend/mcp_server/tools.py`
- Modify: `backend/mcp_server/server.py`
- Modify: `backend/mcp_server/help.py`

**Context:** Tool registration pattern — `tools.py` appends to a list of dicts (see `akb_delete_vault` at line 817 for the shape). Handlers in `server.py` use `@_h("akb_name")` decorator (see `_handle_delete_vault` at line 713). Help in `help.py` adds a verb-table row and a body entry in `_TOOL_DOCS`.

- [ ] **Step 1: Add tool schemas**

Append to `backend/mcp_server/tools.py` (near the existing vault-related tools):

```python
{
    "name": "akb_create_collection",
    "description": (
        "Create an empty collection (folder) inside a vault. Idempotent — "
        "returns {created: false} if the collection already exists."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "vault": {"type": "string", "description": "Vault name."},
            "path":  {"type": "string", "description": "Collection path, e.g. 'api-specs' or 'docs/guides'."},
            "summary": {"type": "string", "description": "Optional one-line description."},
        },
        "required": ["vault", "path"],
    },
},
{
    "name": "akb_delete_collection",
    "description": (
        "Delete a collection. If empty, removes the metadata row. If non-empty, "
        "requires recursive=true to cascade delete every document and file under the path."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "vault": {"type": "string"},
            "path":  {"type": "string"},
            "recursive": {
                "type": "boolean", "default": False,
                "description": "Required when the collection is non-empty.",
            },
        },
        "required": ["vault", "path"],
    },
},
```

- [ ] **Step 2: Add handlers**

Append to `backend/mcp_server/server.py` (near `_handle_delete_vault`):

```python
from app.services.collection_service import (
    CollectionService, CollectionNotEmptyError, InvalidPathError,
)

_collection_service = CollectionService()


@_h("akb_create_collection")
async def _handle_create_collection(args: dict, uid: str, user: _MCPUser) -> dict:
    vault = args["vault"]
    await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        return await _collection_service.create(
            vault=vault, path=args["path"],
            summary=args.get("summary"),
            agent_id=user.user_id,
        )
    except InvalidPathError as exc:
        return {"error": "invalid_path", "message": str(exc)}


@_h("akb_delete_collection")
async def _handle_delete_collection(args: dict, uid: str, user: _MCPUser) -> dict:
    vault = args["vault"]
    await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        return await _collection_service.delete(
            vault=vault, path=args["path"],
            recursive=bool(args.get("recursive", False)),
            agent_id=user.user_id,
        )
    except InvalidPathError as exc:
        return {"error": "invalid_path", "message": str(exc)}
    except CollectionNotEmptyError as exc:
        return {
            "error": "not_empty",
            "message": str(exc),
            "doc_count": exc.doc_count,
            "file_count": exc.file_count,
        }
```

(Adjust error envelope to match the project's existing convention — check `_handle_delete_vault` for the exact return shape.)

- [ ] **Step 3: Add help entries**

In `backend/mcp_server/help.py`:
- Add two rows to the verb table near `akb_delete` (line 124-ish):

```
| `akb_create_collection` | Creating an empty collection |
| `akb_delete_collection` | Removing a collection (cascade with recursive) |
```

- Add two `_TOOL_DOCS` entries (near `akb_delete` at line 1020):

```python
    "akb_create_collection": """# akb_create_collection — Create an empty collection

Creates a collection (folder) in a vault. Idempotent — returns
{created: false} if it already exists. No git side effects (empty
collections have no files; the entry is metadata only).

## Example
akb_create_collection(vault="eng", path="api-specs",
                       summary="Public API contracts")
""",
    "akb_delete_collection": """# akb_delete_collection — Delete a collection

Empty collection: pass just vault + path. Non-empty: pass
recursive=true to cascade delete all documents and files under the
path. Cascade emits one git commit for the whole batch.

## Example
akb_delete_collection(vault="eng", path="old-specs")             # empty
akb_delete_collection(vault="eng", path="legacy", recursive=True) # cascade
""",
```

- [ ] **Step 4: Smoke-check tools registered**

```bash
cd backend && python -c "from mcp_server.tools import TOOLS; \
print([t['name'] for t in TOOLS if 'collection' in t['name']])"
```
Expected: `['akb_create_collection', 'akb_delete_collection']`.

- [ ] **Step 5: Commit**

```bash
git add backend/mcp_server/tools.py backend/mcp_server/server.py backend/mcp_server/help.py
git commit -m "feat(mcp): akb_create_collection + akb_delete_collection tools"
```

---

## Task 7 — Backend E2E suite

**Files:**
- Create: `backend/tests/test_collection_lifecycle_e2e.sh`
- Modify: `backend/tests/test_mcp_e2e.sh` (small addition)

**Context:** E2E shell tests are the project's primary integration coverage (see CLAUDE.md). Each test creates an ephemeral user + vault, runs assertions via `mcp_call` and the REST API, and cleans up. Reuse the helper functions from `test_mcp_e2e.sh` (`mcp_call`, `mcp_result`, etc.).

- [ ] **Step 1: Bootstrap the test file**

Create `backend/tests/test_collection_lifecycle_e2e.sh`:

```bash
#!/usr/bin/env bash
# E2E for collection lifecycle: create empty, idempotent create, invalid paths,
# delete empty, delete non-empty (rejected + recursive), ACL.
set -euo pipefail

source "$(dirname "$0")/_test_lib.sh"   # use existing helper file if any;
                                        # otherwise copy mcp_call / pass / fail
                                        # boilerplate from test_mcp_e2e.sh

VAULT="coll-e2e-$(date +%s)"
TOKEN=$(bootstrap_user_and_token "coll-tester-$$@example.com")
create_vault "$VAULT" "$TOKEN"

# ── 1. Create empty ─────────────────────────────────────────────
R=$(mcp_call akb_create_collection "{\"vault\":\"$VAULT\",\"path\":\"specs\"}" | mcp_result)
echo "$R" | jq -e '.created == true' >/dev/null && pass "create empty" || fail "create empty" "$R"

# ── 2. Idempotent create ────────────────────────────────────────
R=$(mcp_call akb_create_collection "{\"vault\":\"$VAULT\",\"path\":\"specs\"}" | mcp_result)
echo "$R" | jq -e '.created == false' >/dev/null && pass "idempotent" || fail "idempotent" "$R"

# ── 3. Empty browse shows the collection with doc_count=0 ───────
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result)
COUNT=$(echo "$R" | jq -r '.items[] | select(.name=="specs") | .doc_count // 0')
[ "$COUNT" = "0" ] && pass "browse shows empty collection" || fail "browse empty" "doc_count=$COUNT"

# ── 4. Invalid paths rejected ───────────────────────────────────
for bad in "" "/" "../etc" "a/../b"; do
  R=$(mcp_call akb_create_collection "{\"vault\":\"$VAULT\",\"path\":\"$bad\"}" | mcp_result)
  echo "$R" | jq -e '.error == "invalid_path"' >/dev/null \
    && pass "reject invalid: $bad" || fail "reject invalid: $bad" "$R"
done

# ── 5. Delete empty ─────────────────────────────────────────────
R=$(mcp_call akb_delete_collection "{\"vault\":\"$VAULT\",\"path\":\"specs\"}" | mcp_result)
echo "$R" | jq -e '.deleted_docs == 0' >/dev/null && pass "delete empty" || fail "delete empty" "$R"

# ── 6. Delete non-empty without recursive → reject ─────────────
mcp_call akb_create_collection "{\"vault\":\"$VAULT\",\"path\":\"docs\"}" >/dev/null
DOC_R=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"docs\",\"title\":\"t\",\"content\":\"body\"}")
R=$(mcp_call akb_delete_collection "{\"vault\":\"$VAULT\",\"path\":\"docs\"}" | mcp_result)
echo "$R" | jq -e '.error == "not_empty" and .doc_count >= 1' >/dev/null \
  && pass "reject non-empty" || fail "reject non-empty" "$R"

# ── 7. Delete recursive cascade ─────────────────────────────────
R=$(mcp_call akb_delete_collection \
    "{\"vault\":\"$VAULT\",\"path\":\"docs\",\"recursive\":true}" | mcp_result)
echo "$R" | jq -e '.deleted_docs >= 1' >/dev/null && pass "cascade" || fail "cascade" "$R"

# Confirm collection + docs gone from browse
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result)
echo "$R" | jq -e '.items | map(select(.name=="docs")) | length == 0' >/dev/null \
  && pass "browse after cascade" || fail "browse after cascade" "$R"

# ── 8. ACL: reader cannot create/delete ─────────────────────────
READER_TOKEN=$(bootstrap_user_and_token "coll-reader-$$@example.com")
mcp_call akb_grant "{\"vault\":\"$VAULT\",\"user\":\"coll-reader-$$@example.com\",\"role\":\"reader\"}" >/dev/null
HTTP=$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "Authorization: Bearer $READER_TOKEN" \
  -X POST "$AKB_URL/api/v1/collections/$VAULT" \
  -H 'Content-Type: application/json' -d '{"path":"x"}')
[ "$HTTP" = "403" ] && pass "reader 403 on create" || fail "reader 403" "got $HTTP"

cleanup_vault "$VAULT" "$TOKEN"
echo "✓ test_collection_lifecycle_e2e.sh"
```

- [ ] **Step 2: Add empty-is-valid case to existing suite**

In `backend/tests/test_mcp_e2e.sh`, after the existing delete-document test (around line 238), add:

```bash
# Empty-is-valid invariant: deleting the last doc in a collection
# leaves the collection row in place (Plan: 2026-05-12 collection lifecycle).
mcp_call akb_create_collection "{\"vault\":\"$VAULT\",\"path\":\"keepempty\"}" >/dev/null
PUTR=$(mcp_call akb_put "{\"vault\":\"$VAULT\",\"collection\":\"keepempty\",\"title\":\"t\",\"content\":\"c\"}")
DOC_ID=$(echo "$PUTR" | mcp_result | jq -r '.id')
mcp_call akb_delete "{\"vault\":\"$VAULT\",\"doc_id\":\"$DOC_ID\"}" >/dev/null
R=$(mcp_call akb_browse "{\"vault\":\"$VAULT\"}" | mcp_result)
HAS=$(echo "$R" | jq -r '.items | map(select(.name=="keepempty")) | length')
[ "$HAS" = "1" ] && pass "empty collection survives last-doc delete" \
  || fail "empty collection should remain" "$R"
```

- [ ] **Step 3: Run the new suite locally**

```bash
cd backend && bash tests/test_collection_lifecycle_e2e.sh
```
Expected: all 8 sections pass against `AKB_URL=http://localhost:8000`.

- [ ] **Step 4: Run extended mcp e2e**

```bash
cd backend && bash tests/test_mcp_e2e.sh
```
Expected: still all green, with the new empty-survives assertion included.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_collection_lifecycle_e2e.sh backend/tests/test_mcp_e2e.sh
git commit -m "test(e2e): collection lifecycle suite + empty-is-valid invariant"
```

---

## Task 8 — Frontend `api.ts` helpers

**Files:**
- Modify: `frontend/src/lib/api.ts`

**Context:** Existing helpers all use the `api<T>(...)` wrapper. Conflict (409) errors need typed payload for the dialog to render counts. Pattern: read `delete-vault-dialog.tsx` and `deleteVault` in `api.ts` to mirror.

- [ ] **Step 1: Add the helpers**

Append to `frontend/src/lib/api.ts` (near `archiveVault` / `deleteVault`):

```typescript
// ── Collections ──
export interface CollectionCreateResult {
  ok: true;
  created: boolean;
  collection: { path: string; name: string; summary: string | null; doc_count: number };
}

export interface CollectionDeleteResult {
  ok: true;
  collection: string;
  deleted_docs: number;
  deleted_files: number;
}

export interface CollectionNotEmptyError {
  message: string;
  doc_count: number;
  file_count: number;
}

export const createCollection = (vault: string, path: string, summary?: string) =>
  api<CollectionCreateResult>(`/collections/${vault}`, {
    method: "POST",
    body: JSON.stringify({ path, summary }),
  });

export const deleteCollection = (vault: string, path: string, recursive: boolean) => {
  const qs = recursive ? "?recursive=true" : "";
  // path may contain '/', and the route already uses {path:path} catch-all
  // — pass it raw (URLEncoded once via encodeURI on each segment).
  const segs = path.split("/").map(encodeURIComponent).join("/");
  return api<CollectionDeleteResult>(`/collections/${vault}/${segs}${qs}`, {
    method: "DELETE",
  });
};
```

- [ ] **Step 2: Smoke check the types compile**

```bash
cd frontend && pnpm tsc --noEmit
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/api.ts
git commit -m "feat(api-client): createCollection + deleteCollection"
```

---

## Task 9 — `CreateCollectionDialog`

**Files:**
- Create: `frontend/src/components/create-collection-dialog.tsx`
- Create: `frontend/src/components/__tests__/create-collection-dialog.test.tsx`

**Context:** Use the project's dialog primitives — read `frontmatter-edit-dialog.tsx` or `delete-vault-dialog.tsx` for the existing pattern (Radix or custom). Input validation mirrors the server: trim slashes, reject empty/`.`/`..`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/__tests__/create-collection-dialog.test.tsx`:

```typescript
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { CreateCollectionDialog } from "../create-collection-dialog";
import * as api from "@/lib/api";

vi.mock("@/lib/api", () => ({
  createCollection: vi.fn(),
}));

describe("CreateCollectionDialog", () => {
  beforeEach(() => vi.clearAllMocks());

  it("validates path client-side before submit", async () => {
    render(<CreateCollectionDialog vault="v" open onClose={() => {}} onCreated={() => {}} />);
    fireEvent.change(screen.getByLabelText(/path/i), { target: { value: "../bad" } });
    fireEvent.click(screen.getByRole("button", { name: /create/i }));
    expect(await screen.findByText(/invalid/i)).toBeInTheDocument();
    expect(api.createCollection).not.toHaveBeenCalled();
  });

  it("renders 'already exists' inline on created=false", async () => {
    (api.createCollection as any).mockResolvedValue({
      ok: true, created: false,
      collection: { path: "x", name: "x", summary: null, doc_count: 0 },
    });
    render(<CreateCollectionDialog vault="v" open onClose={() => {}} onCreated={() => {}} />);
    fireEvent.change(screen.getByLabelText(/path/i), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: /create/i }));
    expect(await screen.findByText(/already exists/i)).toBeInTheDocument();
  });

  it("calls onCreated on successful create", async () => {
    (api.createCollection as any).mockResolvedValue({
      ok: true, created: true,
      collection: { path: "new", name: "new", summary: null, doc_count: 0 },
    });
    const onCreated = vi.fn();
    render(<CreateCollectionDialog vault="v" open onClose={() => {}} onCreated={onCreated} />);
    fireEvent.change(screen.getByLabelText(/path/i), { target: { value: "new" } });
    fireEvent.click(screen.getByRole("button", { name: /create/i }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd frontend && pnpm vitest run components/__tests__/create-collection-dialog.test.tsx
```
Expected: module not found (`create-collection-dialog`).

- [ ] **Step 3: Implement the component**

Create `frontend/src/components/create-collection-dialog.tsx`:

```tsx
import { useState } from "react";
import { createCollection } from "@/lib/api";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface Props {
  vault: string;
  open: boolean;
  onClose: () => void;
  onCreated: (path: string) => void;
}

function validatePath(raw: string): string | null {
  const trimmed = raw.trim().replace(/^\/+|\/+$/g, "");
  if (!trimmed) return "Path is empty.";
  for (const seg of trimmed.split("/")) {
    if (seg === "" || seg === "." || seg === "..") return `Invalid segment: ${seg || "(empty)"}`;
  }
  if (trimmed.length > 1024) return "Path is too long.";
  return null;
}

export function CreateCollectionDialog({ vault, open, onClose, onCreated }: Props) {
  const [path, setPath] = useState("");
  const [summary, setSummary] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit() {
    setError(null);
    setInfo(null);
    const bad = validatePath(path);
    if (bad) { setError(`Invalid: ${bad}`); return; }
    setBusy(true);
    try {
      const r = await createCollection(vault, path.trim().replace(/^\/+|\/+$/g, ""), summary || undefined);
      if (!r.created) {
        setInfo("Collection already exists.");
      } else {
        onCreated(r.collection.path);
        onClose();
      }
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New collection</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <label className="block">
            <span className="coord">Path</span>
            <Input value={path} onChange={(e) => setPath(e.target.value)} placeholder="api-specs" autoFocus />
          </label>
          <label className="block">
            <span className="coord">Summary (optional)</span>
            <Input value={summary} onChange={(e) => setSummary(e.target.value)} />
          </label>
          {error && <p role="alert" className="text-destructive text-xs font-mono">{error}</p>}
          {info && <p className="text-foreground-muted text-xs font-mono">{info}</p>}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button onClick={handleSubmit} disabled={busy}>Create</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
```

(Adjust import paths and primitive names to whatever this project uses — check existing dialogs first.)

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd frontend && pnpm vitest run components/__tests__/create-collection-dialog.test.tsx
```
Expected: 3 passes.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/create-collection-dialog.tsx \
        frontend/src/components/__tests__/create-collection-dialog.test.tsx
git commit -m "feat(ui): CreateCollectionDialog with client-side path validation"
```

---

## Task 10 — `DeleteCollectionDialog`

**Files:**
- Create: `frontend/src/components/delete-collection-dialog.tsx`
- Create: `frontend/src/components/__tests__/delete-collection-dialog.test.tsx`

**Context:** Two modes selected by `docCount + fileCount`. Empty mode = simple confirm. Cascade mode = type-to-confirm input matching the collection's path. Sends `recursive=true` in cascade. Mirror `delete-vault-dialog.tsx` for visual treatment.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/components/__tests__/delete-collection-dialog.test.tsx`:

```typescript
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { DeleteCollectionDialog } from "../delete-collection-dialog";
import * as api from "@/lib/api";

vi.mock("@/lib/api", () => ({ deleteCollection: vi.fn() }));

describe("DeleteCollectionDialog", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders simple confirm when collection is empty", async () => {
    (api.deleteCollection as any).mockResolvedValue({
      ok: true, collection: "x", deleted_docs: 0, deleted_files: 0,
    });
    render(
      <DeleteCollectionDialog
        vault="v" path="x" docCount={0} fileCount={0}
        open onClose={() => {}} onDeleted={() => {}}
      />,
    );
    expect(screen.queryByLabelText(/type/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /delete/i }));
    await waitFor(() => expect(api.deleteCollection).toHaveBeenCalledWith("v", "x", false));
  });

  it("requires type-to-confirm in cascade mode", async () => {
    (api.deleteCollection as any).mockResolvedValue({
      ok: true, collection: "x", deleted_docs: 3, deleted_files: 1,
    });
    render(
      <DeleteCollectionDialog
        vault="v" path="x" docCount={3} fileCount={1}
        open onClose={() => {}} onDeleted={() => {}}
      />,
    );
    const btn = screen.getByRole("button", { name: /delete/i });
    expect(btn).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/type/i), { target: { value: "x" } });
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    await waitFor(() => expect(api.deleteCollection).toHaveBeenCalledWith("v", "x", true));
  });
});
```

- [ ] **Step 2: Run test, see fail**

```bash
cd frontend && pnpm vitest run components/__tests__/delete-collection-dialog.test.tsx
```
Expected: module not found.

- [ ] **Step 3: Implement the component**

Create `frontend/src/components/delete-collection-dialog.tsx` mirroring `delete-vault-dialog.tsx`. Implementation summary:

```tsx
import { useState } from "react";
import { deleteCollection } from "@/lib/api";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface Props {
  vault: string;
  path: string;
  docCount: number;
  fileCount: number;
  open: boolean;
  onClose: () => void;
  onDeleted: () => void;
}

export function DeleteCollectionDialog({ vault, path, docCount, fileCount, open, onClose, onDeleted }: Props) {
  const total = docCount + fileCount;
  const isCascade = total > 0;
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canDelete = !isCascade || typed === path;

  async function handleDelete() {
    setError(null);
    setBusy(true);
    try {
      await deleteCollection(vault, path, isCascade);
      onDeleted();
      onClose();
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isCascade ? "Delete collection and all contents" : "Delete empty collection"}</DialogTitle>
        </DialogHeader>
        {isCascade ? (
          <div className="space-y-3">
            <p className="text-sm">
              This will permanently delete <strong>{docCount}</strong> document{docCount === 1 ? "" : "s"}
              {fileCount > 0 && <> and <strong>{fileCount}</strong> file{fileCount === 1 ? "" : "s"}</>}
              {" "}in <code>{path}</code>.
            </p>
            <label className="block">
              <span className="coord">Type the collection path to confirm</span>
              <Input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={path} />
            </label>
          </div>
        ) : (
          <p className="text-sm">Delete empty collection <code>{path}</code>?</p>
        )}
        {error && <p role="alert" className="text-destructive text-xs font-mono">{error}</p>}
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button
            variant="destructive"
            onClick={handleDelete}
            disabled={!canDelete || busy}
          >
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 4: Run tests, see pass**

```bash
cd frontend && pnpm vitest run components/__tests__/delete-collection-dialog.test.tsx
```
Expected: 2 passes.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/delete-collection-dialog.tsx \
        frontend/src/components/__tests__/delete-collection-dialog.test.tsx
git commit -m "feat(ui): DeleteCollectionDialog with empty + cascade modes"
```

---

## Task 11 — `vault-explorer` integration: `+ Collection` and row hover delete

**Files:**
- Modify: `frontend/src/components/vault-explorer.tsx`

**Context:** Existing component already has `kind === "collection"` rendering with chevron + name + count (`vault-explorer.tsx:351-372`). We add (a) a `+ Collection` button at the top header, (b) a trash icon visible on hover for collection rows when the user is writer+, and (c) preserve the existing tree behavior.

- [ ] **Step 1: Add the header `+ Collection` button**

Find the section header for the documents/tree (look near the top of the component's JSX, where the "DOCUMENTS" or similar label renders). Add a button next to the section label that opens `CreateCollectionDialog`. Pass `vault` and `onCreated` (the latter calls `refetchTree` — wired in Task 13).

- [ ] **Step 2: Add per-row delete icon**

In `TreeRow` (`vault-explorer.tsx:346`), when `node.kind === "collection"` and the current user has `writer | admin | owner` role for the vault, render a small `<button>` with a `Trash2` icon that opens `DeleteCollectionDialog` with the precomputed `countDocs(node)` and a parallel `countFiles(node)` helper.

Suggested addition:

```tsx
// inside TreeRow, after the chevron span, before the count span
{(vaultRole === "writer" || vaultRole === "admin" || vaultRole === "owner") && (
  <button
    onClick={(e) => { e.stopPropagation(); onDeleteCollection(node); }}
    aria-label={`Delete collection ${node.name}`}
    className="ml-auto opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity hover:text-destructive"
  >
    <Trash2 className="h-3 w-3" aria-hidden />
  </button>
)}
```

(Pass `vaultRole` and `onDeleteCollection` callback down from the parent.)

- [ ] **Step 3: Wire dialogs at the top of the component**

Add state for `createOpen: boolean` and `deleteTarget: TreeNode | null`. Render both dialogs at the bottom of the explorer's JSX.

- [ ] **Step 4: Manually verify in the dev server**

```bash
cd frontend && pnpm dev
```
Open the app, log in, navigate to a vault. Test:
- `+ Collection` button opens dialog; create works; new collection appears (after Task 13's refresh wiring; for now you may need to reload).
- Hover a collection row → trash icon visible; click → dialog opens correctly per empty/cascade.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/vault-explorer.tsx
git commit -m "feat(ui): vault-explorer wires create/delete collection dialogs"
```

---

## Task 12 — Refresh: hooks expose `refetch`, context, sidebar `⟳`

**Files:**
- Create: `frontend/src/hooks/use-vaults.ts`
- Modify: `frontend/src/hooks/use-vault-tree.ts`
- Create: `frontend/src/contexts/vault-refresh-context.tsx`
- Modify: `frontend/src/components/vault-nav.tsx`
- Modify: `frontend/src/components/vault-explorer.tsx`
- Modify: `frontend/src/components/vault-shell.tsx` (or whatever mounts both)

**Context:** Two separate fetch sources — vault list (currently inlined in `vault-nav.tsx` with `useEffect` on `pathname`) and per-vault tree (`use-vault-tree.ts`). We extract the former into a hook, expose `refetch` from both, and plumb them via a small React context so mutation handlers can invalidate without prop drilling.

- [ ] **Step 1: Add `refetch` to `use-vault-tree`**

In `frontend/src/hooks/use-vault-tree.ts`, refactor `useVaultTree`:

```typescript
export function useVaultTree(vault: string | undefined) {
  const [items, setItems] = useState<BrowseItem[] | null>(null);
  const [error, setError] = useState<string>("");

  const refetch = useCallback(() => {
    if (!vault) return;
    setItems(null);
    setError("");
    browseVault(vault, undefined, 2)
      .then((d) => setItems(d.items as BrowseItem[]))
      .catch((e) => setError(e.message || String(e)));
  }, [vault]);

  useEffect(() => { refetch(); }, [refetch]);

  const tree = useMemo<TreeNode[] | null>(
    () => (items ? buildTree(items) : null),
    [items],
  );

  return { tree, loading: items === null && !error, error, refetch };
}
```

- [ ] **Step 2: Create `use-vaults` hook**

Create `frontend/src/hooks/use-vaults.ts`:

```typescript
import { useCallback, useEffect, useState } from "react";
import { listVaults } from "@/lib/api";

export interface VaultSummary {
  id: string;
  name: string;
  role?: string;
  is_pinned?: boolean;
}

export function useVaults() {
  const [vaults, setVaults] = useState<VaultSummary[]>([]);
  const [loading, setLoading] = useState(false);

  const refetch = useCallback(() => {
    setLoading(true);
    listVaults()
      .then((d) => setVaults((d.vaults as VaultSummary[]) || []))
      .catch(() => setVaults([]))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { refetch(); }, [refetch]);

  return { vaults, loading, refetch };
}
```

- [ ] **Step 3: Create the context**

Create `frontend/src/contexts/vault-refresh-context.tsx`:

```typescript
import { createContext, ReactNode, useContext } from "react";

interface VaultRefreshContextValue {
  refetchVaults: () => void;
  refetchTree: () => void;
}

const VaultRefreshContext = createContext<VaultRefreshContextValue>({
  refetchVaults: () => {},
  refetchTree: () => {},
});

export function VaultRefreshProvider({
  children, refetchVaults, refetchTree,
}: VaultRefreshContextValue & { children: ReactNode }) {
  return (
    <VaultRefreshContext.Provider value={{ refetchVaults, refetchTree }}>
      {children}
    </VaultRefreshContext.Provider>
  );
}

export const useVaultRefresh = () => useContext(VaultRefreshContext);
```

- [ ] **Step 4: Refactor `vault-nav.tsx` to use the hook**

Replace the inline fetch with `useVaults()`. Add a `⟳` button next to the existing `+` button in the header:

```tsx
import { RefreshCw } from "lucide-react";
import { useVaults } from "@/hooks/use-vaults";
// ...
const { vaults, refetch: refetchVaults, loading } = useVaults();
// drop the existing useState + useEffect for vaults
// ...
<button
  type="button"
  onClick={refetchVaults}
  aria-label="Refresh vaults"
  className="text-foreground-muted hover:text-accent transition-colors ml-1"
>
  <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} aria-hidden />
</button>
```

Export `refetchVaults` via context — see Step 6.

- [ ] **Step 5: Add `⟳` to vault-explorer header**

Mirror the same button next to the documents section header. Consume `tree.refetch` directly within the explorer.

- [ ] **Step 6: Install the provider**

Locate the component that mounts both `<VaultNav>` and `<VaultExplorer>` for the same vault (`vault-shell.tsx` is the likely candidate). Wrap the children in `<VaultRefreshProvider refetchVaults={refetchVaults} refetchTree={refetchTree}>`. This requires lifting `useVaults()` and the tree's `refetch` up to the shell level (call `useVaults()` and `useVaultTree(vault)` in `vault-shell.tsx` and pass the data down — read the existing prop wiring first).

- [ ] **Step 7: Wire mutation success handlers**

Audit and update:
- `CreateCollectionDialog` / `DeleteCollectionDialog` consumers in `vault-explorer.tsx` — call `refetchTree()` on success.
- `delete-vault-dialog.tsx` — call `refetchVaults()` on success.
- Document delete/edit handlers in `document.tsx` — call `refetchTree()` (delete) on success.
- File upload / delete in `file-viewer.tsx` — call `refetchTree()` on success.

Each call site imports `useVaultRefresh` and invokes the appropriate function in its success handler.

- [ ] **Step 8: Manually verify in dev server**

```bash
cd frontend && pnpm dev
```
Verify:
- `⟳` in vault header refetches the vault list (spinner visible).
- `⟳` in tree header refetches the tree.
- Creating a collection auto-refreshes the tree without a manual click.
- Deleting a vault auto-refreshes the vault list.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/hooks/use-vaults.ts \
        frontend/src/hooks/use-vault-tree.ts \
        frontend/src/contexts/vault-refresh-context.tsx \
        frontend/src/components/vault-nav.tsx \
        frontend/src/components/vault-explorer.tsx \
        frontend/src/components/vault-shell.tsx \
        frontend/src/components/delete-vault-dialog.tsx \
        frontend/src/pages/document.tsx \
        frontend/src/components/file-viewer.tsx
git commit -m "feat(ui): manual refresh + post-mutation invalidate via VaultRefreshContext"
```

---

## Task 13 — Document viewer rendered ↔ raw toggle

**Files:**
- Modify: `frontend/src/pages/document.tsx`
- Create: `frontend/src/pages/__tests__/document-view-toggle.test.tsx`

**Context:** Single-file change. View mode lives in URL query (`?view=raw`). Raw branch renders `<pre>` with `whitespace-pre-wrap` and a Copy button using the existing `copied` pattern (see `copyPublicLink` in `document.tsx:280-290`).

- [ ] **Step 1: Write the failing test**

Create `frontend/src/pages/__tests__/document-view-toggle.test.tsx`:

```typescript
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import DocumentPage from "../document";
import * as api from "@/lib/api";

vi.mock("@/lib/api", () => ({
  getDocument: vi.fn().mockResolvedValue({
    id: "d-1", path: "x.md", title: "T", content: "# Hello\n\nworld",
  }),
  getVaultInfo: vi.fn().mockResolvedValue({ role: "owner" }),
  getRelations: vi.fn().mockResolvedValue({ relations: [] }),
  publishDoc: vi.fn(),
  unpublishDoc: vi.fn(),
  deleteDocument: vi.fn(),
}));

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/vault/:name/doc/:id" element={<DocumentPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("DocumentPage view toggle", () => {
  it("renders Markdown by default", async () => {
    renderAt("/vault/v/doc/x.md");
    await screen.findByRole("heading", { name: "Hello" });
  });

  it("renders raw <pre> when ?view=raw", async () => {
    renderAt("/vault/v/doc/x.md?view=raw");
    const pre = await screen.findByTestId("doc-raw");
    expect(pre.textContent).toContain("# Hello");
    expect(screen.queryByRole("heading", { name: "Hello" })).not.toBeInTheDocument();
  });

  it("copy button writes raw content to clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderAt("/vault/v/doc/x.md?view=raw");
    fireEvent.click(await screen.findByRole("button", { name: /copy/i }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("# Hello\n\nworld"));
  });
});
```

- [ ] **Step 2: Run test, see fail**

```bash
cd frontend && pnpm vitest run pages/__tests__/document-view-toggle.test.tsx
```
Expected: failures (no toggle, no data-testid).

- [ ] **Step 3: Implement the toggle**

In `frontend/src/pages/document.tsx`:

```tsx
// near other useState declarations:
const [searchParams, setSearchParams] = useSearchParams();
const view: "rendered" | "raw" = searchParams.get("view") === "raw" ? "raw" : "rendered";
const [copiedRaw, setCopiedRaw] = useState(false);

const setView = (next: "rendered" | "raw") => {
  const p = new URLSearchParams(searchParams);
  if (next === "raw") p.set("view", "raw"); else p.delete("view");
  setSearchParams(p, { replace: true });
};

async function copyRaw() {
  try {
    await navigator.clipboard.writeText(doc?.content || "");
    setCopiedRaw(true);
    setTimeout(() => setCopiedRaw(false), 1500);
  } catch {}
}
```

Replace the markdown render block (`document.tsx:224-231`):

```tsx
<div className="flex items-center gap-2 mb-3">
  <button
    onClick={() => setView(view === "raw" ? "rendered" : "raw")}
    className="text-xs font-mono uppercase tracking-wider text-foreground-muted hover:text-accent transition-colors cursor-pointer"
    aria-pressed={view === "raw"}
  >
    {view === "raw" ? "RENDERED" : "RAW"}
  </button>
</div>

{view === "rendered" ? (
  <div className="prose dark:prose-invert min-w-0" style={{ maxWidth: "100%" }}>
    <Markdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
      {doc.content || ""}
    </Markdown>
  </div>
) : (
  <div className="relative">
    <button
      onClick={copyRaw}
      aria-label="Copy markdown"
      className="absolute top-2 right-2 inline-flex items-center gap-1 px-2 py-1 text-xs font-mono text-foreground-muted hover:text-accent border border-border bg-surface"
    >
      {copiedRaw ? "Copied" : "Copy"}
    </button>
    <pre
      data-testid="doc-raw"
      className="font-mono text-[13px] leading-[1.55] whitespace-pre-wrap overflow-x-auto bg-surface-muted p-4"
    >
      {doc.content || ""}
    </pre>
  </div>
)}
```

Add the import for `useSearchParams`:

```tsx
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
```

- [ ] **Step 4: Run tests, see pass**

```bash
cd frontend && pnpm vitest run pages/__tests__/document-view-toggle.test.tsx
```
Expected: 3 passes.

- [ ] **Step 5: Manual smoke test**

```bash
cd frontend && pnpm dev
```
Open a document. Click `RAW` → URL updates to `?view=raw` and raw markdown shows. Click `Copy` → confirm clipboard via paste somewhere; button briefly reads "Copied". Refresh page → still raw (URL state survives reload).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/document.tsx \
        frontend/src/pages/__tests__/document-view-toggle.test.tsx
git commit -m "feat(ui): document viewer rendered/raw toggle with copy"
```

---

## Task 14 — Wire it up end-to-end and verify

**Files:** (no source changes)

- [ ] **Step 1: Backend integration sanity check**

```bash
docker compose up -d --build
AKB_URL=http://localhost:8000 bash backend/tests/test_collection_lifecycle_e2e.sh
AKB_URL=http://localhost:8000 bash backend/tests/test_mcp_e2e.sh
```
Expected: both suites pass.

- [ ] **Step 2: Full frontend test run**

```bash
cd frontend && pnpm vitest run && pnpm tsc --noEmit && pnpm lint
```
Expected: all green.

- [ ] **Step 3: Manual full path through the UI**

1. Create a new collection from the sidebar → tree refreshes automatically.
2. Put a document into it via the existing put dialog → tree refreshes.
3. Delete the document → collection remains visible with `doc_count: 0`.
4. Delete the empty collection → simple confirm → gone.
5. Create a collection, put 2 docs, attempt delete → cascade dialog with type-to-confirm → confirm → all gone in one commit (verify with `git -C /data/vaults/<vault>.git log --oneline`).
6. Toggle a document between rendered and raw → URL updates, copy works.

- [ ] **Step 4: Final commit (if any leftovers from manual testing)**

```bash
git status
# clean expected
```

---

## Notes for the executing engineer

- Run the **persistent worktree** through `asyncio.to_thread` for every git operation (`CLAUDE.md` convention) — match the pattern of existing `delete_file` callers in `document_service.py:551`.
- Treat the existing `delete-vault-dialog.tsx` as the canonical example for danger-zone visual treatment.
- If `vault_files` is not the actual table name, every SQL fragment that touches files needs updating — verify in `backend/app/db/init.sql` before running unit tests.
- If files are *not* tracked in git (S3-only), strip them from `delete_paths_bulk` calls in Task 4 and rely solely on `s3_delete_outbox`.
- Refresh wiring (Task 12) is the most surgically risky step because it touches 6+ existing files. Take Step 7 audit seriously — missed mutation handlers cause stale-cache bugs that look like "the new feature is broken."
