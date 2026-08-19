"""Reserved-namespace guards on the non-document write surfaces.

Task 4 of the vault-skill reservation: deleting the `overview` collection
would cascade the vault-skill document away, and files/tables must not be
creatable inside the reserved namespace.

Each test proves the guard fires *before* any I/O, so the refusal does not
depend on a database being reachable.
"""

import uuid

import pytest

from app.exceptions import ForbiddenError
from app.services.collection_service import CollectionService


@pytest.mark.asyncio
async def test_collection_delete_overview_forbidden(monkeypatch):
    svc = CollectionService.__new__(CollectionService)

    async def _boom(*a, **k):
        raise AssertionError("guard must fire before repo access")

    monkeypatch.setattr(svc, "_repos", _boom, raising=False)
    with pytest.raises(ForbiddenError):
        await svc.delete(vault="v", path="overview", recursive=True, agent_id=None)
    with pytest.raises(ForbiddenError):
        await svc.delete(vault="v", path="overview/sub", recursive=False, agent_id=None)


@pytest.mark.asyncio
async def test_collection_create_under_overview_forbidden(monkeypatch):
    """Creation must be refused too, or the delete guard traps the result.

    `akb_create_collection(path="overview/junk")` used to succeed, and
    `check_collection_delete` then made the row permanently undeletable.
    """
    svc = CollectionService.__new__(CollectionService)

    async def _boom(*a, **k):
        raise AssertionError("guard must fire before repo access")

    monkeypatch.setattr(svc, "_repos", _boom, raising=False)
    for path in ("overview", "overview/junk", "/overview/", "overview/a/b"):
        with pytest.raises(ForbiddenError):
            await svc.create(vault="v", path=path, summary=None, agent_id=None)


@pytest.mark.asyncio
async def test_create_table_under_overview_forbidden():
    from app.services.table_service import create_table
    with pytest.raises(ForbiddenError):
        await create_table(
            uuid.uuid4(), "t1", [{"name": "c", "type": "text"}],
            actor_id="u", collection="overview",
        )


@pytest.mark.asyncio
async def test_initiate_upload_under_overview_forbidden():
    """Both file backends are covered.

    `FileService.initiate_upload` delegates to the native measurement
    backend before it normalizes `collection`, so a guard placed at the
    normalization site would leave that backend unreserved. The stub here
    fails loudly if the delegation is ever reached for a reserved path.
    """
    from app.services.file_service import FileService

    class _BoomMeasurement:
        async def initiate_upload(self, **kwargs):
            raise AssertionError("guard must fire before backend delegation")

    for measurement in (None, _BoomMeasurement()):
        svc = FileService.__new__(FileService)
        svc._bucket = "test-bucket"
        svc._measurement = measurement
        with pytest.raises(ForbiddenError):
            await svc.initiate_upload(
                "v", uuid.uuid4(), "overview", "a.txt", actor_id="u",
            )
        with pytest.raises(ForbiddenError):
            await svc.initiate_upload(
                "v", uuid.uuid4(), "overview/sub", "a.txt", actor_id="u",
            )
