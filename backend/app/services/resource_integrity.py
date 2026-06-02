"""Operator repair helpers for AKB resource integrity projections."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import TypedDict

import frontmatter

from app.db.postgres import get_pool
from app.repositories import vault_files_repo
from app.services import file_service
from app.services.git_service import GitService
from app.services.resource_hash import (
    HASH_ALGORITHM,
    compute_stream_content_hash,
    compute_text_content_hash,
)


@dataclass
class ResourceHashRepairReport:
    documents_checked: int = 0
    documents_repaired: int = 0
    files_checked: int = 0
    files_repaired: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["errors"] = data["errors"] or []
        return data


def document_hash_projection(raw_markdown: str, current_commit: str | None) -> dict[str, str | None]:
    """Compute the stored body-hash projection for one markdown document."""

    body = frontmatter.loads(raw_markdown).content if raw_markdown else ""
    return {
        "content_hash": compute_text_content_hash(body),
        "hash_algorithm": HASH_ALGORITHM,
        "content_hash_commit": current_commit,
    }


class FileHashProjection(TypedDict):
    size_bytes: int
    content_hash: str
    hash_algorithm: str
    etag: str | None
    storage_version: str | None


def file_hash_projection(
    *,
    chunks,
    head: dict,
) -> FileHashProjection:
    """Compute the stored byte-hash projection for one S3-backed file."""

    return {
        "size_bytes": head["ContentLength"],
        "content_hash": compute_stream_content_hash(chunks),
        "hash_algorithm": HASH_ALGORITHM,
        "etag": (head.get("ETag") or "").strip('"') or None,
        "storage_version": head.get("VersionId"),
    }


async def repair_resource_hashes(
    *,
    vault: str | None = None,
    include_documents: bool = True,
    include_files: bool = True,
    limit: int = 100,
    git: GitService | None = None,
) -> dict:
    """Repair missing/stale document and file content-hash projections.

    This is intentionally an operator path. Normal reads lazily repair
    documents, and normal uploads populate files. The repair command backfills
    older rows without making collectors inspect private AKB storage.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    report = ResourceHashRepairReport(errors=[])
    pool = await get_pool()
    git_service = git or GitService()

    async with pool.acquire() as conn:
        if include_documents:
            doc_rows = await _document_repair_rows(conn, vault=vault, limit=limit)
            report.documents_checked = len(doc_rows)
            for row in doc_rows:
                try:
                    raw = await asyncio.to_thread(
                        git_service.read_file,
                        row["vault_name"],
                        row["path"],
                        row["current_commit"],
                    )
                    projection = document_hash_projection(raw or "", row["current_commit"])
                    await conn.execute(
                        """
                        UPDATE documents SET
                            content_hash = $1,
                            hash_algorithm = $2,
                            content_hash_commit = $3
                        WHERE id = $4
                        """,
                        projection["content_hash"],
                        projection["hash_algorithm"],
                        projection["content_hash_commit"],
                        row["id"],
                    )
                    report.documents_repaired += 1
                except Exception as error:  # noqa: BLE001
                    report.errors.append(f"document {row['vault_name']}/{row['path']}: {error}")

        if include_files:
            file_rows = await _file_repair_rows(conn, vault=vault, limit=limit)
            report.files_checked = len(file_rows)
            for row in file_rows:
                try:
                    head = await asyncio.to_thread(file_service.s3_adapter.head, row["s3_key"])
                    file_projection = await asyncio.to_thread(
                        file_hash_projection,
                        chunks=file_service.s3_adapter.iter_chunks(row["s3_key"]),
                        head=head,
                    )
                    await vault_files_repo.update_confirmed_metadata(
                        conn,
                        row["id"],
                        size_bytes=file_projection["size_bytes"],
                        content_hash=file_projection["content_hash"],
                        hash_algorithm=file_projection["hash_algorithm"],
                        etag=file_projection["etag"],
                        storage_version=file_projection["storage_version"],
                    )
                    report.files_repaired += 1
                except Exception as error:  # noqa: BLE001
                    report.errors.append(f"file {row['vault_name']}/{row['id']}: {error}")

    return report.to_dict()


async def _document_repair_rows(conn, *, vault: str | None, limit: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT d.id, v.name AS vault_name, d.path, d.current_commit,
               d.content_hash, d.hash_algorithm, d.content_hash_commit
          FROM documents d
          JOIN vaults v ON v.id = d.vault_id
         WHERE ($1::text IS NULL OR v.name = $1)
           AND (
                d.content_hash IS NULL
             OR d.hash_algorithm IS DISTINCT FROM $2
             OR d.content_hash_commit IS DISTINCT FROM d.current_commit
           )
         ORDER BY d.updated_at DESC
         LIMIT $3
        """,
        vault,
        HASH_ALGORITHM,
        limit,
    )
    return [dict(row) for row in rows]


async def _file_repair_rows(conn, *, vault: str | None, limit: int) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT vf.id, v.name AS vault_name, vf.s3_key,
               vf.content_hash, vf.hash_algorithm
          FROM vault_files vf
          JOIN vaults v ON v.id = vf.vault_id
         WHERE ($1::text IS NULL OR v.name = $1)
           AND (
                vf.content_hash IS NULL
             OR vf.hash_algorithm IS DISTINCT FROM $2
           )
         ORDER BY vf.updated_at DESC
         LIMIT $3
        """,
        vault,
        HASH_ALGORITHM,
        limit,
    )
    return [dict(row) for row in rows]


__all__ = [
    "ResourceHashRepairReport",
    "document_hash_projection",
    "file_hash_projection",
    "repair_resource_hashes",
]
