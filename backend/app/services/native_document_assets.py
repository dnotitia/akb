"""Document-only attachment projection inside the Native authority transaction.

No object-store calls or pool acquisition belong here. Both approved Native
body placements are local PostgreSQL payloads; the locked parent's verified
body, not a potentially stale live-reference cache, determines its image set.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import frontmatter

from app.exceptions import ValidationError
from app.repositories import vault_files_repo
from app.repositories.vault_files_repo import DocumentAssetOwner
from app.services import asset_service
from app.services.native_payload_verification import verify_native_head_body


async def image_ids(payload: str | bytes) -> set[uuid.UUID]:
    def parse() -> set[uuid.UUID]:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        if asset_service.ASSET_URL_PREFIX not in text.lower():
            return set()
        return asset_service.extract_asset_ids(frontmatter.loads(text).content)

    return await asyncio.to_thread(parse)


async def locked_head_image_ids(conn, repository, resource_id: uuid.UUID) -> set[uuid.UUID]:
    """Read the parent through the caller's connection, with its resource locked."""
    head = await repository.get_resource_head(resource_id=resource_id, conn=conn)
    if head is None or head["payload_manifest_id"] is None:
        raise RuntimeError("Live Native document Head has no payload")
    row = await conn.fetchrow(
        "SELECT * FROM m1_reference_payloads WHERE payload_id=$1",
        head["private_locator"],
    )
    if row is None:
        raise RuntimeError("Native document payload is missing")
    payload = dict(row)

    def verify_and_parse() -> set[uuid.UUID]:
        canonical = verify_native_head_body(payload)
        if any(payload[key] != head[key] for key in (
            "namespace_id", "digest", "byte_size", "encoding", "selected_placement", "verification_profile",
        )) or hashlib.sha256(canonical).hexdigest() != head["digest"]:
            raise RuntimeError("Native document payload does not match its Head manifest")
        text = canonical.decode("utf-8")
        if asset_service.ASSET_URL_PREFIX not in text.lower():
            return set()
        return asset_service.extract_asset_ids(frontmatter.loads(text).content)

    return await asyncio.to_thread(verify_and_parse)


async def publish(
    conn,
    *,
    namespace_id: uuid.UUID,
    resource_id: uuid.UUID,
    path: str,
    revision_id: str,
    asset_ids: set[uuid.UUID],
    previous_ids: set[uuid.UUID] | None = None,
    previous_revision_id: str | None = None,
    previous_path: str | None = None,
    allow_unavailable: bool = False,
) -> None:
    """Claim and publish exact new/previous manifests as part of a fresh mutation."""
    previous = previous_ids or set()
    found = await asset_service.claim_document_asset_ids(
        conn, vault_id=namespace_id, asset_ids=asset_ids | previous, strict=False,
    )
    required = set() if allow_unavailable else asset_ids - previous
    if not required.issubset(found):
        raise ValidationError(
            "Document contains an unavailable image reference; upload the image to this vault first"
        )
    if previous_revision_id is not None:
        await vault_files_repo.retain_document_asset_ids(
            conn, vault_id=namespace_id, document_path=previous_path or path,
            commit_hash=previous_revision_id, asset_ids=found & previous,
            retain_until=asset_service._revision_retain_until(),
        )
    await asset_service.sync_document_assets(
        conn, owner=DocumentAssetOwner(vault_id=namespace_id, native_document_id=resource_id),
        document_path=path, commit_hash=revision_id, asset_ids=found & asset_ids,
    )


async def retain_for_delete(conn, *, namespace_id, resource_id, path, revision_id, asset_ids) -> None:
    found = await asset_service.claim_document_asset_ids(
        conn, vault_id=namespace_id, asset_ids=asset_ids, strict=False,
    )
    await vault_files_repo.retain_document_asset_ids(
        conn, vault_id=namespace_id, document_path=path, commit_hash=revision_id,
        asset_ids=found, retain_until=asset_service._revision_retain_until(),
    )
