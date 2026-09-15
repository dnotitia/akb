"""Upload capability: what the token grants, and that the bytes survive it.

Uploads used to go straight from the client to the object store against a
presigned signature. They now stream through this service, which means the
bytes are this service's responsibility for the first time — so the load
bearing assertion in this file is that what was sent is what gets stored,
byte for byte, on both the single-PUT and the multipart path.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid

import anyio
import pytest

from app.exceptions import AKBError, NotFoundError
from app.services import file_service as fs


_KEY = "team/deadbeef_report.bin"


class _FakeStore:
    """Enough of an object store to reassemble what was written to it."""

    def __init__(self, *, fail_part: int | None = None):
        self.objects: dict[str, bytes] = {}
        self.types: dict[str, str] = {}
        self.parts: dict[str, list[bytes]] = {}
        self.aborted: list[tuple[str, str]] = []
        self.completed: list[str] = []
        self.put_calls = 0
        self.created = 0
        self._fail_part = fail_part

    # --- primitives the service calls through asyncio.to_thread -----------
    def put_bytes(self, key, body, content_type="application/octet-stream"):
        self.put_calls += 1
        self.objects[key] = bytes(body)
        self.types[key] = content_type

    def multipart_create(self, key, content_type):
        self.created += 1
        upload_id = f"u{self.created}"
        self.parts[upload_id] = []
        self.types[key] = content_type
        return upload_id

    def multipart_part(self, key, upload_id, part_number, body):
        if self._fail_part == part_number:
            raise RuntimeError("object store refused the part")
        assert part_number == len(self.parts[upload_id]) + 1, "parts must be sequential"
        self.parts[upload_id].append(bytes(body))
        return f'"etag-{part_number}"'

    def multipart_complete(self, key, upload_id, parts):
        assert [p["PartNumber"] for p in parts] == list(
            range(1, len(self.parts[upload_id]) + 1)
        )
        self.objects[key] = b"".join(self.parts[upload_id])
        self.completed.append(upload_id)
        return {"ContentLength": len(self.objects[key])}

    def multipart_abort(self, key, upload_id):
        self.aborted.append((key, upload_id))
        self.parts.pop(upload_id, None)


def _never(message: str):
    """A stand-in that fails the test if the step it replaces ever runs."""

    async def _fail(*_args, **_kwargs):
        pytest.fail(message)

    return _fail


def _install(monkeypatch, store, *, part_size=None):
    for name in (
        "put_bytes", "multipart_create", "multipart_part",
        "multipart_complete", "multipart_abort",
    ):
        monkeypatch.setattr(fs.s3_adapter, name, getattr(store, name))
    if part_size is not None:
        monkeypatch.setattr(fs.s3_adapter, "MULTIPART_MIN_PART_BYTES", part_size)


async def _stream(data: bytes, chunk: int = 64 * 1024):
    for i in range(0, len(data), chunk):
        yield data[i:i + chunk]


async def _settled(predicate, timeout: float = 5.0) -> bool:
    """Wait for a cleanup that runs on a worker thread and is not awaited."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def test_a_body_that_fits_in_one_part_is_a_single_put(monkeypatch):
    """The common case must not pay for multipart.

    Most stored Files are small, so a create / part / complete round trip for
    each of them would be three calls where one is correct."""
    store = _FakeStore()
    _install(monkeypatch, store, part_size=1024)
    body = os.urandom(900)

    size = await fs.store_object_stream(
        _KEY, _stream(body), content_type="image/png", max_bytes=1 << 30,
    )

    assert size == len(body)
    assert store.objects[_KEY] == body
    assert store.put_calls == 1
    assert store.created == 0, "a body this size must not open a multipart upload"
    assert store.types[_KEY] == "image/png"


async def test_an_empty_body_stores_an_empty_object(monkeypatch):
    """A presigned PUT of nothing stored a zero-byte object. So does this."""
    store = _FakeStore()
    _install(monkeypatch, store, part_size=1024)

    size = await fs.store_object_stream(
        _KEY, _stream(b""), content_type="text/plain", max_bytes=1 << 30,
    )

    assert size == 0
    assert store.objects[_KEY] == b""
    assert store.created == 0


async def test_large_body_round_trips_byte_for_byte(monkeypatch):
    """The condition the whole change is judged on.

    Run against the real part size, with a body well past it so the multipart
    path is the one under test. An upload that silently loses, reorders or
    truncates bytes is data loss that only shows up when someone opens the
    file months later, so the assertion is the digest of the reassembled
    object against the digest of what was sent — not a length, and not a
    call count."""
    store = _FakeStore()
    _install(monkeypatch, store)
    body = os.urandom(17 * 1024 * 1024)
    sent = hashlib.sha256(body).hexdigest()

    size = await fs.store_object_stream(
        _KEY, _stream(body), content_type="application/pdf", max_bytes=1 << 30,
    )

    stored = store.objects[_KEY]
    assert size == len(body)
    assert len(stored) == len(body)
    assert hashlib.sha256(stored).hexdigest() == sent
    assert store.created == 1
    assert store.completed == ["u1"]
    assert store.put_calls == 0
    assert not store.aborted


async def test_a_body_landing_exactly_on_a_part_boundary_completes(monkeypatch):
    """The off-by-one that would leave a trailing empty part.

    A multipart upload whose final part is zero bytes is rejected by the
    store, and a body that is an exact multiple of the part size is the only
    way to reach that state."""
    store = _FakeStore()
    _install(monkeypatch, store, part_size=1024)
    body = os.urandom(4096)

    await fs.store_object_stream(
        _KEY, _stream(body, chunk=1024), content_type="application/octet-stream",
        max_bytes=1 << 30,
    )

    assert store.objects[_KEY] == body
    assert len(store.parts["u1"]) == 4
    assert all(part for part in store.parts["u1"]), "no part may be empty"


async def test_chunk_boundaries_do_not_shift_the_bytes(monkeypatch):
    """Whatever the ASGI server hands over, the object is the same.

    Chunk sizes are the transport's business and vary with the client, the
    proxy and the body. They must not change what gets stored."""
    store = _FakeStore()
    body = os.urandom(20_000)
    digests = set()
    for chunk in (1, 7, 1023, 1024, 4096, 19_999, 20_000):
        store = _FakeStore()
        _install(monkeypatch, store, part_size=1024)
        await fs.store_object_stream(
            _KEY, _stream(body, chunk=chunk),
            content_type="application/octet-stream", max_bytes=1 << 30,
        )
        digests.add(hashlib.sha256(store.objects[_KEY]).hexdigest())
    assert digests == {hashlib.sha256(body).hexdigest()}


async def test_oversize_body_is_refused_and_leaves_nothing_behind(monkeypatch):
    """Declared length can lie, so the streaming counter is the real bound.

    Fed in small chunks so the limit is crossed after parts are already in
    flight — a body that blows the limit inside its first chunk never opens a
    multipart upload and so proves nothing about cleaning one up."""
    store = _FakeStore()
    _install(monkeypatch, store, part_size=1024)

    with pytest.raises(AKBError) as exc:
        await fs.store_object_stream(
            _KEY, _stream(os.urandom(8192), chunk=1024),
            content_type="application/octet-stream", max_bytes=4096,
        )

    assert exc.value.status_code == 413
    assert _KEY not in store.objects
    assert await _settled(lambda: store.aborted == [(_KEY, "u1")]), store.aborted


async def test_a_failed_part_abandons_the_multipart(monkeypatch):
    """An abandoned multipart upload is storage nobody can see and everybody
    pays for: no listing shows it, and it is never garbage collected."""
    store = _FakeStore(fail_part=3)
    _install(monkeypatch, store, part_size=1024)

    with pytest.raises(RuntimeError, match="refused the part"):
        await fs.store_object_stream(
            _KEY, _stream(os.urandom(8192), chunk=1024),
            content_type="application/octet-stream", max_bytes=1 << 30,
        )

    assert await _settled(lambda: store.aborted == [(_KEY, "u1")]), store.aborted
    assert _KEY not in store.objects


async def test_a_cancelled_upload_still_abandons_the_multipart(monkeypatch):
    """Cancellation is the common case, not the exotic one — a browser tab
    closed mid-upload raises it — and it is the case an awaited cleanup
    silently skips.

    Cancelled through an anyio scope, because that is what actually cancels a
    request handler here: Starlette runs handlers under anyio, whose
    cancellation is level-triggered, so the *next* await inside the `except`
    block raises again before it can do anything. Plain `Task.cancel()` is
    edge-triggered and lets that await through, which makes it the wrong
    instrument for this assertion — it passes either way.
    """
    store = _FakeStore()
    _install(monkeypatch, store, part_size=1024)
    started = anyio.Event()

    async def _stalls():
        yield os.urandom(2048)
        started.set()
        await anyio.sleep(30)

    async def _upload():
        with pytest.raises(BaseException):
            await fs.store_object_stream(
                _KEY, _stalls(), content_type="application/octet-stream",
                max_bytes=1 << 30,
            )

    with anyio.CancelScope() as scope:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_upload)
            await started.wait()
            scope.cancel()

    assert await _settled(lambda: store.aborted == [(_KEY, "u1")]), store.aborted
    assert _KEY not in store.objects


# --- what the capability resolves to ------------------------------------


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return None


class _Conn:
    def __init__(self, row=None):
        self.row = row
        self.queries: list[tuple] = []

    async def fetchrow(self, sql, *params):
        self.queries.append((sql, params))
        return self.row


class _Pool:
    def __init__(self, row=None):
        self.conn = _Conn(row)

    def acquire(self):
        return _Context(self.conn)


async def _service(monkeypatch, row=None):
    monkeypatch.setattr(fs, "measurement_enabled", lambda: False)
    pool = _Pool(row)

    async def _pool():
        return pool

    monkeypatch.setattr(fs, "get_pool", _pool)
    return fs.FileService(), pool


async def test_a_malformed_token_never_reaches_the_database(monkeypatch):
    """This route is unauthenticated by design, so anything shaped wrong is
    refused before it can cost a connection or raise inside an encode."""
    service, pool = await _service(monkeypatch)

    for token in ("", "short", "has spaces in it!!!!!!!!", "토큰" * 10, "x" * 200):
        with pytest.raises(NotFoundError):
            await service.resolve_write_capability(token)

    assert pool.conn.queries == []


async def test_a_grant_without_an_object_key_is_refused(monkeypatch):
    """A measurement-lane PUT intent carries its bytes in the database and
    addresses no object store. Its token must not resolve to a key here — and
    the SQL, not the caller, is what excludes it."""
    service, pool = await _service(monkeypatch, row=None)

    with pytest.raises(NotFoundError):
        await service.resolve_write_capability("A" * 43)

    sql, _params = pool.conn.queries[0]
    assert "i.object_key IS NOT NULL" in sql
    assert "i.method = 'PUT'" in sql
    assert "i.expires_at > NOW()" in sql


async def test_the_grant_is_joined_to_a_file_that_still_exists(monkeypatch):
    """A presigned URL kept writing after its File was deleted, leaving bytes
    under a key nothing referenced. The capability cannot: the same query that
    resolves it requires the row to still be there."""
    service, pool = await _service(monkeypatch, row=None)

    with pytest.raises(NotFoundError):
        await service.resolve_write_capability("A" * 43)

    sql, _params = pool.conn.queries[0]
    assert "JOIN vault_files" in sql
    assert "f.id = i.file_id AND f.vault_id = i.vault_id" in sql


async def test_a_grant_resolves_to_its_key_and_the_stored_type(monkeypatch):
    """The type the bytes are stored under is the one AKB normalized when the
    capability was issued — not one the uploading client restates at PUT
    time, which is what let a caller pick how its bytes would later be
    served."""
    row = {
        "file_id": uuid.uuid4(),
        "vault_id": uuid.uuid4(),
        "object_key": _KEY,
        "mime_type": "IMAGE/PNG; charset=utf-8",
    }
    service, _pool = await _service(monkeypatch, row=row)

    grant = await service.resolve_write_capability("A" * 43)

    assert grant["object_key"] == _KEY
    assert grant["mime_type"] == "image/png"


async def test_the_token_is_stored_only_as_a_digest(monkeypatch):
    """The URL handed back is the only copy of the token that ever exists."""
    conn = _Conn()
    executed: list[tuple] = []

    async def _execute(*args):
        executed.append(args)

    conn.execute = _execute

    token = await fs._issue_write_capability(
        conn,
        file_id=uuid.uuid4(),
        vault_id=uuid.uuid4(),
        object_key=_KEY,
        filename="report.bin",
        mime_type="application/pdf",
        actor_id="alice",
    )

    _sql, *params = executed[0]
    assert token not in params
    assert hashlib.sha256(token.encode("ascii")).hexdigest() in params
    assert _KEY in params


# --- the adapter primitive the cleanup depends on ------------------------

def test_multipart_abort_swallows_its_own_failure(monkeypatch, caplog):
    """It runs while another failure is already being handled, so it must not
    become the failure the caller sees. It is also never awaited, so anything
    it raised would be lost anyway — better to log it where it happened."""
    from app.services.adapters import s3_adapter

    def _broken():
        raise RuntimeError("no route to the object store")

    monkeypatch.setattr(s3_adapter, "client", _broken)
    with caplog.at_level("WARNING"):
        assert s3_adapter.multipart_abort(_KEY, "u1") is None
    assert any("u1" in r.getMessage() for r in caplog.records)


# --- the URL a holder receives, and the route that answers it ------------

def test_upload_url_is_absolute_and_carries_no_locator(monkeypatch):
    """Consumers parse it with `new URL()` / `httpx.URL()`, so it has to be
    absolute; it carries no locator and no query because there is nothing to
    sign."""
    from app.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)
    token = fs._new_capability_token()
    url = fs._write_capability_url(token)

    assert url == f"https://akb.example.com/api/v1/files/upload/{token}"
    assert "?" not in url
    for leaked in ("X-Amz-", "akb-files"):
        assert leaked not in url, leaked


def _match(app, method: str, path: str):
    from starlette.routing import Match

    scope = {
        "type": "http", "method": method, "path": path,
        "headers": [], "query_string": b"", "root_path": "",
    }
    for route in app.routes:
        if route.matches(scope)[0] is Match.FULL:
            return route
    return None


@pytest.fixture(scope="module")
def app():
    from app.main import app as fastapi_app
    return fastapi_app


def test_upload_route_wins_over_the_vault_parameter(app):
    """Nothing collides with it today — a token cannot spell `upload` — but
    registration order is what keeps that true whatever a vault is called."""
    route = _match(app, "PUT", "/api/v1/files/upload/sometoken")
    assert route is not None
    assert route.path == "/api/v1/files/upload/{token}"


def test_reserving_an_upload_still_matches_its_own_route(app):
    """The neighbouring path differs only in which segment is the literal."""
    route = _match(app, "POST", "/api/v1/files/team/upload")
    assert route is not None
    assert route.path == "/api/v1/files/{vault}/upload"


def test_upload_route_is_not_in_the_public_schema(app):
    """The contract is the `upload_url` field. Publishing the path would
    invite callers to mint their own."""
    route = _match(app, "PUT", "/api/v1/files/upload/sometoken")
    assert getattr(route, "include_in_schema", True) is False


class _Request:
    def __init__(self, headers, body=b"", content_type=None):
        self.headers = dict(headers)
        if content_type is not None:
            self.headers["content-type"] = content_type
        self._body = body

    async def stream(self):
        yield self._body


async def test_an_oversized_declared_length_is_refused_before_any_body(monkeypatch):
    """Rejecting on the header saves transferring gigabytes that are going to
    be refused anyway. The streaming counter stays the authority, because a
    header can be absent or wrong."""
    from app.api.routes import files as route_mod
    from app.config import settings

    async def _grant(_token):
        return {"object_key": _KEY, "mime_type": "application/pdf"}

    monkeypatch.setattr(route_mod.file_service, "resolve_write_capability", _grant)
    monkeypatch.setattr(
        route_mod, "store_object_stream",
        _never("an oversized upload must not be read"),
    )
    monkeypatch.setattr(settings, "file_upload_max_bytes", 1024, raising=False)

    with pytest.raises(AKBError) as exc:
        await route_mod.upload_by_capability(
            "A" * 43, _Request({"content-length": "4096"}),
        )
    assert exc.value.status_code == 413


async def test_the_stored_type_is_the_grants_not_the_clients_header(monkeypatch):
    """A presigned PUT was signed for one content type and the client had to
    send it. Now that AKB writes the object, the type comes from the grant —
    so a caller cannot choose how its bytes will later be served."""
    from app.api.routes import files as route_mod
    from app.config import settings

    seen: dict = {}

    async def _grant(_token):
        return {"object_key": _KEY, "mime_type": "image/png"}

    async def _store(key, chunks, *, content_type, max_bytes):
        seen.update(key=key, content_type=content_type, max_bytes=max_bytes)
        return sum([len(c) async for c in chunks])

    monkeypatch.setattr(route_mod.file_service, "resolve_write_capability", _grant)
    monkeypatch.setattr(route_mod, "store_object_stream", _store)
    monkeypatch.setattr(settings, "file_upload_max_bytes", 4096, raising=False)

    response = await route_mod.upload_by_capability(
        "A" * 43,
        _Request({"content-length": "3"}, body=b"png", content_type="text/html"),
    )

    assert response.status_code == 200
    assert seen == {"key": _KEY, "content_type": "image/png", "max_bytes": 4096}
