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

from app.db.postgres import get_pool
from app.repositories.vault_files_repo import DocumentAssetOwner
from app.services import asset_service

logger = logging.getLogger("akb.native_asset_ref_backfill")


@dataclass
class NativeAssetRefReport:
    """What the walk saw, in the terms an operator has to check."""

    scanned: int = 0
    with_images: int = 0
    refs_written: int = 0
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
    from app.services.revision_backend import get_document_service

    report = NativeAssetRefReport()
    documents = get_document_service()
    pool = await get_pool()

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
                       AND lifecycle = 'live'
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
            document = await documents.get(vault_name, path)
        except Exception as exc:  # noqa: BLE001 — one unreadable document is not the run
            report.failed += 1
            report.findings.append(f"{vault_name}:{path}: unreadable ({type(exc).__name__})")
            continue

        body = document.content or ""
        wanted = await asset_service.extract_asset_ids_async(body)
        if not wanted:
            continue
        report.with_images += 1

        head = document.current_commit
        if not head:
            report.failed += 1
            report.findings.append(f"{vault_name}:{path}: no head revision")
            continue

        if dry_run:
            report.refs_written += len(wanted)
            continue

        try:
            async with pool.acquire() as conn, conn.transaction():
                found = await asset_service.claim_document_assets(
                    conn, vault_id=vault_id, markdown=body, strict=False,
                )
                await asset_service.sync_document_assets(
                    conn,
                    owner=DocumentAssetOwner(
                        vault_id=vault_id, native_document_id=resource_id,
                    ),
                    document_path=path,
                    commit_hash=head,
                    asset_ids=found,
                )
        except Exception as exc:  # noqa: BLE001 — report and keep walking
            report.failed += 1
            report.findings.append(f"{vault_name}:{path}: {type(exc).__name__}: {exc}"[:200])
            continue

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
