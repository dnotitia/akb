from __future__ import annotations

import asyncio
import base64
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
        asset_service.vault_files_repo, "delete_unclaimed_attachment", fake_delete,
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
async def test_public_document_body_and_asset_manifest_share_pinned_cache(
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

    publication_service._read_pinned_document_body.cache_clear()
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
    finally:
        publication_service._read_pinned_document_body.cache_clear()

    assert reads == 1
    assert str(visible) in rendered["content"]
    assert str(hidden) not in rendered["content"]
    assert asset_ids == frozenset({visible})


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

    publication_service._read_pinned_document_body.cache_clear()
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
        publication_service._read_pinned_document_body.cache_clear()

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
    grant = public._make_view_grant("share")

    now += 599
    assert public._verify_view_grant("share", grant) is True
    now += 2
    assert public._verify_view_grant("share", grant) is False


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

    async def fake_enqueue(_conn, key):
        calls.append(("enqueue", key))

    async def fake_get_pool():
        return _Pool()

    monkeypatch.setattr(assets, "resolve_file_write_context", fake_actor)
    monkeypatch.setattr(assets, "get_pool", fake_get_pool)
    monkeypatch.setattr(
        assets.vault_files_repo,
        "delete_unclaimed_attachment",
        fake_delete,
    )
    monkeypatch.setattr(assets, "enqueue_delete", fake_enqueue)

    response = await assets.discard_document_image(
        object(), "vault", str(file_id), SimpleNamespace(),
    )

    assert response.status_code == 204
    assert calls == [
        ("tx_enter",),
        ("delete", vault_id, file_id, "alice"),
        ("enqueue", "vault/.akb-assets/object"),
        ("tx_exit",),
    ]


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
async def test_pending_upload_delete_is_rechecked_after_presigned_put_window() -> None:
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
