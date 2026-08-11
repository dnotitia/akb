from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.exceptions import AKBError, NotFoundError
from app.services.asset_service import claim_document_assets, extract_asset_ids, inspect_image


ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_inspect_image_uses_bytes_and_reads_dimensions() -> None:
    assert inspect_image(ONE_PIXEL_PNG) == ("image/png", 1, 1)


def test_inspect_image_rejects_truncated_or_unknown_content() -> None:
    with pytest.raises(AKBError) as truncated:
        inspect_image(ONE_PIXEL_PNG[:-12])
    assert truncated.value.status_code == 415

    with pytest.raises(AKBError) as unknown:
        inspect_image(b"<svg><script>alert(1)</script></svg>")
    assert unknown.value.status_code == 415

    corrupt = bytearray(ONE_PIXEL_PNG)
    corrupt[-16] ^= 0xFF  # corrupt the IDAT CRC while preserving PNG/IEND markers
    with pytest.raises(AKBError) as bad_crc:
        inspect_image(bytes(corrupt))
    assert bad_crc.value.status_code == 415


def test_safe_filename_uses_supported_format_metadata() -> None:
    from app.services import asset_service

    assert asset_service._safe_filename("\x7f", "image/webp") == "image.webp"
    assert asset_service._safe_filename(".", "image/png") == "image.png"
    assert asset_service._safe_filename("..", "image/jpeg") == "image.jpg"


@pytest.mark.asyncio
async def test_cancelled_upload_waits_for_decoder_before_releasing_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import asset_service

    decode_started = threading.Event()
    finish_decode = threading.Event()
    storage_touched = False

    def blocking_inspect(_body):
        decode_started.set()
        finish_decode.wait(timeout=2)
        return "image/png", 1, 1

    def unexpected_storage(*_args):
        nonlocal storage_touched
        storage_touched = True
        raise AssertionError("cancelled inspection must not reach storage")

    monkeypatch.setattr(asset_service, "measurement_enabled", lambda: False)
    monkeypatch.setattr(asset_service, "inspect_image", blocking_inspect)
    monkeypatch.setattr(asset_service.s3_adapter, "ensure_bucket", unexpected_storage)

    upload = asyncio.create_task(asset_service.create_image_asset(
        vault_id=uuid.uuid4(),
        vault_name="team",
        filename="diagram.png",
        declared_mime="image/png",
        body=ONE_PIXEL_PNG,
        actor_id="alice",
    ))
    assert await asyncio.to_thread(decode_started.wait, 1)
    upload.cancel()
    await asyncio.sleep(0)
    assert not upload.done()

    finish_decode.set()
    with pytest.raises(asyncio.CancelledError):
        await upload
    assert storage_touched is False


def test_extract_asset_ids_is_conservative_around_code() -> None:
    visible = uuid.uuid4()
    titled = uuid.uuid4()
    referenced = uuid.uuid4()
    fenced = uuid.uuid4()
    inline = uuid.uuid4()
    ordinary_link = uuid.uuid4()
    external = uuid.uuid4()
    markdown = f"""
![diagram](/api/assets/{visible})
![diagram with title](/api/assets/{titled} "Architecture")
![reference image][asset]

[asset]: /api/assets/{referenced}

`![not rendered](/api/assets/{inline})`

```markdown
![example only](/api/assets/{fenced})
```

[ordinary link](/api/assets/{ordinary_link})
![remote](https://example.com/{external})
"""

    assert extract_asset_ids(markdown) == {visible, titled, referenced}


def test_asset_urls_accept_case_variants_and_normalize_to_one_uuid() -> None:
    asset_id = uuid.uuid4()
    assert extract_asset_ids(f"![ok](/api/assets/{asset_id})") == {asset_id}
    assert extract_asset_ids(f"![prefix](/API/assets/{asset_id})") == {asset_id}
    assert extract_asset_ids(f"![uuid](/api/assets/{str(asset_id).upper()})") == {asset_id}


@pytest.mark.asyncio
async def test_create_asset_decodes_off_loop_and_records_pending_before_s3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import asset_service

    events: list[tuple] = []
    main_thread = threading.get_ident()

    class _Transaction:
        async def __aenter__(self):
            events.append(("tx_enter",))
            return None

        async def __aexit__(self, *_args):
            events.append(("tx_exit",))
            return None

    class _Connection:
        def transaction(self):
            return _Transaction()

        async def fetchval(self, *_args):
            events.append(("lock_vault",))
            return uuid.uuid4()

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_get_pool():
        return _Pool()

    def fake_inspect(_body):
        events.append(("inspect", threading.get_ident()))
        return "image/png", 1, 1

    def fake_ensure(_bucket):
        events.append(("ensure",))

    def fake_put(_key, _body, _mime):
        events.append(("put",))

    async def fake_insert(_conn, **_kwargs):
        events.append(("insert_pending",))

    async def fake_finalize(_conn, **_kwargs):
        events.append(("finalize",))
        return True

    monkeypatch.setattr(asset_service, "measurement_enabled", lambda: False)
    monkeypatch.setattr(asset_service, "get_pool", fake_get_pool)
    monkeypatch.setattr(asset_service, "inspect_image", fake_inspect)
    monkeypatch.setattr(asset_service.s3_adapter, "ensure_bucket", fake_ensure)
    monkeypatch.setattr(asset_service.s3_adapter, "put_bytes", fake_put)
    monkeypatch.setattr(
        asset_service.vault_files_repo, "insert_pending_attachment", fake_insert,
    )
    monkeypatch.setattr(
        asset_service.vault_files_repo, "finalize_attachment", fake_finalize,
    )

    result = await asset_service.create_image_asset(
        vault_id=uuid.uuid4(),
        vault_name="team",
        filename="diagram.png",
        declared_mime="image/png",
        body=ONE_PIXEL_PNG,
        actor_id="alice",
    )

    names = [event[0] for event in events]
    assert events[names.index("inspect")][1] != main_thread
    lock_indexes = [i for i, name in enumerate(names) if name == "lock_vault"]
    tx_enter_indexes = [i for i, name in enumerate(names) if name == "tx_enter"]
    tx_exit_indexes = [i for i, name in enumerate(names) if name == "tx_exit"]
    assert len(lock_indexes) == 2
    assert (
        lock_indexes[0]
        < names.index("insert_pending")
        < names.index("put")
        < lock_indexes[1]
        < names.index("finalize")
    )
    assert tx_exit_indexes[0] < names.index("put") < tx_enter_indexes[1]
    assert result["url"] == f"/api/assets/{result['id']}"


@pytest.mark.asyncio
async def test_create_asset_rejects_vault_deleted_while_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import asset_service

    class _Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _Connection:
        def transaction(self):
            return _Transaction()

        async def fetchval(self, *_args):
            return None

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_get_pool():
        return _Pool()

    async def unexpected_insert(*_args, **_kwargs):
        raise AssertionError("a deleted vault must not receive a pending asset row")

    def unexpected_put(*_args, **_kwargs):
        raise AssertionError("a deleted vault must not receive object bytes")

    monkeypatch.setattr(asset_service, "measurement_enabled", lambda: False)
    monkeypatch.setattr(asset_service, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        asset_service, "inspect_image", lambda _body: ("image/png", 1, 1),
    )
    monkeypatch.setattr(asset_service.s3_adapter, "ensure_bucket", lambda _bucket: None)
    monkeypatch.setattr(asset_service.s3_adapter, "put_bytes", unexpected_put)
    monkeypatch.setattr(
        asset_service.vault_files_repo, "insert_pending_attachment", unexpected_insert,
    )

    with pytest.raises(AKBError) as exc_info:
        await asset_service.create_image_asset(
            vault_id=uuid.uuid4(),
            vault_name="deleted",
            filename="diagram.png",
            declared_mime="image/png",
            body=ONE_PIXEL_PNG,
            actor_id="alice",
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_cancelled_upload_settles_put_before_enqueuing_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import asset_service

    events: list[str] = []
    put_started = threading.Event()
    allow_put_to_finish = threading.Event()

    class _Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _Connection:
        def transaction(self):
            return _Transaction()

        async def fetchval(self, *_args):
            events.append("lock_vault")
            return uuid.uuid4()

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_get_pool():
        return _Pool()

    def fake_put(_key, _body, _mime):
        events.append("put_started")
        put_started.set()
        assert allow_put_to_finish.wait(timeout=2)
        events.append("put_finished")

    async def fake_insert(_conn, **_kwargs):
        events.append("insert_pending")

    async def fake_delete(_conn, **_kwargs):
        events.append("delete_metadata")
        return {"s3_key": "team/.akb-assets/id/image.png"}

    async def fake_enqueue(_conn, _key):
        events.append("enqueue_delete")

    monkeypatch.setattr(asset_service, "measurement_enabled", lambda: False)
    monkeypatch.setattr(asset_service, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        asset_service, "inspect_image", lambda _body: ("image/png", 1, 1),
    )
    monkeypatch.setattr(asset_service.s3_adapter, "ensure_bucket", lambda _bucket: None)
    monkeypatch.setattr(asset_service.s3_adapter, "put_bytes", fake_put)
    monkeypatch.setattr(
        asset_service.vault_files_repo, "insert_pending_attachment", fake_insert,
    )
    monkeypatch.setattr(
        asset_service.vault_files_repo, "delete_failed_pending_attachment", fake_delete,
    )
    monkeypatch.setattr(asset_service, "enqueue_delete", fake_enqueue)

    upload = asyncio.create_task(
        asset_service.create_image_asset(
            vault_id=uuid.uuid4(),
            vault_name="team",
            filename="diagram.png",
            declared_mime="image/png",
            body=ONE_PIXEL_PNG,
            actor_id="alice",
        )
    )
    assert await asyncio.to_thread(put_started.wait, 1)
    upload.cancel()
    await asyncio.sleep(0)
    assert "delete_metadata" not in events

    allow_put_to_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await upload

    assert events.index("put_finished") < events.index("delete_metadata")
    assert events.index("delete_metadata") < events.index("enqueue_delete")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delete_name", "expected_state"),
    [
        ("delete_unclaimed_attachment", "confirmed"),
        ("delete_failed_pending_attachment", "pending"),
    ],
)
async def test_user_discard_and_failed_upload_cleanup_have_separate_state_guards(
    delete_name: str,
    expected_state: str,
) -> None:
    from app.repositories import vault_files_repo

    captured: dict[str, object] = {}

    class _Conn:
        async def fetchrow(self, query, *args):
            captured.update(query=" ".join(query.split()), args=args)
            return None

    await getattr(vault_files_repo, delete_name)(
        _Conn(),
        vault_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        created_by="alice",
    )

    assert f"upload_state = '{expected_state}'" in str(captured["query"])


@pytest.mark.asyncio
async def test_public_asset_requires_counted_page_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException
    from app.api.routes import public

    resolved = False

    async def unexpected_resolve(*_args, **_kwargs):
        nonlocal resolved
        resolved = True
        raise AssertionError("an ungranted image must not resolve or spend a view")

    monkeypatch.setattr(public, "_extract_view_grant", lambda _request: None)
    monkeypatch.setattr(public, "_verify_view_grant", lambda _slug, _grant: False)
    monkeypatch.setattr(public, "_resolve_with_access", unexpected_resolve)

    with pytest.raises(HTTPException) as exc:
        await public.publication_document_asset("share", str(uuid.uuid4()), object())
    assert exc.value.status_code == 404
    assert resolved is False


@pytest.mark.asyncio
async def test_public_asset_grant_never_increments_publication_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.responses import Response
    from app.api.routes import public

    file_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    captured: dict = {}

    request = object()

    async def fake_resolve(slug, resolved_request, **kwargs):
        captured.update(slug=slug, request=resolved_request, **kwargs)
        return {"resource_type": public.ResourceType.DOCUMENT, "vault_id": vault_id}

    async def fake_asset_ids(_publication):
        return frozenset({file_id})

    async def fake_load(requested, requested_vault):
        captured.update(file_id=requested, vault_id=requested_vault)
        return {"id": file_id}

    async def fake_response(_row, *, public: bool):
        captured["public"] = public
        return Response(content=b"image", media_type="image/png")

    monkeypatch.setattr(public, "_extract_view_grant", lambda _request: "grant")
    monkeypatch.setattr(public, "_verify_view_grant", lambda _slug, _grant: True)
    monkeypatch.setattr(public, "_resolve_with_access", fake_resolve)
    monkeypatch.setattr(
        public.publication_service,
        "resolve_document_publication_asset_ids",
        fake_asset_ids,
    )
    monkeypatch.setattr(public.assets, "load_asset_row", fake_load)
    monkeypatch.setattr(public.assets, "image_asset_response", fake_response)

    response = await public.publication_document_asset("share", str(file_id), request)

    assert response.body == b"image"
    assert captured == {
        "slug": "share",
        "request": request,
        "increment_view": False,
        "enforce_view_cap": False,
        "file_id": str(file_id),
        "vault_id": vault_id,
        "public": True,
    }


@pytest.mark.asyncio
async def test_public_asset_grant_does_not_bypass_password_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException
    from app.api.routes import public

    file_id = uuid.uuid4()

    async def require_password(*_args, **_kwargs):
        raise public.PublicationPasswordRequired()

    monkeypatch.setattr(public, "_extract_view_grant", lambda _request: "grant")
    monkeypatch.setattr(public, "_verify_view_grant", lambda _slug, _grant: True)
    monkeypatch.setattr(public, "_resolve_with_access", require_password)

    with pytest.raises(HTTPException) as exc:
        await public.publication_document_asset("share", str(file_id), object())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_public_asset_manifest_cache_does_not_retain_document_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import publication_service

    visible = uuid.uuid4()
    hidden = uuid.uuid4()
    reads = 0

    class _Git:
        def read_file(self, vault, path, commit):
            nonlocal reads
            reads += 1
            assert (vault, path, commit) == ("team", "weekly.md", "a" * 40)
            return (
                "---\ntitle: Weekly\n---\n"
                f"# Public\n\n![shown](/api/assets/{visible})\n\n"
                f"# Private\n\n![hidden](/api/assets/{hidden})\n"
            )

    row = {
        "path": "weekly.md",
        "title": "Weekly",
        "doc_type": "note",
        "summary": None,
        "domain": None,
        "updated_at": None,
        "tags": [],
        "current_commit": "a" * 40,
        "vault_name": "team",
        "created_by_name": "Alice",
    }
    publication = {
        "resource_type": publication_service.ResourceType.DOCUMENT,
        "section_filter": "Public",
        "vault_id": str(uuid.uuid4()),
    }

    async def fake_find(_publication):
        return row

    publication_service._PINNED_DOCUMENT_ASSET_CACHE.clear()
    monkeypatch.setattr(
        publication_service,
        "_get_doc_service",
        lambda: SimpleNamespace(git=_Git()),
    )
    monkeypatch.setattr(publication_service, "_find_published_document", fake_find)
    try:
        rendered = await publication_service.resolve_document_publication(publication)
        asset_ids = await publication_service.resolve_document_publication_asset_ids(
            publication,
        )
        repeated_asset_ids = await asyncio.gather(
            *(
                publication_service.resolve_document_publication_asset_ids(publication)
                for _ in range(8)
            )
        )
    finally:
        publication_service._PINNED_DOCUMENT_ASSET_CACHE.clear()

    # Page resolution seeds the compact UUID manifest, so subordinate image
    # requests neither reread Git nor retain the complete body.
    assert reads == 1
    assert str(visible) in rendered["content"]
    assert str(hidden) not in rendered["content"]
    assert asset_ids == frozenset({visible})
    assert repeated_asset_ids == [asset_ids] * 8


@pytest.mark.asyncio
async def test_legacy_public_document_resolves_head_into_pinned_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import publication_service

    visible = uuid.uuid4()
    reads = 0
    heads = 0

    class _Git:
        def current_commit(self, vault):
            nonlocal heads
            heads += 1
            assert vault == "team"
            return "c" * 40

        def read_file(self, vault, path, commit):
            nonlocal reads
            reads += 1
            assert (vault, path, commit) == ("team", "legacy.md", "c" * 40)
            return f"---\ntitle: Legacy\n---\n![shown](/api/assets/{visible})"

    row = {
        "path": "legacy.md",
        "title": "Legacy",
        "doc_type": "note",
        "summary": None,
        "domain": None,
        "updated_at": None,
        "tags": [],
        "current_commit": None,
        "vault_name": "team",
        "created_by_name": "Alice",
    }
    publication = {
        "resource_type": publication_service.ResourceType.DOCUMENT,
        "section_filter": None,
        "vault_id": str(uuid.uuid4()),
    }

    async def fake_find(_publication):
        return row

    publication_service._PINNED_DOCUMENT_ASSET_CACHE.clear()
    monkeypatch.setattr(
        publication_service,
        "_get_doc_service",
        lambda: SimpleNamespace(git=_Git()),
    )
    monkeypatch.setattr(publication_service, "_find_published_document", fake_find)
    try:
        await publication_service.resolve_document_publication(publication)
        asset_ids = await publication_service.resolve_document_publication_asset_ids(
            publication,
        )
    finally:
        publication_service._PINNED_DOCUMENT_ASSET_CACHE.clear()

    assert heads == 2
    assert reads == 1
    assert asset_ids == frozenset({visible})


def test_publication_view_grant_is_short_lived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import public

    now = 1_000_000.0
    monkeypatch.setattr(public.time, "time", lambda: now)
    monkeypatch.setattr(public.settings, "publication_view_grant_ttl_secs", 600)
    monkeypatch.setattr(public.settings, "publication_view_grant_session_secs", 3600)
    grant = public._make_view_grant("share")

    now += 599
    assert public._verify_view_grant("share", grant) is True
    now += 2
    assert public._verify_view_grant("share", grant) is False


def test_publication_grant_emission_stays_legacy_during_rolling_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import public

    monkeypatch.setattr(public.settings, "publication_view_grant_emit_legacy", True)
    grant = public._make_view_grant("share", issued_at=1_000_000)

    assert len(grant.split(".")) == 2
    assert public._parse_view_grant("share", grant) == (1_000_000, 1_000_600)


def test_legacy_publication_view_grant_remains_valid_during_rollout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import public

    now = 1_000_000
    monkeypatch.setattr(public.time, "time", lambda: float(now))
    monkeypatch.setattr(public.settings, "publication_view_grant_ttl_secs", 600)
    monkeypatch.setattr(public.settings, "publication_view_grant_session_secs", 3600)
    message = f"grant:share:{now}".encode()
    signature = hmac.new(
        public.settings.jwt_secret.encode(), message, hashlib.sha256,
    ).hexdigest()
    legacy_grant = f"{now}.{signature}"

    assert public._verify_view_grant("share", legacy_grant) is True
    assert public._parse_view_grant("other", legacy_grant) is None


def test_non_ascii_hmac_inputs_fail_closed() -> None:
    from app.api.routes import public

    assert public._verify_view_grant("share", "1000000.é") is False
    assert public._verify_view_grant("share", "1000000.1000600.é") is False
    assert public._verify_token("share", "1000000.é") is False


def test_publication_view_grant_cannot_rotate_past_fixed_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import public

    now = 1_000_000.0
    monkeypatch.setattr(public.time, "time", lambda: now)
    monkeypatch.setattr(public.settings, "publication_view_grant_ttl_secs", 600)
    monkeypatch.setattr(public.settings, "publication_view_grant_session_secs", 3600)
    monkeypatch.setattr(public.settings, "publication_view_grant_emit_legacy", False)
    grant = public._make_view_grant("share")

    now += 3599
    rotated = public._renew_view_grant("share", grant)
    assert rotated is not None
    issued, expires = public._parse_view_grant("share", rotated) or (0, 0)
    assert expires == issued + 3600

    now += 2
    assert public._renew_view_grant("share", rotated) is None


def test_bounded_grant_rotation_does_not_downgrade_during_legacy_emission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import public

    now = 1_000_000.0
    monkeypatch.setattr(public.time, "time", lambda: now)
    monkeypatch.setattr(public.settings, "publication_view_grant_ttl_secs", 600)
    monkeypatch.setattr(public.settings, "publication_view_grant_session_secs", 3600)
    monkeypatch.setattr(public.settings, "publication_view_grant_emit_legacy", True)
    grant = public._make_bounded_view_grant("share")

    now += 601
    rotated = public._renew_view_grant("share", grant)

    assert rotated is not None
    assert len(rotated.split(".")) == 3
    assert public._verify_view_grant("share", rotated) is True


@pytest.mark.asyncio
async def test_legacy_view_grant_cannot_be_promoted_to_a_longer_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.api.routes import public

    now = 1_000_000
    monkeypatch.setattr(public.time, "time", lambda: float(now))
    monkeypatch.setattr(public.settings, "publication_view_grant_ttl_secs", 600)
    monkeypatch.setattr(public.settings, "publication_view_grant_session_secs", 3600)
    signature = hmac.new(
        public.settings.jwt_secret.encode(),
        f"grant:share:{now}".encode(),
        hashlib.sha256,
    ).hexdigest()
    old_grant = f"{now}.{signature}"
    now += 601
    resolve_called = False

    async def fake_resolve(slug, request, increment_view=True, enforce_view_cap=True):
        nonlocal resolve_called
        resolve_called = True
        return {"slug": slug}

    monkeypatch.setattr(public, "_resolve_with_access", fake_resolve)
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "query_string": f"grant={old_grant}".encode(),
    })

    with pytest.raises(HTTPException) as exc:
        await public.renew_publication_view_grant("share", request)

    assert exc.value.status_code == 404
    assert resolve_called is False


@pytest.mark.asyncio
async def test_asset_response_heads_then_streams_without_buffering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.responses import StreamingResponse
    from app.api.routes import assets

    calls: list[tuple] = []

    def fake_head(key):
        calls.append(("head", key))
        return {"ContentLength": 5}

    def fake_stream(key, *, max_bytes):
        calls.append(("stream", key, max_bytes))
        yield b"im"
        yield b"age"

    def unexpected_buffer(*_args, **_kwargs):
        raise AssertionError("image responses must not buffer the full object")

    monkeypatch.setattr(assets.file_service, "head_object", fake_head)
    monkeypatch.setattr(assets.file_service, "iter_object_chunks", fake_stream)
    monkeypatch.setattr(assets.file_service, "get_object_bytes", unexpected_buffer)

    response = await assets.image_asset_response(
        {
            "id": uuid.uuid4(),
            "s3_key": "team/.akb-assets/id/image.png",
            "mime_type": "image/png",
            "size_bytes": 5,
        }
    )

    assert isinstance(response, StreamingResponse)
    assert b"".join([chunk async for chunk in response.body_iterator]) == b"image"
    assert calls == [
        ("head", "team/.akb-assets/id/image.png"),
        (
            "stream",
            "team/.akb-assets/id/image.png",
            assets.asset_service.IMAGE_ASSET_MAX_BYTES,
        ),
    ]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization"


@pytest.mark.asyncio
async def test_document_asset_claim_rejects_missing_or_cross_vault_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible = uuid.uuid4()
    foreign = uuid.uuid4()
    captured: dict = {}

    async def fake_claim(_conn, vault_id, file_ids, *, strict):
        captured.update(vault_id=vault_id, file_ids=file_ids, strict=strict)
        return {visible}

    from app.services import asset_service

    monkeypatch.setattr(
        asset_service.vault_files_repo,
        "claim_attachment_references",
        fake_claim,
    )
    vault_id = uuid.uuid4()
    markdown = (
        f"![local](/api/assets/{visible})\n"
        f"![foreign](/api/assets/{foreign})"
    )

    with pytest.raises(AKBError) as exc:
        await claim_document_assets(
            object(), vault_id=vault_id, markdown=markdown,
        )

    assert exc.value.status_code == 422
    assert captured == {
        "vault_id": vault_id,
        "file_ids": {visible, foreign},
        "strict": True,
    }


@pytest.mark.asyncio
async def test_document_asset_claim_rejects_only_new_unavailable_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import asset_service

    existing_broken = uuid.uuid4()
    newly_missing = uuid.uuid4()

    async def find_none(_conn, _vault_id, _file_ids, *, strict):
        assert strict is False
        return set()

    monkeypatch.setattr(
        asset_service.vault_files_repo,
        "claim_attachment_references",
        find_none,
    )
    previous = f"![old](/api/assets/{existing_broken})"
    assert await claim_document_assets(
        object(),
        vault_id=uuid.uuid4(),
        markdown=previous,
        strict=False,
        previous_markdown=previous,
    ) == set()

    with pytest.raises(AKBError) as exc:
        await claim_document_assets(
            object(),
            vault_id=uuid.uuid4(),
            markdown=previous + f"\n![new](/api/assets/{newly_missing})",
            strict=False,
            previous_markdown=previous,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_discard_route_deletes_only_through_transactional_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import assets

    file_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    calls: list[tuple] = []

    class _Transaction:
        async def __aenter__(self):
            calls.append(("tx_enter",))

        async def __aexit__(self, *_args):
            calls.append(("tx_exit",))

    class _Connection:
        def transaction(self):
            return _Transaction()

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_actor(_request, _vault, _user):
        return {"vault_id": vault_id}, "alice", None

    async def fake_delete(_conn, *, vault_id, file_id, created_by):
        calls.append(("delete", vault_id, file_id, created_by))
        return {"id": file_id, "s3_key": "vault/.akb-assets/object"}

    async def fake_state(_conn, *, vault_id, file_id, created_by):
        calls.append(("state", vault_id, file_id, created_by))
        return {
            "id": file_id,
            "s3_key": "vault/.akb-assets/object",
            "upload_state": "confirmed",
            "attachment_claimed_at": None,
        }

    async def fake_enqueue(_conn, key):
        calls.append(("enqueue", key))

    async def fake_get_pool():
        return _Pool()

    monkeypatch.setattr(assets, "resolve_file_write_context", fake_actor)
    monkeypatch.setattr(assets, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        assets.vault_files_repo,
        "find_owned_attachment_for_discard",
        fake_state,
    )
    monkeypatch.setattr(
        assets.vault_files_repo,
        "delete_unclaimed_attachment",
        fake_delete,
    )
    monkeypatch.setattr(assets, "enqueue_delete", fake_enqueue)

    response = await assets.discard_document_image(
        object(), "vault", str(file_id), SimpleNamespace(),
    )

    assert response == {"discarded": True}
    assert calls == [
        ("tx_enter",),
        ("state", vault_id, file_id, "alice"),
        ("delete", vault_id, file_id, "alice"),
        ("enqueue", "vault/.akb-assets/object"),
        ("tx_exit",),
    ]


@pytest.mark.asyncio
async def test_discard_route_reports_non_disclosing_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import assets

    vault_id = uuid.uuid4()

    class _Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _Connection:
        def transaction(self):
            return _Transaction()

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_actor(_request, _vault, _user):
        return {"vault_id": vault_id}, "alice", None

    async def fake_state(*_args, **_kwargs):
        return None

    async def fake_get_pool():
        return _Pool()

    monkeypatch.setattr(assets, "resolve_file_write_context", fake_actor)
    monkeypatch.setattr(assets, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        assets.vault_files_repo,
        "find_owned_attachment_for_discard",
        fake_state,
    )

    response = await assets.discard_document_image(
        object(), "vault", str(uuid.uuid4()), SimpleNamespace(),
    )

    assert response == {"discarded": False}


@pytest.mark.asyncio
async def test_regular_file_confirm_rejects_attachment_before_storage_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic File confirm endpoint must not mutate editor assets."""
    from app.services import file_service

    class _Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _Connection:
        def transaction(self):
            return _Transaction()

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_get_pool():
        return _Pool()

    async def fake_lease(_conn, _vault_id, _file_id):
        # The repository predicate excludes kind='attachment'.
        return None

    storage_touched = False

    def unexpected_head(_key):
        nonlocal storage_touched
        storage_touched = True
        raise AssertionError("attachment storage must not be touched")

    monkeypatch.setattr(file_service, "get_pool", fake_get_pool)
    monkeypatch.setattr(file_service, "measurement_enabled", lambda: False)
    monkeypatch.setattr(
        file_service.vault_files_repo,
        "lease_file_upload_confirmation",
        fake_lease,
    )
    monkeypatch.setattr(file_service.s3_adapter, "head", unexpected_head)

    with pytest.raises(NotFoundError):
        await file_service.FileService().confirm_upload(
            uuid.uuid4(), str(uuid.uuid4()), actor_id="tester",
        )

    assert storage_touched is False


@pytest.mark.asyncio
async def test_sync_references_retains_previous_and_publishes_current() -> None:
    from app.repositories import vault_files_repo

    document_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    retain_until = datetime.now(timezone.utc)
    calls: list[tuple[str, tuple]] = []

    class _Connection:
        async def execute(self, sql, *args):
            calls.append((" ".join(sql.split()), args))

    await vault_files_repo.sync_document_asset_references(
        _Connection(),
        document_id=document_id,
        vault_id=vault_id,
        document_path="new/doc.md",
        commit_hash="b" * 40,
        asset_ids={asset_id},
        retain_until=retain_until,
        previous_commit="a" * 40,
        previous_path="old/doc.md",
    )

    assert len(calls) == 4
    assert "SELECT live.vault_id" in calls[0][0]
    assert calls[0][1][2:4] == ("old/doc.md", "a" * 40)
    assert "DELETE FROM document_asset_refs" in calls[1][0]
    assert "INSERT INTO document_asset_refs" in calls[2][0]
    assert "INSERT INTO document_asset_revision_refs" in calls[3][0]
    assert calls[3][1][1:4] == ("new/doc.md", "b" * 40, [asset_id])


@pytest.mark.asyncio
async def test_private_asset_lookup_carries_live_owner_and_revision_scope() -> None:
    from app.repositories import vault_files_repo

    file_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    captured: dict = {}

    class _Connection:
        async def fetchrow(self, sql, *args):
            captured.update(sql=" ".join(sql.split()), args=args)
            return None

    result = await vault_files_repo.find_authorized_attachment(
        _Connection(),
        vault_id=vault_id,
        file_id=file_id,
        created_by="alice",
        document_path="notes/weekly.md",
        commit_prefix="abcdef1",
    )

    assert result is None
    assert "document_asset_refs live" in captured["sql"]
    assert "document_asset_revision_refs rev" in captured["sql"]
    assert "rev.retain_until > NOW()" in captured["sql"]
    assert captured["args"] == (
        file_id, vault_id, "alice", "notes/weekly.md", "abcdef1",
    )


@pytest.mark.asyncio
async def test_private_preview_uses_the_delegated_upload_actor(monkeypatch) -> None:
    from app.api.routes import assets

    file_id = uuid.uuid4()
    vault_id = uuid.uuid4()
    captured: dict = {}

    class _Acquire:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_access(*_args, **_kwargs):
        return {"vault_id": vault_id}

    async def fake_actor(request, vault, user):
        assert vault == "team"
        return "delegated-human"

    actors: list[str] = []

    async def fake_find(_conn, **kwargs):
        actors.append(kwargs["created_by"])
        captured.update(kwargs)
        if kwargs["created_by"] == "service":
            return None
        return {"id": file_id, "s3_key": "key", "size_bytes": 1}

    async def fake_response(row):
        return row

    async def fake_pool():
        return _Pool()

    monkeypatch.setattr(assets, "check_vault_access", fake_access)
    monkeypatch.setattr(assets, "resolve_file_read_actor", fake_actor)
    monkeypatch.setattr(assets, "get_pool", fake_pool)
    monkeypatch.setattr(assets.vault_files_repo, "find_authorized_attachment", fake_find)
    monkeypatch.setattr(assets, "image_asset_response", fake_response)

    request = SimpleNamespace(headers={"x-akb-delegated-authorization": "Bearer session"})
    result = await assets.read_document_image(
        request,
        str(file_id),
        vault="team",
        document=None,
        commit=None,
        user=SimpleNamespace(user_id="service", username="service"),
    )

    assert result["id"] == file_id
    assert captured["created_by"] == "delegated-human"
    assert actors == ["service", "delegated-human"]


@pytest.mark.asyncio
async def test_claimed_private_image_does_not_require_delegated_writer(monkeypatch) -> None:
    """A delegated header must not downgrade an ordinary reader path."""
    from app.api.routes import assets

    file_id = uuid.uuid4()
    vault_id = uuid.uuid4()

    class _Acquire:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_access(*_args, **_kwargs):
        return {"vault_id": vault_id}

    async def unexpected_actor(*_args):
        raise AssertionError("a claimed image must not require writer delegation")

    async def fake_find(_conn, **kwargs):
        assert kwargs["created_by"] == "reader-service"
        return {"id": file_id, "s3_key": "key", "size_bytes": 1}

    async def fake_response(row):
        return row

    async def fake_pool():
        return _Pool()

    monkeypatch.setattr(assets, "check_vault_access", fake_access)
    monkeypatch.setattr(assets, "resolve_file_read_actor", unexpected_actor)
    monkeypatch.setattr(assets, "get_pool", fake_pool)
    monkeypatch.setattr(assets.vault_files_repo, "find_authorized_attachment", fake_find)
    monkeypatch.setattr(assets, "image_asset_response", fake_response)

    result = await assets.read_document_image(
        SimpleNamespace(headers={"x-akb-delegated-authorization": "Bearer session"}),
        str(file_id),
        vault="team",
        document=None,
        commit=None,
        user=SimpleNamespace(user_id="service", username="reader-service"),
    )

    assert result["id"] == file_id


@pytest.mark.asyncio
async def test_asset_gc_deletes_metadata_and_enqueues_object_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import asset_gc_worker

    calls: list[tuple] = []

    class _Transaction:
        async def __aenter__(self):
            calls.append(("tx_enter",))

        async def __aexit__(self, *_args):
            calls.append(("tx_exit",))

    class _Connection:
        def transaction(self):
            return _Transaction()

        async def execute(self, sql, *args):
            calls.append(("execute", " ".join(sql.split()), args))

        async def fetch(self, sql, *_args):
            calls.append(("fetch", " ".join(sql.split())))
            return [
                {
                    "id": uuid.uuid4(),
                    "s3_key": "vault/.akb-assets/old/image.png",
                    "kind": "attachment",
                    "upload_state": "confirmed",
                },
            ]

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_get_pool():
        return _Pool()

    async def fake_enqueue(_conn, key):
        calls.append(("enqueue", key))

    monkeypatch.setattr(asset_gc_worker, "get_pool", fake_get_pool)
    monkeypatch.setattr(asset_gc_worker, "enqueue_delete", fake_enqueue)

    assert await asset_gc_worker.collect_once() == 1
    assert calls[0] == ("tx_enter",)
    assert ("enqueue", "vault/.akb-assets/old/image.png") in calls
    expiry_call = next(call for call in calls if call[0] == "execute")
    assert "ORDER BY retain_until LIMIT $1 FOR UPDATE SKIP LOCKED" in expiry_call[1]
    assert expiry_call[2] == (asset_gc_worker.REVISION_EXPIRE_BATCH_SIZE,)
    candidate_sql = next(call[1] for call in calls if call[0] == "fetch")
    assert "hash_verified_at IS NOT NULL" not in candidate_sql
    assert "vf.kind = 'file'" in candidate_sql
    assert "vf.upload_state = 'pending'" in candidate_sql
    assert calls[-1] == ("tx_exit",)


@pytest.mark.asyncio
async def test_pending_upload_delete_schedules_post_put_reconciliation() -> None:
    from app.services import s3_delete_worker

    calls: list[tuple[str, int]] = []

    class _Connection:
        async def execute(self, _sql, key, delay_seconds):
            calls.append((key, delay_seconds))

    await s3_delete_worker.enqueue_pending_upload_delete(
        _Connection(), "vault/file.bin",
    )

    assert calls == [
        ("vault/file.bin", 0),
        (
            "vault/file.bin",
            s3_delete_worker.PENDING_UPLOAD_DELETE_RECHECK_SECONDS,
        ),
    ]


@pytest.mark.parametrize("referenced", [False, True])
@pytest.mark.asyncio
async def test_s3_delete_worker_rechecks_live_key_under_shared_lock(
    monkeypatch: pytest.MonkeyPatch,
    referenced: bool,
) -> None:
    from app.services import s3_delete_worker

    calls: list[tuple] = []

    class _Transaction:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _Connection:
        def transaction(self):
            return _Transaction()

        async def fetchval(self, sql, key):
            assert "SELECT EXISTS" in sql
            calls.append(("referenced", key))
            return referenced

    class _Acquire:
        async def __aenter__(self):
            return _Connection()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_get_pool():
        return _Pool()

    async def fake_claim(_conn):
        calls.append(("claim",))
        return [{"id": 7, "s3_key": "vault/shared.bin", "retry_count": 0}]

    async def fake_lock(_conn, key):
        calls.append(("lock", key))

    async def fake_success(_conn, outbox_id):
        calls.append(("success", outbox_id))

    def fake_delete(key):
        calls.append(("delete", key))

    monkeypatch.setattr(s3_delete_worker, "get_pool", fake_get_pool)
    monkeypatch.setattr(s3_delete_worker, "_claim_batch", fake_claim)
    monkeypatch.setattr(s3_delete_worker, "_mark_success", fake_success)
    monkeypatch.setattr(
        s3_delete_worker.vault_files_repo,
        "lock_s3_key_for_mutation",
        fake_lock,
    )
    monkeypatch.setattr(s3_delete_worker.s3_adapter, "delete", fake_delete)

    assert await s3_delete_worker._process_deletes_once() == 1
    assert calls[:3] == [
        ("claim",),
        ("lock", "vault/shared.bin"),
        ("referenced", "vault/shared.bin"),
    ]
    if referenced:
        assert ("delete", "vault/shared.bin") not in calls
    else:
        assert calls[3] == ("delete", "vault/shared.bin")
    assert calls[-1] == ("success", 7)
