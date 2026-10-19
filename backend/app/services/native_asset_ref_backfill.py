"""Give Native documents written before migration 107 their image references.

An inline image becomes readable by being referenced: `read_document_image`
authorizes bytes when a live `document_asset_refs` row names the asset. Until
migration 107 that row could only name a `documents.id`, so on a Native
deployment the reference was never written and every image uploaded after the
cutover rendered broken — silently, because nothing in the write path failed.

The code fix stops that happening again; it does not repair the documents
already written. This walks the live Native documents whose body names an
asset URL and publishes the reference the write should have.

It is safe to re-run. `sync_document_asset_references` replaces a document's
live set rather than appending to it, so a second pass over an already-correct
document is a no-op, and a document whose body no longer names an image has
its stale references removed.

Claiming is deliberately non-strict. A body written during the broken window
can name an asset that has since been discarded or collected — the reference
for that one cannot be restored, and refusing the whole document because of it
would leave its other images broken too. Those are counted as `unresolved`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from app.config import settings
from app.db.postgres import get_pool
from app.repositories import vault_files_repo
from app.repositories.native_revision_repo import NativeRevisionRepository
from app.repositories.vault_files_repo import DocumentAssetOwner
from app.services import asset_service, native_document_assets

logger = logging.getLogger("akb.native_asset_ref_backfill")


@dataclass
class NativeAssetRefReport:
    """What the walk saw, in the terms an operator has to check."""

    scanned: int = 0
    with_images: int = 0
    refs_written: int = 0
    stale_refs: int = 0
    refs_removed: int = 0
    unresolved: int = 0
    failed: int = 0
    complete: bool = True
    vaults: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


async def _candidate_vaults(conn, vault: str | None) -> list[tuple[uuid.UUID, str]]:
    """Vaults that own at least one confirmed attachment.

    A vault with no attachment cannot have a document referencing one, so this
    is what keeps the walk proportional to the problem instead of to the
    corpus.
    """
    if vault is not None:
        rows = await conn.fetch(
            "SELECT id, name FROM vaults WHERE name = $1", vault,
        )
        if not rows:
            raise ValueError(f"unknown vault: {vault}")
        return [(row["id"], row["name"]) for row in rows]
    rows = await conn.fetch(
        """
        SELECT DISTINCT v.id, v.name
          FROM vaults v
          JOIN vault_files vf ON vf.vault_id = v.id
         WHERE vf.kind = 'attachment'
         ORDER BY v.name
        """
    )
    return [(row["id"], row["name"]) for row in rows]


async def backfill_native_asset_refs(
    *,
    vault: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> NativeAssetRefReport:
    """Publish live image references for Native documents that lack them."""
    report = NativeAssetRefReport()
    pool = await get_pool()
    repository = NativeRevisionRepository(pool)

    async with pool.acquire() as conn:
        targets = await _candidate_vaults(conn, vault)
        report.vaults = [name for _, name in targets]
        rows: list[tuple[uuid.UUID, str, uuid.UUID, str]] = []
        for vault_id, vault_name in targets:
            rows.extend(
                (vault_id, vault_name, row["resource_id"], row["current_path"])
                for row in await conn.fetch(
                    """
                    SELECT resource_id, current_path
                      FROM native_resources
                     WHERE namespace_id = $1
                       AND surface = 'document'
                       AND (lifecycle = 'live' OR EXISTS (
                           SELECT 1 FROM document_asset_refs refs
                            WHERE refs.native_document_id = native_resources.resource_id
                              AND refs.vault_id = native_resources.namespace_id
                       ))
                     ORDER BY current_path
                    """,
                    vault_id,
                )
            )

    if limit is not None and len(rows) > limit:
        rows = rows[:limit]
        report.complete = False

    for vault_id, vault_name, resource_id, path in rows:
        report.scanned += 1
        try:
            async with pool.acquire() as conn, conn.transaction():
                resource = await repository.lock_resource(conn, resource_id)
                if resource is None:
                    continue
                if resource["namespace_id"] != vault_id or resource["surface"] != "document":
                    raise ValueError("Native document identity changed")
                path = resource["current_path"]
                owner = DocumentAssetOwner(vault_id=vault_id, native_document_id=resource_id)
                current_refs = await vault_files_repo.list_live_document_asset_ids(conn, owner=owner)
                if resource["lifecycle"] != "live":
                    report.stale_refs += len(current_refs)
                    if not dry_run:
                        await conn.execute(
                            "DELETE FROM document_asset_refs WHERE vault_id=$1 AND native_document_id=$2",
                            vault_id, resource_id,
                        )
                        report.refs_removed += len(current_refs)
                    continue
                wanted = await native_document_assets.locked_head_image_ids(conn, repository, resource_id)
                report.with_images += bool(wanted)
                head = await repository.get_resource_head(resource_id=resource_id, conn=conn)
                if head is None:
                    raise ValueError("Native document Head is missing")
                if dry_run:
                    available = await conn.fetch(
                        """SELECT id FROM vault_files
                           WHERE vault_id=$1 AND id=ANY($2::uuid[])
                             AND kind='attachment' AND upload_state='confirmed'
                             AND hash_verified_at IS NOT NULL""",
                        vault_id, list(wanted),
                    )
                    found = {row["id"] for row in available}
                else:
                    found = await asset_service.claim_document_asset_ids(
                        conn, vault_id=vault_id, asset_ids=wanted, strict=False,
                    )
                    await vault_files_repo.sync_document_asset_references(
                        conn,
                        owner=owner,
                        document_path=path, commit_hash=head["revision_id"], asset_ids=found,
                        # Repair is not a new revision. Do not restart history's
                        # retention clock every time an operator repeats it.
                        retain_until=head["occurred_at"] + timedelta(
                            days=settings.document_asset_revision_retention_days,
                        ),
                    )
                report.stale_refs += len(current_refs - found)
                if not dry_run:
                    report.refs_removed += len(current_refs - found)
        except Exception as exc:  # noqa: BLE001 — report and keep walking
            report.failed += 1
            report.findings.append(f"{vault_name}:{path}: {type(exc).__name__}: {exc}"[:200])
            continue

        if not dry_run:
            report.refs_written += len(found)
        missing = wanted - found
        if missing:
            report.unresolved += len(missing)
            report.findings.append(
                f"{vault_name}:{path}: {len(missing)} asset id(s) no longer stored"
            )

    logger.info(
        "native asset refs: scanned=%d with_images=%d written=%d unresolved=%d failed=%d",
        report.scanned, report.with_images, report.refs_written,
        report.unresolved, report.failed,
    )
    return report
