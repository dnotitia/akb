import pytest

from app.exceptions import ForbiddenError
from app.models.document import DocumentPutRequest
from app.services.native_document_service import NativeDocumentService


@pytest.fixture()
def svc(monkeypatch):
    s = NativeDocumentService.__new__(NativeDocumentService)

    async def _boom(*a, **k):
        raise AssertionError("guard must fire before vault lookup")

    monkeypatch.setattr(s, "_vault_id", _boom, raising=False)
    monkeypatch.setattr(s, "_current", _boom, raising=False)
    return s


@pytest.mark.asyncio
async def test_native_put_into_overview_forbidden(svc):
    req = DocumentPutRequest(
        vault="v", collection="overview", title="T", content="x", type="note",
    )
    with pytest.raises(ForbiddenError):
        await svc.put(req)


@pytest.mark.asyncio
async def test_native_put_skill_type_elsewhere_forbidden(svc):
    req = DocumentPutRequest(
        vault="v", collection="notes", title="T", content="x", type="skill",
    )
    with pytest.raises(ForbiddenError):
        await svc.put(req)


@pytest.mark.asyncio
async def test_native_put_internal_bypass(svc):
    req = DocumentPutRequest(
        vault="v", collection="overview", title="T", content="x",
        type="skill", slug="vault-skill",
    )
    with pytest.raises(AssertionError, match="guard must fire"):
        await svc.put(req, skill_internal=True)
