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
