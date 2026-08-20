import asyncio

import pytest

from app.services import vault_skill_service as vss


@pytest.fixture(autouse=True)
def _reset():
    vss.reset()
    yield
    vss.reset()


def _fake_fetch(content="# skill body", version="abc12345"):
    async def fetch(vault, vault_id=None):
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
    async def fetch(vault, vault_id=None):
        return None  # mirror vault / absent
    monkeypatch.setattr(vss, "_fetch_skill", fetch)
    assert await vss.injection_payload("s", "mirror") is None


@pytest.mark.asyncio
async def test_fetch_error_injects_nothing(monkeypatch):
    async def fetch(vault, vault_id=None):
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
async def test_name_reuse_identity_mismatch_never_reads_the_new_vault(monkeypatch):
    import uuid

    import app.db.postgres as pg
    import app.repositories.vault_external_git_repo as veg
    import app.repositories.vault_repo as vr
    import app.services.revision_backend as rb

    new_id = uuid.uuid4()

    async def fake_pool():
        return "POOL"

    class FakeVaultRepo:
        def __init__(self, pool):
            pass

        async def get_id_by_name(self, name):
            return new_id

    class FakeExtRepo:
        def __init__(self, pool):
            pass

        async def exists(self, vid):
            raise AssertionError("identity mismatch must stop before mirror/doc reads")

    def never_document_service():
        raise AssertionError("identity mismatch must not read the new vault")

    monkeypatch.setattr(pg, "get_pool", fake_pool)
    monkeypatch.setattr(vr, "VaultRepository", FakeVaultRepo)
    monkeypatch.setattr(veg, "VaultExternalGitRepository", FakeExtRepo)
    monkeypatch.setattr(rb, "get_document_service", never_document_service)

    assert await vss._fetch_skill("reused-name", str(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_recreate_during_document_read_discards_replacement_body(monkeypatch):
    import uuid
    from types import SimpleNamespace

    import app.db.postgres as pg
    import app.repositories.vault_external_git_repo as veg
    import app.repositories.vault_repo as vr
    import app.services.revision_backend as rb

    old_id, new_id = uuid.uuid4(), uuid.uuid4()
    ids = iter((old_id, new_id))

    async def fake_pool():
        return "POOL"

    class FakeVaultRepo:
        def __init__(self, pool):
            pass

        async def get_id_by_name(self, name):
            return next(ids)

    class FakeExtRepo:
        def __init__(self, pool):
            pass

        async def exists(self, vid):
            return False

    class FakeDocs:
        async def get(self, vault, path):
            return SimpleNamespace(
                content="NEW VAULT PRIVATE GUIDE",
                content_hash="a" * 64,
                current_commit="b" * 40,
            )

    monkeypatch.setattr(pg, "get_pool", fake_pool)
    monkeypatch.setattr(vr, "VaultRepository", FakeVaultRepo)
    monkeypatch.setattr(veg, "VaultExternalGitRepository", FakeExtRepo)
    monkeypatch.setattr(rb, "get_document_service", lambda: FakeDocs())

    assert await vss._fetch_skill("reused-name", str(old_id)) is None


@pytest.mark.asyncio
async def test_session_map_bounded(monkeypatch):
    monkeypatch.setattr(vss, "_fetch_skill", _fake_fetch())
    monkeypatch.setattr(vss, "_SESSION_MAP_MAX", 3)
    for i in range(5):
        await vss.injection_payload(f"s{i}", "v1")
    assert len(vss._session_map) <= 3


@pytest.mark.asyncio
async def test_vault_cache_bounded(monkeypatch):
    """Negative entries are the cheap ones to create, so bound on those."""
    async def absent(vault, vault_id=None):
        return None
    monkeypatch.setattr(vss, "_fetch_skill", absent)
    monkeypatch.setattr(vss, "_VAULT_CACHE_MAX", 3)
    for i in range(20):
        await vss.injection_payload("s", f"v{i}")
    assert len(vss._vault_cache) <= 3
    # LRU, not "clear when full": the most recent misses are what survives.
    assert ("v19", None) in vss._vault_cache


@pytest.mark.asyncio
async def test_overlong_vault_name_never_reaches_the_cache(monkeypatch):
    """The clamp must not depend on the authorization coupling upstream."""
    calls: list[str] = []

    async def counting(vault, vault_id=None):
        calls.append(vault)
        return {"content": "x", "version": "v"}

    monkeypatch.setattr(vss, "_fetch_skill", counting)
    assert await vss.injection_payload("s", "n" * 257) is None
    assert calls == []
    assert vss._vault_cache == {}


@pytest.mark.asyncio
async def test_concurrent_misses_single_flight(monkeypatch):
    """N concurrent first-touches on one vault produce ONE fetch."""
    started = 0
    release = asyncio.Event()

    async def slow(vault, vault_id=None):
        nonlocal started
        started += 1
        await release.wait()
        return {"content": "# body", "version": "abc12345"}

    monkeypatch.setattr(vss, "_fetch_skill", slow)

    tasks = [
        asyncio.create_task(vss.injection_payload(f"s{i}", "v1")) for i in range(8)
    ]
    await asyncio.sleep(0)  # let every task reach the fetch
    release.set()
    results = await asyncio.gather(*tasks)

    assert started == 1
    # Distinct sessions, so every waiter still gets its own first-touch payload.
    assert sum(1 for r in results if r is not None) == 8


def _blocking_fetch(release: asyncio.Event, started: asyncio.Event):
    async def slow(vault, vault_id=None):
        started.set()
        await release.wait()
        return {"content": "# body", "version": "abc12345"}
    return slow


async def _park_follower(vault: str):
    """Start a follower and let it reach the `shield` await."""
    task = asyncio.create_task(vss.injection_payload("follower", vault))
    for _ in range(3):
        await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_cancelled_leader_hands_followers_a_miss(monkeypatch):
    """A cancelled leader must not abort the tool calls waiting behind it."""
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(vss, "_fetch_skill", _blocking_fetch(release, started))

    leader = asyncio.create_task(vss.injection_payload("leader", "v1"))
    await started.wait()
    follower = await _park_follower("v1")
    assert ("v1", None) in vss._pending

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    # The follower completes normally — "no injection this time", not a crash.
    assert await follower is None
    assert vss._vault_cache == {}
    assert vss._pending == {}

    # Nothing was cached, so the next call is a fresh fetch.
    release.set()
    monkeypatch.setattr(vss, "_fetch_skill", _fake_fetch())
    assert await vss.injection_payload("later", "v1") is not None


@pytest.mark.asyncio
async def test_cancelled_follower_propagates_and_spares_the_leader(monkeypatch):
    """A follower's OWN cancellation is its caller's — it must still raise.

    The two cases are told apart by the shared future's state: a leader
    outcome always arrives as a COMPLETED future (result or exception), while
    the follower's own cancellation raises out of `shield` with the inner
    future untouched — so the leader's fetch survives it.
    """
    started, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(vss, "_fetch_skill", _blocking_fetch(release, started))

    leader = asyncio.create_task(vss.injection_payload("leader", "v1"))
    await started.wait()
    follower = await _park_follower("v1")
    fut = vss._pending[("v1", None)]

    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower

    # `shield` kept the shared fetch alive: not done, not cancelled.
    assert not fut.done()
    release.set()
    assert await leader is not None
    assert fut.result() == ("abc12345", "# body")


@pytest.mark.asyncio
async def test_fetch_timeout_yields_none_and_caches_nothing(monkeypatch):
    """A transient stall must not become a TTL-long negative entry."""
    monkeypatch.setattr(vss, "_FETCH_TIMEOUT", 0.01)
    hung = asyncio.Event()

    async def hang(vault, vault_id=None):
        await hung.wait()
        return {"content": "# body", "version": "abc12345"}

    monkeypatch.setattr(vss, "_fetch_skill", hang)
    assert await vss.injection_payload("s", "v1") is None
    assert vss._vault_cache == {}
    assert vss._pending == {}
    hung.set()

    # The next call is a fresh attempt, not a cache hit on the failure.
    monkeypatch.setattr(vss, "_fetch_skill", _fake_fetch())
    assert await vss.injection_payload("s", "v1") is not None


@pytest.mark.asyncio
async def test_invalidation_during_fetch_cannot_restore_stale_body(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    bodies = iter(("OLD PRIVATE GUIDE", "NEW GUIDE"))

    async def fetch(vault, vault_id=None):
        body = next(bodies)
        if body.startswith("OLD"):
            started.set()
            await release.wait()
        return {"content": body, "version": body[:3]}

    monkeypatch.setattr(vss, "_fetch_skill", fetch)
    first = asyncio.create_task(vss.injection_payload("s", "v1", "id-1"))
    await started.wait()
    vss.invalidate("v1")
    release.set()

    assert await first is None
    fresh = await vss.injection_payload("s", "v1", "id-1")
    assert fresh is not None
    assert fresh["body"] == "NEW GUIDE"


@pytest.mark.asyncio
async def test_same_name_different_vault_id_has_separate_session_state(monkeypatch):
    monkeypatch.setattr(vss, "_fetch_skill", _fake_fetch())
    assert await vss.injection_payload("s", "v1", "old-id") is not None
    assert await vss.injection_payload("s", "v1", "old-id") is None
    assert await vss.injection_payload("s", "v1", "new-id") is not None
