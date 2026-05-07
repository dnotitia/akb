"""Public sharing routes — unified document/table/file public access.

Authenticated endpoints (writer role required):
  POST   /publications/{vault}/create               — create a public publication
  DELETE /publications/{vault}/{publication_id}           — delete a publication
  GET    /publications/{vault}                      — list publications for a vault
  POST   /publications/{vault}/{publication_id}/snapshot  — create snapshot for table_query

Public endpoints (no auth):
  GET  /public/{slug}                 — resolve & render publication (dispatches by type)
  GET  /public/{slug}/meta            — metadata (esp. for files)
  GET  /public/{slug}/raw             — stream small text files for in-browser preview
  GET  /public/{slug}/download        — force download
  GET  /public/{slug}/embed           — embed-mode (minimal chrome)
  POST /public/{slug}/auth            — submit password, returns session token
  GET  /oembed                        — oEmbed endpoint for unfurling
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.config import settings
from app.db.postgres import get_pool
from app.util.text import NFCModel
from app.services import file_service, publication_service
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services.publication_service import (
    PublicationError,
    ResourceType,
    PublicationExpired,
    PublicationNotFound,
    PublicationPasswordInvalid,
    PublicationPasswordRequired,
    PublicationViewLimitReached,
    to_uuid,
)

router = APIRouter()


# ============================================================
# HMAC token for password-protected publications
# ============================================================

_TOKEN_TTL = 3600  # 1 hour


def _make_token(slug: str) -> str:
    ts = str(int(time.time()))
    msg = f"{slug}:{ts}".encode("utf-8")
    sig = hmac.new(settings.jwt_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def _verify_token(slug: str, token: str) -> bool:
    if not token:
        return False
    try:
        ts_str, sig = token.split(".", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return False
    if abs(time.time() - ts) > _TOKEN_TTL:
        return False
    msg = f"{slug}:{ts_str}".encode("utf-8")
    expected = hmac.new(settings.jwt_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ============================================================
# Request models
# ============================================================

class CreatePublicationRequest(NFCModel):
    resource_type: str = "document"  # 'document','table_query','file'
    doc_id: str | None = None
    file_id: str | None = None
    query_sql: str | None = None
    query_vault_names: list[str] | None = None
    query_params: dict | None = None
    password: str | None = None
    max_views: int | None = None
    expires_in: str | None = None  # '1h','7d','never'
    title: str | None = None
    mode: str = "live"
    section: str | None = None  # P5 section filter
    allow_embed: bool = True


class PasswordAuthRequest(NFCModel):
    password: str


# ============================================================
# Helpers
# ============================================================

def _publication_error_to_http(e: PublicationError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail=e.message)


# ============================================================
# Authenticated: publications CRUD
# ============================================================

@router.post("/publications/{vault}/create", summary="Create a public publication")
async def create_publication_route(
    vault: str,
    req: CreatePublicationRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        return await publication_service.create_publication_for_vault(
            vault_name=vault,
            resource_type=req.resource_type,
            doc_id=req.doc_id,
            file_id=req.file_id,
            query_sql=req.query_sql,
            query_vault_names=req.query_vault_names,
            query_params=req.query_params,
            password=req.password,
            max_views=req.max_views,
            expires_in=req.expires_in,
            title=req.title,
            mode=req.mode,
            section_filter=req.section,
            allow_embed=req.allow_embed,
            created_by=uuid.UUID(user.user_id),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/publications/{vault}/{publication_id}", summary="Delete a public publication")
async def delete_publication_route(
    vault: str,
    publication_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        sid = uuid.UUID(publication_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid publication_id")
    deleted = await publication_service.delete_publication(publication_id=sid)
    return {"deleted": deleted}


@router.get("/publications/{vault}", summary="List publications for a vault")
async def list_publications_route(
    vault: str,
    resource_type: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    publications = await publication_service.list_publications(access["vault_id"], resource_type)
    return {"publications": publications}


@router.post("/publications/{vault}/{publication_id}/snapshot", summary="Create snapshot for table_query publication")
async def create_snapshot_route(
    vault: str,
    publication_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    await check_vault_access(user.user_id, vault, required_role="writer")
    try:
        sid = uuid.UUID(publication_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid publication_id")
    try:
        return await publication_service.create_snapshot(sid)
    except PublicationError as e:
        raise _publication_error_to_http(e)


# ============================================================
# Public access (NO AUTH)
# ============================================================

def _extract_password(request: Request, body_password: str | None = None) -> str | None:
    """Extract password from query string, header, or body."""
    pw = request.query_params.get("password")
    if pw:
        return pw
    pw = request.headers.get("x-publication-password")
    if pw:
        return pw
    return body_password


def _extract_auth_token(request: Request) -> str | None:
    """Extract HMAC auth token from query string or cookie."""
    return request.query_params.get("token") or request.cookies.get("akb_publication_token")


async def _resolve_with_access(slug: str, request: Request, increment_view: bool = True) -> dict:
    """Resolve a publication, handling password and HMAC token."""
    # If a valid token is present, bypass password check
    token = _extract_auth_token(request)
    if token and _verify_token(slug, token):
        return await publication_service.resolve_publication(
            slug, password=None, increment_view=increment_view, bypass_password=True,
        )

    # Otherwise check password from request
    password = _extract_password(request)
    return await publication_service.resolve_publication(slug, password=password, increment_view=increment_view)


@router.post("/public/{slug}/auth", summary="Submit password for a publication")
async def publication_auth(slug: str, req: PasswordAuthRequest):
    """Verify password and return a short-lived HMAC token."""
    try:
        await publication_service.resolve_publication(slug, password=req.password, increment_view=False)
    except PublicationNotFound as e:
        raise _publication_error_to_http(e)
    except PublicationPasswordInvalid:
        raise HTTPException(status_code=401, detail="Invalid password")
    except PublicationError as e:
        raise _publication_error_to_http(e)

    return {"authorized": True, "token": _make_token(slug), "expires_in": _TOKEN_TTL}


@router.get("/public/{slug}/meta", summary="Get publication metadata (no content)")
async def publication_meta(slug: str, request: Request):
    """Return metadata about a publication without resolving full content.

    For files: returns mime_type, size, etc. so the frontend viewer can pick a renderer.
    Does NOT increment view_count.
    """
    try:
        publication = await _resolve_with_access(slug, request, increment_view=False)
    except PublicationNotFound as e:
        raise _publication_error_to_http(e)
    except (PublicationExpired, PublicationViewLimitReached, PublicationPasswordRequired, PublicationPasswordInvalid) as e:
        raise _publication_error_to_http(e)

    rt = publication["resource_type"]
    meta = {
        "resource_type": rt,
        "title": publication.get("title"),
        "expires_at": publication.get("expires_at"),
        "view_count": publication.get("view_count"),
        "max_views": publication.get("max_views"),
        "mode": publication.get("mode", "live"),
        "snapshot_at": publication.get("snapshot_at"),
        "allow_embed": publication.get("allow_embed", True),
    }

    if rt == ResourceType.FILE:
        # Get file basic info without presigned URL
        pool = await get_pool()
        async with pool.acquire() as conn:
            file_row = await conn.fetchrow(
                "SELECT name, mime_type, size_bytes FROM vault_files WHERE id = $1",
                to_uuid(publication["file_id"]),
            )
        if file_row:
            meta.update({
                "name": file_row["name"],
                "mime_type": file_row["mime_type"],
                "size_bytes": file_row["size_bytes"],
            })
    elif rt == ResourceType.DOCUMENT:
        meta["title"] = publication.get("title")
    elif rt == ResourceType.TABLE_QUERY:
        meta["query_params"] = publication.get("query_params") or {}

    return meta


_RAW_PREVIEW_MAX_BYTES = 5 * 1024 * 1024  # 5MB
_RAW_PREVIEWABLE_MIMES = {
    "application/json",
    "text/plain",
    "text/csv",
    "text/markdown",
    "text/html",
    "text/css",
    "text/javascript",
    "application/javascript",
    "application/xml",
    "text/xml",
    "application/x-yaml",
    "text/yaml",
}


@router.get("/public/{slug}/raw", summary="Stream file content for preview (small text files)")
async def publication_raw(slug: str, request: Request):
    """Proxy file content from S3 for in-browser preview.

    Only applies to small text-based file types (JSON, text, etc.) where
    CORS would block a direct presigned URL fetch. For images/PDFs the
    browser uses the direct presigned URL via <img>/<embed> tags.
    """
    try:
        publication = await _resolve_with_access(slug, request, increment_view=False)
    except PublicationError as e:
        raise _publication_error_to_http(e)

    if publication["resource_type"] != ResourceType.FILE:
        raise HTTPException(status_code=400, detail="Not a file publication")

    pool = await get_pool()
    async with pool.acquire() as conn:
        file_row = await conn.fetchrow(
            "SELECT s3_key, mime_type, size_bytes, name FROM vault_files WHERE id = $1",
            to_uuid(publication["file_id"]),
        )
    if not file_row:
        raise HTTPException(status_code=404, detail="File not found")

    if file_row["size_bytes"] and file_row["size_bytes"] > _RAW_PREVIEW_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large for preview, use /download instead")

    mime = file_row["mime_type"] or ""
    if not (mime in _RAW_PREVIEWABLE_MIMES or mime.startswith("text/")):
        raise HTTPException(status_code=415, detail=f"Preview not supported for mime type: {mime}")

    # Stream content from S3 via server (small files only — see _RAW_PREVIEW_MAX_BYTES)
    body = file_service.get_object_bytes(file_row["s3_key"])  # raises StorageError → 502
    return Response(content=body, media_type=mime)


@router.get("/public/{slug}/download", summary="Force download (file or csv)")
async def publication_download(slug: str, request: Request):
    """For files: 302 to presigned URL with attachment disposition.
    For table_query: returns CSV.
    For documents: returns raw markdown.
    """
    try:
        publication = await _resolve_with_access(slug, request, increment_view=True)
    except PublicationError as e:
        raise _publication_error_to_http(e)

    rt = publication["resource_type"]
    if rt == ResourceType.FILE:
        try:
            file_storage = await publication_service.get_file_storage_for_publication(publication)
        except PublicationError as e:
            raise _publication_error_to_http(e)
        # Proxy bytes through the backend instead of redirecting to the
        # presigned S3 URL. The S3 endpoint is HTTP on a private IP, and
        # browsers block HTTP downloads triggered from an HTTPS page
        # (mixed-content download). Streaming through the same HTTPS origin
        # sidesteps that and removes the cross-origin <a download> caveat.
        # Content-Length is intentionally omitted — let chunked encoding
        # handle the body so a DB/S3 size mismatch can't truncate the wire.
        return StreamingResponse(
            file_service.iter_object_chunks(file_storage["s3_key"]),
            media_type=file_storage.get("mime_type") or "application/octet-stream",
            headers={
                "Content-Disposition": file_service.content_disposition_attachment(
                    file_storage.get("name") or "download"
                )
            },
        )

    if rt == ResourceType.TABLE_QUERY:
        try:
            data = await publication_service.resolve_table_query_publication(
                publication, dict(request.query_params)
            )
        except PublicationError as e:
            raise _publication_error_to_http(e)
        return _to_csv_response(data)

    if rt == ResourceType.DOCUMENT:
        try:
            data = await publication_service.resolve_document_publication(publication)
        except PublicationError as e:
            raise _publication_error_to_http(e)
        return PlainTextResponse(
            content=data["content"],
            media_type="text/markdown",
            headers={
                "Content-Disposition": file_service.content_disposition_attachment(
                    f'{data["title"]}.md'
                )
            },
        )

    raise HTTPException(status_code=400, detail="Unknown resource type")


def _iter_table_cells(data: dict):
    """Yield (columns, rows) pairs for table rendering. Single source of truth."""
    columns = data.get("columns", [])
    rows = data.get("rows", [])
    return columns, rows


def _to_csv_response(data: dict) -> Response:
    columns, rows = _iter_table_cells(data)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(c) for c in columns])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": file_service.content_disposition_attachment("query.csv"),
        },
    )


@router.get("/public/{slug}", summary="Resolve and render a public publication")
async def get_public_publication(
    slug: str,
    request: Request,
    format: str | None = Query(None),
):
    """Universal public publication endpoint. Dispatches by resource_type.

    Format selection (table_query):
      ?format=json (default), ?format=csv, ?format=html
      Or via Accept header.
    """
    # Resolve with view-count increment
    try:
        publication = await _resolve_with_access(slug, request, increment_view=True)
    except PublicationNotFound as e:
        raise _publication_error_to_http(e)
    except PublicationPasswordRequired:
        return JSONResponse(
            status_code=401,
            content={"error": "Password required", "password_required": True, "slug": slug},
        )
    except PublicationPasswordInvalid:
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid password", "password_required": True, "slug": slug},
        )
    except (PublicationExpired, PublicationViewLimitReached) as e:
        raise _publication_error_to_http(e)

    rt = publication["resource_type"]

    if rt == ResourceType.DOCUMENT:
        try:
            data = await publication_service.resolve_document_publication(publication)
        except PublicationError as e:
            raise _publication_error_to_http(e)
        return data

    if rt == ResourceType.FILE:
        try:
            file_data = await publication_service.resolve_file_publication(publication)
        except PublicationError as e:
            raise _publication_error_to_http(e)
        # JSON metadata for the frontend viewer to route by mime_type.
        # Callers needing bytes use /public/{slug}/raw (preview, capped) or
        # /public/{slug}/download (force-download). The legacy ?format=raw
        # alias was removed — it had no callers and no size cap.
        return file_data

    if rt == ResourceType.TABLE_QUERY:
        url_params = dict(request.query_params)
        # Strip our own params
        for k in ("format", "password", "token"):
            url_params.pop(k, None)
        try:
            data = await publication_service.resolve_table_query_publication(publication, url_params)
        except PublicationError as e:
            raise _publication_error_to_http(e)

        # Format negotiation
        accept = request.headers.get("accept", "").lower()
        fmt = format or ("csv" if "text/csv" in accept else "json")
        if fmt == "csv":
            return _to_csv_response(data)
        if fmt == "html":
            return _to_html_table_response(data)
        return data

    raise HTTPException(status_code=400, detail=f"Unknown resource_type: {rt}")


def _to_html_table_response(data: dict) -> Response:
    columns, rows = _iter_table_cells(data)
    html_rows = ["<table border='1' cellpadding='4' cellspacing='0'>", "<thead><tr>"]
    html_rows += [f"<th>{_html_escape(c)}</th>" for c in columns]
    html_rows.append("</tr></thead><tbody>")
    for r in rows:
        html_rows.append("<tr>")
        html_rows += [f"<td>{_html_escape(r.get(c))}</td>" for c in columns]
        html_rows.append("</tr>")
    html_rows.append("</tbody></table>")
    return Response(content="\n".join(html_rows), media_type="text/html")


def _html_escape(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@router.get("/public/{slug}/embed", summary="Embed-friendly publication view (P5)")
async def publication_embed(slug: str, request: Request):
    """Same as get_public_publication but adds embed: true. Bypasses password (so the
    iframe doesn't double-prompt) but only if the publication has explicitly allowed embedding.
    """
    try:
        publication = await publication_service.resolve_publication(
            slug, password=None, increment_view=False, bypass_password=True,
        )
    except PublicationNotFound as e:
        raise _publication_error_to_http(e)
    except PublicationError as e:
        raise _publication_error_to_http(e)

    if not publication.get("allow_embed", True):
        raise HTTPException(status_code=403, detail="Embedding is disabled for this publication")

    # Password-protected publications cannot be embedded silently — require token
    if publication.get("password_hash"):
        token = _extract_auth_token(request)
        if not token or not _verify_token(slug, token):
            raise HTTPException(
                status_code=401,
                detail="Password-protected publications require an auth token to embed",
            )

    # Re-resolve via main path (which will increment view count)
    result = await get_public_publication(slug=slug, request=request, format=None)
    if isinstance(result, dict):
        result["embed"] = True
    return result


@router.get("/oembed", summary="oEmbed endpoint (P5)")
async def oembed(url: str, format: str = "json"):
    """oEmbed-compatible response for publication URLs.

    Slack/Discord/etc. call this to auto-unfurl publication links.
    """
    # Parse slug from URL (expects .../p/{slug})
    import re as _re
    m = _re.search(r"/p/([A-Za-z0-9_-]+)", url)
    if not m:
        raise HTTPException(status_code=400, detail="Invalid publication URL")
    slug = m.group(1)

    try:
        publication = await publication_service.resolve_publication(
            slug, password=None, increment_view=False, bypass_password=True,
        )
    except PublicationError as e:
        raise _publication_error_to_http(e)

    if not publication.get("allow_embed", True):
        raise HTTPException(status_code=403, detail="Embedding is disabled for this publication")

    rt = publication["resource_type"]

    # Resolve a useful title
    title = publication.get("title")
    if not title:
        if rt == ResourceType.DOCUMENT and publication.get("document_id"):
            pool = await get_pool()
            async with pool.acquire() as conn:
                doc_row = await conn.fetchrow(
                    "SELECT title FROM documents WHERE id = $1",
                    to_uuid(publication["document_id"]),
                )
                if doc_row:
                    title = doc_row["title"]
        elif rt == ResourceType.FILE and publication.get("file_id"):
            pool = await get_pool()
            async with pool.acquire() as conn:
                f_row = await conn.fetchrow(
                    "SELECT name FROM vault_files WHERE id = $1",
                    to_uuid(publication["file_id"]),
                )
                if f_row:
                    title = f_row["name"]
        elif rt == ResourceType.TABLE_QUERY:
            title = "Shared query"  # display title; ok to keep "shared" in user-facing copy
    title = title or "AKB Publication"

    return {
        "version": "1.0",
        "type": "rich" if rt != ResourceType.FILE else "link",
        "title": title,
        "provider_name": "AKB",
        "provider_url": "/",
        "html": f'<iframe src="/p/{slug}/embed" width="600" height="400" frameborder="0"></iframe>',
        "width": 600,
        "height": 400,
    }
