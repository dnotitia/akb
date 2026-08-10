from __future__ import annotations

import base64
import uuid
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


def test_extract_asset_ids_is_conservative_around_code() -> None:
    visible = uuid.uuid4()
    fenced = uuid.uuid4()
    inline = uuid.uuid4()
    external = uuid.uuid4()
    markdown = f"""
![diagram](/api/assets/{visible})

`![not rendered](/api/assets/{inline})`

```markdown
![example only](/api/assets/{fenced})
```

![remote](https://example.com/{external})
"""

    assert extract_asset_ids(markdown) == {visible}


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
        return {"vault_id": vault_id}, "alice"

    async def fake_delete(_conn, *, vault_id, file_id, created_by):
        calls.append(("delete", vault_id, file_id, created_by))
        return {"id": file_id, "s3_key": "vault/.akb-assets/object"}

    async def fake_enqueue(_conn, key):
        calls.append(("enqueue", key))

    async def fake_get_pool():
        return _Pool()

    monkeypatch.setattr(assets, "_asset_write_actor", fake_actor)
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

    class _Acquire:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    class _Pool:
        def acquire(self):
            return _Acquire()

    async def fake_get_pool():
        return _Pool()

    async def fake_find_by_id(_conn, _vault_id, _file_id):
        return {"kind": "attachment", "s3_key": "must-not-be-read"}

    storage_touched = False

    def unexpected_head(_key):
        nonlocal storage_touched
        storage_touched = True
        raise AssertionError("attachment storage must not be touched")

    monkeypatch.setattr(file_service, "get_pool", fake_get_pool)
    monkeypatch.setattr(file_service, "measurement_enabled", lambda: False)
    monkeypatch.setattr(file_service.vault_files_repo, "find_by_id", fake_find_by_id)
    monkeypatch.setattr(file_service.s3_adapter, "head", unexpected_head)

    with pytest.raises(NotFoundError):
        await file_service.FileService().confirm_upload(
            uuid.uuid4(), str(uuid.uuid4()), actor_id="tester",
        )

    assert storage_touched is False
