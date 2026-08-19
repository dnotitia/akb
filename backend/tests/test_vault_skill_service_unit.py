import pytest

from app.services import vault_skill_service as vss


@pytest.fixture(autouse=True)
def _reset():
    vss.reset()
    yield
    vss.reset()


def _fake_fetch(content="# skill body", version="abc12345"):
    async def fetch(vault):
        return {"content": content, "version": version}
    return fetch


@pytest.mark.asyncio
async def test_first_touch_injects(monkeypatch):
    monkeypatch.setattr(vss, "_fetch_skill", _fake_fetch())
    p = await vss.injection_payload("sess1", "v1")
    assert p is not None
    assert p["reason"] == "first_touch"
    assert p["vault"] == "v1"
    assert p["body"] == "# skill body"
    assert p["truncated"] is False


@pytest.mark.asyncio
async def test_same_version_not_reinjected(monkeypatch):
    monkeypatch.setattr(vss, "_fetch_skill", _fake_fetch())
    assert await vss.injection_payload("sess1", "v1") is not None
    assert await vss.injection_payload("sess1", "v1") is None


@pytest.mark.asyncio
async def test_version_change_reinjects(monkeypatch):
    monkeypatch.setattr(vss, "_fetch_skill", _fake_fetch(version="v-old"))
    assert (await vss.injection_payload("s", "v1"))["reason"] == "first_touch"
    monkeypatch.setattr(vss, "_fetch_skill", _fake_fetch(version="v-new"))
    vss.invalidate("v1")  # simulate the write-through hook
    p = await vss.injection_payload("s", "v1")
    assert p is not None and p["reason"] == "updated"


@pytest.mark.asyncio
async def test_missing_skill_injects_nothing(monkeypatch):
    async def fetch(vault):
        return None  # mirror vault / absent
    monkeypatch.setattr(vss, "_fetch_skill", fetch)
    assert await vss.injection_payload("s", "mirror") is None


@pytest.mark.asyncio
async def test_fetch_error_injects_nothing(monkeypatch):
    async def fetch(vault):
        raise RuntimeError("db down")
    monkeypatch.setattr(vss, "_fetch_skill", fetch)
    assert await vss.injection_payload("s", "v1") is None  # never raises


@pytest.mark.asyncio
async def test_body_clipped(monkeypatch):
    big = "x" * (vss._BODY_MAX + 100)
    monkeypatch.setattr(vss, "_fetch_skill", _fake_fetch(content=big))
    p = await vss.injection_payload("s", "v1")
    assert p["truncated"] is True
    assert len(p["body"].encode("utf-8")) <= vss._BODY_MAX + 200  # clip + pointer suffix


@pytest.mark.asyncio
async def test_mirror_vault_never_injected_or_fetched(monkeypatch):
    """Mirror exclusion is a security control, so exercise the REAL
    `_fetch_skill` — every other test stubs it away. An external-git mirror
    must yield no payload AND must never reach the document read: upstream
    markdown may not enter agent context through the automatic channel.
    """
    import uuid

    import app.db.postgres as pg
    import app.repositories.vault_external_git_repo as veg
    import app.repositories.vault_repo as vr
    import app.services.revision_backend as rb

    vault_id = uuid.uuid4()

    async def fake_pool():
        return "POOL"

    class FakeVaultRepo:
        def __init__(self, pool):
            pass

        async def get_id_by_name(self, name):
            return vault_id

    class FakeExtRepo:
        def __init__(self, pool):
            pass

        async def exists(self, vid):
            assert vid == vault_id
            return True

    def _never():
        raise AssertionError("mirror vault must not reach the document service")

    monkeypatch.setattr(pg, "get_pool", fake_pool)
    monkeypatch.setattr(vr, "VaultRepository", FakeVaultRepo)
    monkeypatch.setattr(veg, "VaultExternalGitRepository", FakeExtRepo)
    monkeypatch.setattr(rb, "get_document_service", _never)

    assert await vss._fetch_skill("mirror") is None
    assert await vss.injection_payload("s", "mirror") is None


@pytest.mark.asyncio
async def test_session_map_bounded(monkeypatch):
    monkeypatch.setattr(vss, "_fetch_skill", _fake_fetch())
    monkeypatch.setattr(vss, "_SESSION_MAP_MAX", 3)
    for i in range(5):
        await vss.injection_payload(f"s{i}", "v1")
    assert len(vss._session_map) <= 3
