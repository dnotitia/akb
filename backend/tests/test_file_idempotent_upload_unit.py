"""Contract for content-addressed file keys and idempotent uploads.

An uploaded file's identity used to be the moment it was uploaded: `_s3_key`
prefixed every key with a fresh `uuid4`, so the one unique constraint the table
has — `UNIQUE(vault_id, s3_key)` — could never fire and the same bytes uploaded
twice produced two rows. `content_hash` is recorded but not unique, so nothing
downstream could tell the copies apart either.

These tests pin the fix: when the caller states the bytes up front, the key
becomes a function of the bytes and the second upload of the same artifact
resolves to the first one.

The pure-unit half covers key construction. The DB half runs against a real
Postgres (`AKB_TEST_DSN`, auto-skip otherwise) because the invariant under test
*is* a database constraint — asserting it against a mock would prove only that
the mock agrees with itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from starlette.requests import Request

from app.exceptions import AKBError
from app.repositories import vault_files_repo
from app.repositories.vault_repo import VaultRepository
from app.services.file_service import (
    FileService,
    _content_key_honors_hash,
    _s3_key,
)

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get(
    "AKB_TEST_DSN",
    "postgresql://akb:akb@localhost:15432/akb",  # pragma: allowlist secret
)

_BYTES_A = b"the same page, fetched twice"
_BYTES_B = b"a genuinely different page"
_HASH_A = hashlib.sha256(_BYTES_A).hexdigest()
_HASH_B = hashlib.sha256(_BYTES_B).hexdigest()


# ── key construction ─────────────────────────────────────────────


async def test_key_without_content_hash_keeps_the_random_legacy_shape():
    """No hash stated ⇒ unchanged historical behaviour: a fresh key per call.

    This is the backwards-compatibility guarantee. A caller that knows nothing
    about content addressing must see exactly what it saw before.
    """
    first = _s3_key("vault", "confluence/IS", "page.html")
    second = _s3_key("vault", "confluence/IS", "page.html")

    assert first != second
    assert first.startswith("vault/confluence/IS/")
    assert first.endswith("_page.html")
    prefix = first.rsplit("/", 1)[-1].removesuffix("_page.html")
    assert len(prefix) == 8 and int(prefix, 16) >= 0  # 8 random hex chars


async def test_same_bytes_same_name_produce_one_identical_key():
    """The whole point: identity comes from the artifact, not the clock."""
    first = _s3_key("vault", "confluence/IS", "page.html", content_hash=_HASH_A)
    second = _s3_key("vault", "confluence/IS", "page.html", content_hash=_HASH_A)

    assert first == second
    assert first == f"vault/confluence/IS/sha256-{_HASH_A[:16]}_page.html"


async def test_key_discriminates_content_name_and_collection():
    """Only a genuinely identical artifact collapses onto one key."""
    base = _s3_key("vault", "confluence/IS", "page.html", content_hash=_HASH_A)

    assert _s3_key("vault", "confluence/IS", "page.html", content_hash=_HASH_B) != base
    assert _s3_key("vault", "confluence/IS", "other.html", content_hash=_HASH_A) != base
    assert _s3_key("vault", "confluence/OPS", "page.html", content_hash=_HASH_A) != base
    assert _s3_key("other", "confluence/IS", "page.html", content_hash=_HASH_A) != base


async def test_root_collection_key_is_addressable_too():
    root = _s3_key("vault", "", "page.html", content_hash=_HASH_A)
    assert root == f"vault/sha256-{_HASH_A[:16]}_page.html"
    assert root == _s3_key("vault", "", "page.html", content_hash=_HASH_A)


async def test_the_key_carries_the_content_hash_verbatim_for_operators():
    """An operator holding a `vault_files.content_hash` can pair it to a key.

    Not decoration: the reported defect included an orphan-retirement step that
    was unsatisfiable precisely because no key said anything about its bytes.
    """
    key = _s3_key("vault", "coll", "page.html", content_hash=_HASH_A)
    assert _HASH_A[:16] in key
    assert _HASH_A.startswith(key.rsplit("sha256-", 1)[-1].split("_", 1)[0])


# ── the key's claim is enforced, not trusted ─────────────────────


async def test_content_key_honors_only_the_hash_it_was_built_from():
    key = _s3_key("vault", "coll", "page.html", content_hash=_HASH_A)

    assert _content_key_honors_hash(key, _HASH_A) is True
    assert _content_key_honors_hash(key, _HASH_B) is False


async def test_legacy_random_keys_make_no_claim_and_always_pass():
    """Existing rows must keep confirming. A random key asserts nothing."""
    legacy = _s3_key("vault", "coll", "page.html")

    assert _content_key_honors_hash(legacy, _HASH_A) is True
    assert _content_key_honors_hash(legacy, _HASH_B) is True


async def test_a_filename_that_mimics_the_marker_cannot_forge_a_claim():
    """The marker is positional — it is the key's prefix, not any substring."""
    key = _s3_key("vault", "coll", f"sha256-{_HASH_B[:16]}_decoy.html")

    assert key.rsplit("/", 1)[-1].startswith("sha256-") is False
    assert _content_key_honors_hash(key, _HASH_A) is True


async def test_initiate_upload_rejects_a_malformed_content_hash():
    """Same 400 and same message `confirm_upload` already raises.

    Unreachable for a caller that does not send the new parameter.
    """
    with pytest.raises(AKBError) as err:
        await FileService().initiate_upload(
            vault_name="vault",
            vault_id=uuid.uuid4(),
            collection="coll",
            filename="page.html",
            actor_id="tester",
            content_hash="not-a-sha256",
        )
    assert err.value.status_code == 400


# ── the constraint itself, against a real Postgres ───────────────


async def _can_connect(dsn: str) -> bool:
    try:
        conn = await asyncpg.connect(dsn, timeout=2)
    except (OSError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


@pytest_asyncio.fixture
async def pool():
    if not await _can_connect(_DSN):
        pytest.skip(f"Postgres not reachable at {_DSN}")
    pool = await asyncpg.create_pool(dsn=_DSN, min_size=1, max_size=6)
    backend_dir = Path(__file__).resolve().parents[1]
    init_sql = (backend_dir / "app" / "db" / "init.sql").read_text()
    async with pool.acquire() as conn:
        await conn.execute(init_sql)
    # `events` (migration 015) and `s3_delete_outbox` (019) are not in
    # init.sql but are part of the confirm/cleanup contract. Idempotent.
    import importlib.util
    for mig_name in ("015_events_outbox.py", "019_s3_delete_outbox.py"):
        mig_path = backend_dir / "app" / "db" / "migrations" / mig_name
        spec = importlib.util.spec_from_file_location(mig_name, str(mig_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        async with pool.acquire() as conn:
            await module.migrate(conn=conn)
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def vault_id(pool):
    vault_repo = VaultRepository(pool)
    name = f"_test_idempotent_upload_{uuid.uuid4().hex[:8]}"
    vid = await vault_repo.create(
        name=name,
        description="ephemeral test vault",
        git_path=f"/tmp/{name}.git",
        owner_id=None,
    )
    try:
        yield vid
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM vaults WHERE id = $1", vid)


async def _put(conn, vault_id, s3_key, *, name="page.html", collection_id=None,
               description="", mime_type="text/html") -> uuid.UUID:
    return await vault_files_repo.insert_or_adopt(
        conn,
        file_id=uuid.uuid4(), vault_id=vault_id, name=name,
        s3_key=s3_key, mime_type=mime_type, size_bytes=0,
        description=description, created_by="tester",
        collection_id=collection_id,
    )


async def _row_count(conn, vault_id) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM vault_files WHERE vault_id = $1", vault_id,
    )


async def test_same_bytes_same_name_twice_is_one_row(pool, vault_id):
    key = _s3_key("v", "IS", "page.html", content_hash=_HASH_A)
    async with pool.acquire() as conn:
        first = await _put(conn, vault_id, key)
        second = await _put(conn, vault_id, key)

        assert second == first, "the second upload must adopt the first row"
        assert await _row_count(conn, vault_id) == 1


async def test_different_bytes_same_name_is_two_rows(pool, vault_id):
    """The random prefix protected *this*: same name, genuinely different file.

    Content addressing keeps that protection — nothing is overwritten in place
    — which is why keying on `(vault, collection, name)` alone was rejected.
    """
    async with pool.acquire() as conn:
        a = await _put(conn, vault_id, _s3_key("v", "IS", "page.html", content_hash=_HASH_A))
        b = await _put(conn, vault_id, _s3_key("v", "IS", "page.html", content_hash=_HASH_B))

        assert a != b
        assert await _row_count(conn, vault_id) == 2


async def test_different_bytes_different_name_is_two_rows(pool, vault_id):
    async with pool.acquire() as conn:
        await _put(conn, vault_id, _s3_key("v", "IS", "a.html", content_hash=_HASH_A), name="a.html")
        await _put(conn, vault_id, _s3_key("v", "IS", "b.html", content_hash=_HASH_B), name="b.html")

        assert await _row_count(conn, vault_id) == 2


async def test_same_bytes_different_name_stay_separate_files(pool, vault_id):
    """Each caller keeps the name it asked for; dedupe never renames a file."""
    async with pool.acquire() as conn:
        await _put(conn, vault_id, _s3_key("v", "IS", "a.html", content_hash=_HASH_A), name="a.html")
        await _put(conn, vault_id, _s3_key("v", "IS", "b.html", content_hash=_HASH_A), name="b.html")

        assert await _row_count(conn, vault_id) == 2
        names = await conn.fetch(
            "SELECT name FROM vault_files WHERE vault_id = $1 ORDER BY name", vault_id,
        )
        assert [r["name"] for r in names] == ["a.html", "b.html"]


async def test_without_a_stated_hash_the_duplicate_still_happens(pool, vault_id):
    """The defect, reproduced — and left alone for callers that do not opt in.

    Two rows for one artifact is what the old path does, and this fix does not
    change it for a caller that cannot state its bytes up front.
    """
    async with pool.acquire() as conn:
        await _put(conn, vault_id, _s3_key("v", "IS", "page.html"))
        await _put(conn, vault_id, _s3_key("v", "IS", "page.html"))

        assert await _row_count(conn, vault_id) == 2


async def test_adopt_leaves_first_writer_metadata_alone(pool, vault_id):
    key = _s3_key("v", "IS", "page.html", content_hash=_HASH_A)
    async with pool.acquire() as conn:
        first = await _put(conn, vault_id, key, description="original")
        await _put(conn, vault_id, key, description="second attempt")

        row = await conn.fetchrow(
            "SELECT description FROM vault_files WHERE id = $1", first,
        )
        assert row["description"] == "original"


async def test_adopt_reattaches_a_collection_that_was_dropped(pool, vault_id):
    """`collections.id ON DELETE SET NULL` can strand a row at vault root.

    Re-uploading to the collection the key encodes must put the row back where
    it says it lives, rather than reporting a collection the row is not in.
    """
    key = _s3_key("v", "IS", "page.html", content_hash=_HASH_A)
    async with pool.acquire() as conn:
        coll_id = await conn.fetchval(
            "INSERT INTO collections (vault_id, path, name) VALUES ($1,$2,$3) RETURNING id",
            vault_id, "IS", "IS",
        )
        file_id = await _put(conn, vault_id, key, collection_id=coll_id)
        await conn.execute(
            "UPDATE vault_files SET collection_id = NULL WHERE id = $1", file_id,
        )

        adopted = await _put(conn, vault_id, key, collection_id=coll_id)

        assert adopted == file_id
        assert await conn.fetchval(
            "SELECT collection_id FROM vault_files WHERE id = $1", file_id,
        ) == coll_id


@pytest_asyncio.fixture
async def confirmable(pool, monkeypatch):
    """Wire `confirm_upload` to the test pool with a fake object store.

    Returns a helper that stores `body` at `s3_key` and confirms `file_id`,
    so each test states only the bytes actually sitting in storage.
    """
    from app.services import file_service as fs

    async def _get_pool():
        return pool

    monkeypatch.setattr(fs, "get_pool", _get_pool)
    monkeypatch.setattr(fs.s3_adapter, "ensure_bucket", lambda _b: None)

    async def _noop_index(*_args, **_kwargs):
        return None

    monkeypatch.setattr(fs, "index_file_metadata", _noop_index)

    async def confirm(vault_id, file_id, s3_key, body, **kwargs):
        monkeypatch.setattr(
            fs.s3_adapter, "head",
            lambda key: {"ContentLength": len(body), "ETag": '"etag"'},
        )
        monkeypatch.setattr(
            fs.s3_adapter, "iter_chunks",
            lambda key, **_kw: iter([body]),
        )
        return await fs.FileService().confirm_upload(
            vault_id, str(file_id), actor_id="tester", **kwargs,
        )

    return confirm


async def test_confirm_certifies_bytes_that_match_the_key_they_are_under(
    pool, vault_id, confirmable,
):
    key = _s3_key("v", "IS", "page.html", content_hash=_HASH_A)
    async with pool.acquire() as conn:
        file_id = await _put(conn, vault_id, key)

    result = await confirmable(vault_id, file_id, key, _BYTES_A)

    assert result["content_hash"] == _HASH_A
    async with pool.acquire() as conn:
        assert await _row_count(conn, vault_id) == 1


async def test_confirm_rejects_bytes_stored_under_another_content_key(
    pool, vault_id, confirmable,
):
    """The key states a hash; storage holds something else ⇒ 409 + cleanup.

    Without this the claim at `initiate_upload` would be unenforced whenever
    the caller simply omits `content_hash` here: a writer could park arbitrary
    bytes on some other content's key and the next caller's idempotent upload
    would adopt them as its own.
    """
    key = _s3_key("v", "IS", "page.html", content_hash=_HASH_A)
    async with pool.acquire() as conn:
        file_id = await _put(conn, vault_id, key)

    with pytest.raises(AKBError) as err:
        await confirmable(vault_id, file_id, key, _BYTES_B)  # no content_hash

    assert err.value.status_code == 409
    async with pool.acquire() as conn:
        assert await _row_count(conn, vault_id) == 0, "the bad row must be cleaned up"
        assert await conn.fetchval("SELECT count(*) FROM s3_delete_outbox") >= 1


async def test_confirm_on_a_legacy_key_is_unchanged(pool, vault_id, confirmable):
    """A random key claims nothing, so any bytes confirm — as they always did."""
    key = _s3_key("v", "IS", "page.html")
    async with pool.acquire() as conn:
        file_id = await _put(conn, vault_id, key)

    result = await confirmable(vault_id, file_id, key, _BYTES_B)

    assert result["content_hash"] == _HASH_B


async def test_confirm_still_rejects_a_mismatched_client_hash(
    pool, vault_id, confirmable,
):
    """Pre-existing 409 path, untouched."""
    key = _s3_key("v", "IS", "page.html")
    async with pool.acquire() as conn:
        file_id = await _put(conn, vault_id, key)

    with pytest.raises(AKBError) as err:
        await confirmable(vault_id, file_id, key, _BYTES_B, content_hash=_HASH_A)

    assert err.value.status_code == 409


async def test_upload_route_forwards_content_hash_and_defaults_it_off(monkeypatch):
    """The parameter has to survive the HTTP boundary, and default to absent.

    Without the default, every existing caller of `POST /files/{vault}/upload`
    would change behaviour; without the forwarding, the feature is unreachable.
    """
    from app.api.routes import files

    seen: list[dict] = []

    async def _access(*_args, **_kwargs):
        return {"vault_id": uuid.uuid4(), "role": "writer", "role_source": "member"}

    async def _initiate(**kwargs):
        seen.append(kwargs)
        return {"uri": "akb://team/file/f-1"}

    monkeypatch.setattr(files, "check_vault_access", _access)
    monkeypatch.setattr(files.file_service, "initiate_upload", _initiate)

    async def _call(**overrides):
        await files.upload_file(
            request=Request({"type": "http", "method": "POST", "path": "/", "headers": []}),
            vault="team", filename="page.html", collection="IS",
            description="", mime_type="text/html",
            user=_service_user(), **overrides,
        )

    await _call(content_hash=_HASH_A)
    await _call(content_hash=None)

    assert seen[0]["content_hash"] == _HASH_A
    assert seen[1]["content_hash"] is None


def _service_user():
    from app.services.auth_service import AuthenticatedUser

    return AuthenticatedUser(
        user_id=str(uuid.uuid4()), username="tester", email="t@example.test",
        display_name=None, is_admin=False, auth_method="pat",
        token_id=None, key_class=None, token_scopes=None,
    )


async def test_concurrent_uploads_of_the_same_artifact_settle_on_one_row(pool, vault_id):
    """Two writers, same artifact, overlapping transactions ⇒ still one row.

    `insert_or_adopt` uses `ON CONFLICT ... DO UPDATE`, the form Postgres
    guarantees to be an atomic insert-or-update: the loser waits on the
    winner's uncommitted insert and is then handed the winner's id. `DO
    NOTHING` would return no row here and force a SELECT that races.
    """
    key = _s3_key("v", "IS", "page.html", content_hash=_HASH_A)

    async def writer(hold: float) -> uuid.UUID:
        async with pool.acquire() as conn:
            async with conn.transaction():
                stored = await _put(conn, vault_id, key)
                await asyncio.sleep(hold)  # keep the transaction open
                return stored

    first, second = await asyncio.gather(writer(0.4), writer(0.0))

    assert first == second
    async with pool.acquire() as conn:
        assert await _row_count(conn, vault_id) == 1
