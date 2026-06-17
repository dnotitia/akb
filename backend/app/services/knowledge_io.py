"""Knowledge bundle export/import — format-dispatch layer.

Turns an AKB vault into a portable knowledge bundle (and back). Today the only
registered format is **OKF** (Open Knowledge Format, see ``app.services.okf``),
but every entry point takes a ``fmt`` argument and dispatches through the
``EXPORTERS`` / ``IMPORTERS`` registries, so adding another format later is a
registration, not a rewrite.

Surfaces that call this:
  * REST  — ``GET /api/v1/vaults/{vault}/export`` / ``POST .../import``
  * MCP   — ``akb_export`` / ``akb_import`` tools

Export reads a vault via the existing ``DocumentService`` (browse + per-doc
get), so documents carry their real body, and tables/files become OKF *concept
documents* (schema / metadata + a ``resource`` pointer). Import is
document-oriented: OKF carries no rows/bytes, so a ``type: table``/``file``
concept doc imports as a regular document describing that asset.
"""
from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping
from typing import Any

from app.exceptions import ConflictError
from app.models.document import DocumentPutRequest
from app.services import okf
from app.util.text import slugify

# Registered bundle formats. `fmt` values accepted by every entry point.
SUPPORTED_FORMATS = ("okf",)

# Guard rails for untrusted import archives (zip-bomb / runaway payloads).
_MAX_BUNDLE_FILES = 10_000
_MAX_BUNDLE_BYTES = 64 * 1024 * 1024  # 64 MiB uncompressed


def _require_format(fmt: str) -> str:
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported format '{fmt}'. Supported: {', '.join(SUPPORTED_FORMATS)}"
        )
    return fmt


# ─────────────────────────────────────────────────────────────────────────────
# Export: vault → bundle (path → content)
# ─────────────────────────────────────────────────────────────────────────────
def _table_record(item: Any) -> dict[str, Any]:
    coll = (item.collection or "").strip("/")
    base = slugify(item.name)
    path = f"{coll}/{base}" if coll else base
    return {
        "uri": item.uri,
        "path": path,
        "name": item.name,
        "title": item.name,
        "description": item.summary,
        "columns": item.columns or [],
        "row_count": item.row_count,
        "sql_name": item.sql_name,
        "tags": item.tags,
        "updated_at": item.last_updated,
    }


def _file_record(item: Any) -> dict[str, Any]:
    coll = (item.collection or "").strip("/")
    base = slugify(item.name)
    path = f"{coll}/{base}" if coll else base
    return {
        "uri": item.uri,
        "path": path,
        "name": item.name,
        "title": item.name,
        "description": item.summary,
        "mime_type": item.mime_type,
        "size_bytes": item.size_bytes,
        "tags": item.tags,
        "updated_at": item.last_updated,
    }


async def export_vault(vault_name: str, *, fmt: str = "okf", doc_service: Any) -> dict[str, str]:
    """Build a bundle (bundle-relative path → content) for an entire vault.

    Caller must have already authorized read access. ``doc_service`` is an
    instance of ``DocumentService`` (injected to avoid an import cycle / to
    reuse the module-level singleton the surfaces already hold).
    """
    _require_format(fmt)
    browse = await doc_service.browse(
        vault_name, collection=None, depth=-1, content_type="all", include_archived=False
    )
    documents: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for item in browse.items:
        if item.type == "document":
            resp = await doc_service.get(vault_name, item.path)
            if resp is not None:
                documents.append(resp.model_dump())
        elif item.type == "table":
            tables.append(_table_record(item))
        elif item.type == "file":
            files.append(_file_record(item))
    return okf.build_bundle(documents=documents, tables=tables, files=files)


# ─────────────────────────────────────────────────────────────────────────────
# Import: bundle (path → content) → vault documents
# ─────────────────────────────────────────────────────────────────────────────
async def import_bundle(
    vault_name: str,
    files: Mapping[str, str],
    *,
    fmt: str = "okf",
    actor_id: str,
    doc_service: Any,
    status: str | None = None,
) -> dict[str, Any]:
    """Import a bundle's concept documents into ``vault_name``.

    Caller must have already authorized write access. Existing paths are
    skipped (not overwritten); per-document failures are collected rather than
    aborting the whole import. ``status`` overrides each doc's status when set.
    """
    _require_format(fmt)
    records = okf.parse_okf_bundle(files)
    created: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    for rec in records:
        req = DocumentPutRequest(
            vault=vault_name,
            collection=rec["collection"],
            slug=rec["slug"],
            title=rec["title"],
            content=rec["content"],
            type=rec["type"],
            status=status or rec["status"],
            tags=rec["tags"],
            summary=rec.get("summary"),
            domain=rec.get("domain"),
        )
        try:
            resp = await doc_service.put(req, agent_id=actor_id)
            created.append(resp.uri)
        except ConflictError:
            skipped.append(rec["path"])
        except Exception as exc:  # noqa: BLE001 — collect, don't abort the batch
            errors.append({"path": rec["path"], "error": str(exc)})
    return {
        "format": fmt,
        "vault": vault_name,
        "created": len(created),
        "skipped": len(skipped),
        "failed": len(errors),
        "uris": created,
        "skipped_paths": skipped,
        "errors": errors,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Zip (de)serialisation — REST's binary transport
# ─────────────────────────────────────────────────────────────────────────────
def bundle_to_zip(files: Mapping[str, str]) -> bytes:
    """Serialise a bundle (path → content) to a deterministic zip archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            zf.writestr(path, files[path])
    return buffer.getvalue()


def zip_to_bundle(data: bytes) -> dict[str, str]:
    """Read a zip archive into a bundle (path → content).

    Guards against runaway archives (file count + total uncompressed size) and
    skips directory entries. Decodes as UTF-8 (OKF bundles are UTF-8 markdown).
    """
    out: dict[str, str] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_BUNDLE_FILES:
            raise ValueError(f"bundle has too many entries (>{_MAX_BUNDLE_FILES})")
        for info in infos:
            if info.is_dir():
                continue
            total += info.file_size
            if total > _MAX_BUNDLE_BYTES:
                raise ValueError("bundle exceeds maximum uncompressed size")
            rel = info.filename.replace("\\", "/").lstrip("/")
            if not rel or ".." in rel.split("/"):
                continue  # ignore absolute / traversal paths
            out[rel] = zf.read(info).decode("utf-8", errors="replace")
    return out
