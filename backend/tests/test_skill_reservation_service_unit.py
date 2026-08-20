"""Service-level wiring tests: guards fire before any repo access."""
import pytest

from app.exceptions import ForbiddenError
from app.models.document import DocumentPutRequest
from app.services.document_service import DocumentService


@pytest.fixture()
def svc(monkeypatch):
    s = DocumentService.__new__(DocumentService)  # no __init__ side effects

    async def _boom(*a, **k):
        raise AssertionError("guard must fire before repo access")

    monkeypatch.setattr(s, "_repos", _boom, raising=False)
    return s


@pytest.mark.asyncio
async def test_put_into_overview_forbidden(svc):
    req = DocumentPutRequest(
        vault="v", collection="overview", title="T", content="x", type="note",
    )
    with pytest.raises(ForbiddenError):
        await svc.put(req)


@pytest.mark.asyncio
async def test_put_skill_type_elsewhere_forbidden(svc):
    req = DocumentPutRequest(
        vault="v", collection="notes", title="T", content="x", type="skill",
    )
    with pytest.raises(ForbiddenError):
        await svc.put(req)


@pytest.mark.asyncio
async def test_seed_bypass_reaches_repos(svc):
    # With skill_internal=True the guard passes and the (exploding) repo
    # access is reached — proving the bypass takes the normal path.
    req = DocumentPutRequest(
        vault="v", collection="overview", title="T", content="x",
        type="skill", slug="vault-skill",
    )
    with pytest.raises(AssertionError, match="guard must fire"):
        await svc.put(req, skill_internal=True)


@pytest.mark.asyncio
async def test_move_involving_overview_forbidden(svc, monkeypatch):
    # move() consults repos before computing the target, so give it a benign
    # fake that yields the canonical row, then expect the guard on the target.
    class FakeVaultRepo:
        async def get_id_by_name(self, name):
            return "11111111-1111-1111-1111-111111111111"

    class FakeDocRepo:
        async def find_by_ref(self, vault_id, ref):
            return {"path": "overview/vault-skill.md",
                    "id": __import__("uuid").uuid4(), "collection_id": None}

    async def _repos():
        return FakeVaultRepo(), FakeDocRepo(), None

    monkeypatch.setattr(svc, "_repos", _repos, raising=False)
    with pytest.raises(ForbiddenError):
        await svc.move("v", "overview/vault-skill.md", collection="notes")


@pytest.mark.asyncio
async def test_delete_canonical_forbidden(svc, monkeypatch):
    class FakeVaultRepo:
        async def get_id_by_name(self, name):
            return "11111111-1111-1111-1111-111111111111"

    class FakeDocRepo:
        async def find_by_ref(self, vault_id, ref):
            return {"path": "overview/vault-skill.md"}

    async def _repos():
        return FakeVaultRepo(), FakeDocRepo(), None

    monkeypatch.setattr(svc, "_repos", _repos, raising=False)
    with pytest.raises(ForbiddenError):
        await svc.delete("v", "overview/vault-skill.md")


# ── Version-cache invalidation on the legacy edit path ────────────────
# `update()` invalidates post-commit; `edit()` reaches the same document
# through a different lane (`_edit_locked`) and used to skip it entirely, so a
# skill authored with akb_edit stayed stale in every session for a full TTL.


def _edit_harness(monkeypatch, path: str):
    """A DocumentService whose edit() lane is stubbed down to the seam:
    resolve → lock → _edit_locked → (post-commit hook). Returns the spy list."""
    from contextlib import asynccontextmanager

    from app.services import vault_skill_service

    svc = DocumentService.__new__(DocumentService)
    seen: list[str] = []

    class FakeVaultRepo:
        async def get_id_by_name(self, name):
            return "11111111-1111-1111-1111-111111111111"

    class FakeDocRepo:
        async def find_by_ref(self, vault_id, ref):
            return {"path": path, "current_commit": "c0"}

        async def find_by_ref_with_conn(self, conn, vault_id, ref, for_update=False):
            return {"path": path, "current_commit": "c0"}

    async def _repos():
        return FakeVaultRepo(), FakeDocRepo(), None

    @asynccontextmanager
    async def _lock(vault_id, file_path, *, vault_name):
        # The invalidation must land AFTER this context exits (that is where
        # the transaction commits), never inside it.
        assert not seen, "invalidated before the commit"
        yield None

    async def _edit_locked(**kwargs):
        return "RESPONSE"

    monkeypatch.setattr(svc, "_repos", _repos, raising=False)
    monkeypatch.setattr(svc, "_path_lock", _lock, raising=False)
    monkeypatch.setattr(svc, "_edit_locked", _edit_locked, raising=False)
    monkeypatch.setattr(vault_skill_service, "invalidate", seen.append)
    return svc, seen


@pytest.mark.asyncio
async def test_edit_on_canonical_path_invalidates_version_cache(monkeypatch):
    svc, seen = _edit_harness(monkeypatch, "overview/vault-skill.md")
    assert await svc.edit(
        "v", "overview/vault-skill.md", "a", "b", skill_internal=True
    ) == "RESPONSE"
    assert seen == ["v"]


@pytest.mark.asyncio
async def test_edit_on_canonical_path_requires_owner_authorized_bypass(monkeypatch):
    svc, _ = _edit_harness(monkeypatch, "overview/vault-skill.md")
    with pytest.raises(ForbiddenError, match="vault owner"):
        await svc.edit("v", "overview/vault-skill.md", "a", "b")


@pytest.mark.asyncio
async def test_edit_elsewhere_does_not_invalidate(monkeypatch):
    svc, seen = _edit_harness(monkeypatch, "notes/a.md")
    assert await svc.edit("v", "notes/a.md", "a", "b") == "RESPONSE"
    assert seen == []
