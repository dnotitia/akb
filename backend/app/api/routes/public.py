"""Public sharing routes — unified document/table/file public access.

Authenticated endpoints (writer role required):
  POST   /publications/{vault}/create        — create a public publication
  DELETE /publications/{vault}/{slug}        — delete a publication
  GET    /publications/{vault}               — list publications for a vault
  POST   /publications/{vault}/{slug}/snapshot — create snapshot for table_query

Public endpoints (no auth):
  GET  /public/{slug}                 — resolve & render publication (dispatches by type)
  GET  /public/{slug}/meta            — metadata (esp. for files)
  GET  /public/{slug}/raw             — stream small text files for in-browser preview
  GET  /public/{slug}/download        — force download
  GET  /public/{slug}/embed           — embed-mode (minimal chrome)
  POST /public/{slug}/auth            — submit password, returns session token
  GET  /oembed                        — oEmbed endpoint for unfurling

``slug`` is the single external identifier for a publication. All write
endpoints take it in the URL path; the response of every CRUD endpoint
is the canonical publication dict produced by
``publication_service.to_public_dict`` (or a list of them).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import ConfigDict

from app.api.deps import get_current_user, get_optional_user
from app.exceptions import ForbiddenError, NotFoundError
from app.config import settings
from app.db.postgres import get_pool
from app.util.text import NFCModel
from app.services import audit_log, file_service, publication_service
from app.services import publication_rate_limit as pub_rl
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
logger = logging.getLogger("akb.publications.public")


# ============================================================
# HMAC token for password-protected publications
# ============================================================

_TOKEN_TTL = 3600  # 1 hour


def _make_token(slug: str) -> str:
    # Bound to the slug (not the password): publications are create-only, so a
    # password "change" is an unpublish + republish, which mints a NEW slug —
    # tokens for the old slug are already dead. If an in-place password-update
    # endpoint is ever added, bind this token to the password_hash too (and make
    # _verify_token re-check it) or those tokens won't revoke. (M3.)
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
# View-grant: proves a COUNTED page open, so the paired /raw and /download
# re-serves of that same view don't re-count and stay usable at the last allowed
# view. Distinct from the password token (different HMAC purpose prefix) so one
# can't substitute for the other: a password token must NOT skip view-counting,
# and a grant must NOT bypass the password gate. WITHOUT a grant, /raw and
# /download each count as their own view and are capped — so max_views is a HARD
# cap on every content-delivery path, not just the page GET. TTL is short: it
# only has to outlive a single page session's preview→download, and a short
# window bounds how long a leaked/shared grant URL can fetch without counting.
# ============================================================

_VIEW_GRANT_TTL = 600  # 10 minutes


def _make_view_grant(slug: str) -> str:
    ts = str(int(time.time()))
    msg = f"grant:{slug}:{ts}".encode("utf-8")
    sig = hmac.new(settings.jwt_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def _verify_view_grant(slug: str, grant: str | None) -> bool:
    if not grant:
        return False
    try:
        ts_str, sig = grant.split(".", 1)
        ts = int(ts_str)
    except (ValueError, AttributeError):
        return False
    if abs(time.time() - ts) > _VIEW_GRANT_TTL:
        return False
    msg = f"grant:{slug}:{ts_str}".encode("utf-8")
    expected = hmac.new(settings.jwt_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _extract_view_grant(request: Request) -> str | None:
    """The view-grant from the query string or cookie. Query-string is
    acceptable here (unlike the password): the grant is not a secret — it only
    suppresses re-counting an already-counted view for a short window."""
    return request.query_params.get("grant") or request.cookies.get("akb_publication_grant")


# ============================================================
# Request models
# ============================================================

class CreatePublicationRequest(NFCModel):
    """Request body for `POST /publications/{vault}/create`.

    URI-canonical: for document/file publications, pass the resource
    `uri` (`akb://{vault}/doc/{path}` or `akb://{vault}/file/{id}`).
    For table_query publications, pass `query_sql` plus optional
    `query_vault_names`; no per-resource handle is needed since the
    query is the publishable surface.

    Every publication is created live. Snapshot is a table_query-only
    state transition reached via `POST /publications/{vault}/{slug}/snapshot`,
    not a create-time option.

    ``extra='forbid'`` here is deliberate: a typo like ``mode`` or
    ``section`` (both removed in 0.6.0) should 422 instead of being
    silently dropped. Quiet drops are exactly the "did this option
    apply?" ambiguity we're trying to design out of this surface.
    """
    model_config = ConfigDict(extra="forbid")

    resource_type: str = "document"  # 'document','table_query','file'
    uri: str | None = None
    query_sql: str | None = None
    query_vault_names: list[str] | None = None
    query_params: dict | None = None
    password: str | None = None
    max_views: int | None = None
    expires_in: str | None = None  # '1h','7d','never'
    title: str | None = None
    section_filter: str | None = None  # document-only, filters to one heading section
    allow_embed: bool = True


class PasswordAuthRequest(NFCModel):
    password: str


# ============================================================
# Helpers
# ============================================================

def _publication_error_to_http(e: PublicationError) -> HTTPException:
    # These distinct codes (404 not-found, 401 password-required, 410
    # expired/view-exhausted, 429 throttled) let an anonymous holder of the link
    # tell a publication's state apart. That disclosure was reviewed and
    # deliberately accepted (publish-hardening F9, informational): the slug is a
    # 96-bit unguessable capability, so anyone seeing these codes already holds
    # the link, and the frontend NEEDS the distinction to render the password
    # gate vs an expired/removed notice. Collapsing everything to 404 would break
    # the UX and buy no real secrecy.
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
    # A table_query publication runs against EVERY vault in
    # query_vault_names, served to unauthenticated visitors. Authorize
    # each one so a writer on one vault cannot publish a query that reads
    # another vault's tables (cross-vault exfiltration).
    if req.resource_type == "table_query":
        for qv in (req.query_vault_names or [vault]):
            if qv != vault:
                await check_vault_access(user.user_id, qv, required_role="writer")
    # Split the canonical URI into the service-layer args
    # (doc path / file uuid). Reject mismatches between resource_type
    # and URI scheme so callers can't smuggle a doc URI into a file
    # publication or vice versa.
    doc_id: str | None = None
    file_id: str | None = None
    if req.resource_type in ("document", "file"):
        if not req.uri:
            raise HTTPException(
                status_code=400,
                detail=f"`uri` is required for resource_type={req.resource_type!r}",
            )
        from app.services.uri_service import parse_uri
        parsed = parse_uri(req.uri)
        if parsed is None:
            raise HTTPException(status_code=400, detail=f"Invalid AKB URI: {req.uri!r}")
        uri_vault, uri_type, uri_ident = parsed.vault, parsed.kind, parsed.identifier
        if uri_vault != vault:
            raise HTTPException(
                status_code=400,
                detail=f"URI vault {uri_vault!r} does not match route vault {vault!r}",
            )
        if req.resource_type == "document":
            if uri_type != "doc":
                raise HTTPException(
                    status_code=400,
                    detail=f"resource_type=document needs a doc URI, got {uri_type}",
                )
            doc_id = uri_ident
        else:  # file
            if uri_type != "file":
                raise HTTPException(
                    status_code=400,
                    detail=f"resource_type=file needs a file URI, got {uri_type}",
                )
            file_id = uri_ident
    try:
        return await publication_service.create_publication_for_vault(
            vault_name=vault,
            resource_type=req.resource_type,
            doc_id=doc_id,
            file_id=file_id,
            query_sql=req.query_sql,
            query_vault_names=req.query_vault_names,
            query_params=req.query_params,
            password=req.password,
            max_views=req.max_views,
            expires_in=req.expires_in,
            title=req.title,
            section_filter=req.section_filter,
            allow_embed=req.allow_embed,
            created_by=uuid.UUID(user.user_id),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/publications/{vault}/{slug}", summary="Delete a public publication")
async def delete_publication_route(
    vault: str,
    slug: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    # Bind the delete to the authorized vault — without this a writer on
    # `vault` could delete any publication by slug regardless of owner (IDOR).
    ok = await publication_service.delete_publication(
        slug=slug, expected_vault_id=access["vault_id"],
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Publication not found in this vault")
    return {"deleted": 1}


@router.get("/publications/{vault}", summary="List publications for a vault")
async def list_publications_route(
    vault: str,
    resource_type: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List every publication in the vault. Unpaginated by contract.

    Two callers depend on the response being complete, and a `limit`/`offset`
    pair added here would break them in opposite ways:

    - `frontend/src/lib/api.ts` `listPublications` renders the vault's
      publication list from one unbounded response, so a bound would silently
      hide rows from the owner.
    - `backend/tests/test_publications_e2e.sh` asserts the cascade on document
      and file delete by counting matching rows here. A surviving row that fell
      off the first page would count as zero and read as "cascade removed it" —
      the assertion would pass for the wrong reason rather than fail, which is
      the one failure mode a test cannot report on itself.

    Paginating is fine; doing it without changing both is not.
    """
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    publications = await publication_service.list_publications(access["vault_id"], resource_type)
    return {"publications": publications}


@router.get("/public/{slug}/capabilities", summary="Owner capabilities for a publication (optional auth)")
async def publication_capabilities(
    slug: str,
    user: AuthenticatedUser | None = Depends(get_optional_user),
):
    """Anonymous-first: returns ``{"can_edit": false}`` unless the *current*
    session can write to this publication's vault. Read-only and side-effect
    free; it reveals only the capability plus where to manage the source (never
    the owner's name, email, or account existence), so the public page can add a
    quiet owner toolbar client-side without leaking anything to anonymous
    viewers. The read-only public link stays the canonical audience view — this
    just gives the owner a route back into the app. (publish-hardening F6.)
    """
    if user is None:
        return {"can_edit": False}
    pub = await publication_service.get_publication_by_slug(slug)
    if pub is None:
        return {"can_edit": False}
    try:
        await check_vault_access(user.user_id, pub["vault"], required_role="writer")
    except (ForbiddenError, NotFoundError):
        return {"can_edit": False}
    # Authorized: safe to name the vault + resource kind (the caller can already
    # read/write them) so the client can deep-link "Manage" / "Open in AKB".
    return {
        "can_edit": True,
        "vault": pub["vault"],
        "resource_type": pub["resource_type"],
    }


@router.post("/publications/{vault}/{slug}/snapshot", summary="Create snapshot for table_query publication")
async def create_snapshot_route(
    vault: str,
    slug: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM publications WHERE slug = $1 AND vault_id = $2",
            slug, access["vault_id"],
        )
    if not row:
        raise HTTPException(status_code=404, detail="Publication not found in this vault")
    try:
        return await publication_service.create_snapshot(
            row["id"], expected_vault_id=access["vault_id"],
        )
    except PublicationError as e:
        raise _publication_error_to_http(e)


# ============================================================
# Public access (NO AUTH)
# ============================================================

def _extract_password(request: Request, body_password: str | None = None) -> str | None:
    """Extract the publication password from the `x-publication-password`
    header or the request body — NEVER the query string.

    A password in the query string (`?password=…`) leaks through browser
    history, server/proxy access logs, `Referer` headers, analytics, and
    copy-pasted/forwarded URLs. The frontend submits the password via
    POST /public/{slug}/auth (JSON body) and then carries the returned
    short-lived HMAC token (`?token=`/cookie); programmatic callers use the
    header or a body field. (publish-hardening M1.)
    """
    pw = request.headers.get("x-publication-password")
    if pw:
        return pw
    return body_password


def _extract_auth_token(request: Request) -> str | None:
    """Extract HMAC auth token from query string or cookie."""
    return request.query_params.get("token") or request.cookies.get("akb_publication_token")


def _client_ip(request: Request) -> str:
    """Best-effort client IP for throttling. Behind the ingress the socket peer
    is the proxy, so prefer the leftmost X-Forwarded-For hop (the original
    client). It is client-spoofable, which is exactly why the per-slug backstop
    in publication_rate_limit does not rely on the IP alone."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _audit(**kwargs) -> None:
    """Record one audit line. ``audit_log.record`` is already non-blocking — it
    enqueues for a single dedicated writer thread (bounded, drop-on-overflow, off
    the shared executor) — so the password hot path can call it inline without
    ever serializing disk I/O next to the awaited bcrypt verify (the stall class
    behind past 503s). Kept as a thin alias so call sites read clearly."""
    audit_log.record(**kwargs)


_MANAGER_ROLE_SOURCES = frozenset({"member", "system_admin", "write_policy_admin_bypass"})


async def _is_publication_manager(request: Request, slug: str) -> bool:
    """True iff the request carries a session that can WRITE the publication's
    vault as a REAL manager — the owner, a writer/admin member, or a system
    admin. Fail-closed: any missing session, unknown slug, or access error
    returns False. Used to let the authenticated owner bypass the anonymous
    password throttle so a flood can't lock them out.

    Crucially, a vault with ``public_access="writer"`` would let ANY logged-in
    user pass the writer check (role_source="public") — that must NOT grant the
    throttle bypass, or the anti-brute-force guarantee would silently vanish for
    that whole class of publication. So we require a non-public role_source
    (allowlist, so an unknown future source fails closed)."""
    try:
        user = await get_optional_user(request)
        if user is None:
            return False
        pub = await publication_service.get_publication_by_slug(slug)
        if pub is None:
            return False
        access = await check_vault_access(user.user_id, pub["vault"], required_role="writer")
        return access.get("role_source") in _MANAGER_ROLE_SOURCES
    except (ForbiddenError, NotFoundError):
        return False
    except Exception:  # noqa: BLE001 — the throttle bypass must fail closed
        return False


async def _attempt_password_resolve(
    slug: str, *, password: str | None, ip: str,
    increment_view: bool, bypass_password: bool = False,
    enforce_view_cap: bool = True, request: Request | None = None,
) -> dict:
    """`resolve_publication` wrapped with the F2 password-attempt throttle.

    Only genuine wrong-password attempts are counted: token/bypass requests and
    the "no password supplied" case (which yields PublicationPasswordRequired,
    not Invalid) never touch the limiter. On lockout we return 429 with
    Retry-After and audit the event — unless the caller is the authenticated
    owner, who bypasses the throttle entirely (outside anonymous accounting).
    """
    throttled = password is not None and not bypass_password
    owner_bypass = False
    if throttled:
        # Count this attempt BEFORE the (slow, awaited) bcrypt verify so a
        # concurrent burst can't all slip past a stale counter.
        lock = pub_rl.reserve(slug, ip)
        if lock > 0:
            # Locked. An anonymous flood (rotating IPs) can trip the per-slug
            # backstop; let the authenticated owner through so they're never shut
            # out. reserve() did NOT bump on the locked path, and we skip
            # release() below, so the owner attempt stays entirely outside the
            # anonymous throttle buckets (an attacker can't piggyback on it).
            if request is not None and await _is_publication_manager(request, slug):
                owner_bypass = True
            else:
                _audit(
                    action="publication.auth.throttled", target=slug,
                    outcome="error", code="rate_limited",
                    meta={"ip": ip, "lock_seconds": int(lock)},
                )
                raise HTTPException(
                    status_code=429,
                    detail="Too many password attempts. Please wait and try again.",
                    headers={"Retry-After": str(int(lock) + 1)},
                )
    try:
        pub = await publication_service.resolve_publication(
            slug, password=password, increment_view=increment_view,
            bypass_password=bypass_password, enforce_view_cap=enforce_view_cap,
        )
    except PublicationPasswordInvalid:
        # Already counted by reserve() above. Record the failed attempt for the
        # audit trail (a brute-force signal), then let the 401 surface. (M3.)
        _audit(
            action="publication.auth.failed", target=slug,
            outcome="error", code="invalid_password", meta={"ip": ip},
        )
        raise
    if throttled and not owner_bypass:
        pub_rl.release(slug, ip)  # verified correct — undo the speculative count
    return pub


async def _resolve_with_access(
    slug: str, request: Request, increment_view: bool = True, enforce_view_cap: bool = True,
) -> dict:
    """Resolve a publication, handling password and HMAC token."""
    # If a valid token is present, bypass password check
    token = _extract_auth_token(request)
    if token and _verify_token(slug, token):
        return await publication_service.resolve_publication(
            slug, password=None, increment_view=increment_view, bypass_password=True,
            enforce_view_cap=enforce_view_cap,
        )

    # Otherwise check password from request (throttled against brute force)
    password = _extract_password(request)
    return await _attempt_password_resolve(
        slug, password=password, ip=_client_ip(request), increment_view=increment_view,
        enforce_view_cap=enforce_view_cap, request=request,
    )


@router.post("/public/{slug}/auth", summary="Submit password for a publication")
async def publication_auth(slug: str, req: PasswordAuthRequest, request: Request):
    """Verify password and return a short-lived HMAC token.

    This is the primary brute-force target, so it goes through the same
    throttle as the content path (429 after repeated wrong passwords).
    """
    ip = _client_ip(request)
    try:
        pub = await _attempt_password_resolve(
            slug, password=req.password, ip=ip, increment_view=False, request=request,
        )
    except PublicationNotFound as e:
        raise _publication_error_to_http(e)
    except PublicationPasswordInvalid:
        raise HTTPException(status_code=401, detail="Invalid password")
    except PublicationError as e:
        raise _publication_error_to_http(e)

    # Record the successful authentication (the "login" event for this share). (M3.)
    _audit(
        action="publication.auth.success", target=slug,
        vault=pub.get("vault"), outcome="ok", meta={"ip": ip},
    )
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
        # Get file basic info without presigned URL. Pull the UUID
        # tail off the canonical URI rather than reading a separate
        # column — there is no separate column anymore.
        from app.services.uri_service import parse_uri
        parsed = parse_uri(publication.get("resource_uri") or "")
        file_uuid_str = parsed.identifier if parsed and parsed.kind == "file" else None
        if file_uuid_str:
            pool = await get_pool()
            async with pool.acquire() as conn:
                file_row = await conn.fetchrow(
                    "SELECT name, mime_type, size_bytes FROM vault_files WHERE id = $1 AND vault_id = $2",
                    to_uuid(file_uuid_str), to_uuid(publication["vault_id"]),
                )
            if file_row:
                meta.update({
                    "name": file_row["name"],
                    "mime_type": file_row["mime_type"],
                    "size_bytes": file_row["size_bytes"],
                })
    elif rt == ResourceType.TABLE_QUERY:
        meta["query_params"] = publication.get("query_params") or {}
    # DOCUMENT path needs no per-type augmentation — meta["title"] was
    # already set from the publication dict above.

    return meta


_RAW_TEXT_MAX_BYTES = 5 * 1024 * 1024        # 5MB — text/JSON inline preview
_RAW_INLINE_BINARY_MAX_BYTES = 25 * 1024 * 1024  # 25MB — image/PDF inline render (streamed)
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
# Binary types the browser renders inline via <img>/<embed>. Served here (same
# origin, streamed, view-counted) instead of a presigned S3 URL so the vault
# name embedded in the S3 key never leaks and the view stays revocable. (F4)
_RAW_INLINE_BINARY_MIMES = {"application/pdf"}
_RAW_INLINE_IMAGE_PREFIX = "image/"
# Provably-inert types served WITHOUT a CSP sandbox: raster images, PDF, and
# plain/CSV/markdown text. Everything else /raw serves (text/html, xml, js, any
# future active-document mime) is sandboxed by default — fail closed.
_RAW_INERT_MIMES = {
    "application/pdf",
    "text/plain",
    "text/csv",
    "text/markdown",
}
# image/svg+xml is an image by MIME but an ACTIVE document — it can carry
# <script> that runs same-origin on direct navigation. It must NOT ride the
# generic image/ inert exemption; keep it sandboxed like HTML.
_RAW_ACTIVE_IMAGE_MIMES = {"image/svg+xml", "image/svg"}


def _is_inert_raw_mime(mime: str) -> bool:
    """True when a /raw body can be served without a CSP sandbox — a raster
    image, PDF, or plain text. SVG is explicitly excluded (scriptable), so it
    falls through to the sandboxed default even though it starts with image/."""
    if mime in _RAW_ACTIVE_IMAGE_MIMES:
        return False
    if mime.startswith(_RAW_INLINE_IMAGE_PREFIX):
        return True
    return mime in _RAW_INERT_MIMES


@router.get(
    "/public/{slug}/raw",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                },
                "text/plain": {"schema": {"type": "string"}},
            },
            "description": "Raw preview bytes for a small text-like file",
        }
    },
    summary="Stream file content for inline preview (text, image, PDF)",
)
async def publication_raw(slug: str, request: Request):
    """Proxy file content from S3 for same-origin in-browser preview.

    Serves text/JSON (fetched + rendered by the SPA) and image/PDF (rendered
    via <img>/<embed>) through this origin instead of handing out a presigned
    S3 URL. That keeps the vault name embedded in the S3 key from leaking, and
    keeps the view access-checked, counted, and instantly revocable. Larger
    files fall back to /download. (publish-hardening F4.)
    """
    try:
        # A valid view-grant means the paired page GET already counted this view,
        # so re-serve WITHOUT counting or re-checking the cap (a page opened at
        # its last allowed view can still fetch its bytes / Range requests).
        # WITHOUT a grant this is a direct fetch = its own view: count it and
        # enforce the cap, so max_views can't be bypassed by hitting /raw
        # directly. Expiry and password are enforced either way.
        has_grant = _verify_view_grant(slug, _extract_view_grant(request))
        publication = await _resolve_with_access(
            slug, request, increment_view=not has_grant, enforce_view_cap=not has_grant,
        )
    except PublicationError as e:
        raise _publication_error_to_http(e)

    if publication["resource_type"] != ResourceType.FILE:
        raise HTTPException(status_code=400, detail="Not a file publication")

    from app.services.uri_service import parse_uri
    parsed = parse_uri(publication.get("resource_uri") or "")
    if not parsed or parsed.kind != "file":
        raise HTTPException(status_code=404, detail="File not found")

    pool = await get_pool()
    async with pool.acquire() as conn:
        file_row = await conn.fetchrow(
            "SELECT s3_key, mime_type, size_bytes, name FROM vault_files WHERE id = $1 AND vault_id = $2",
            to_uuid(parsed.identifier), to_uuid(publication["vault_id"]),
        )
    if not file_row:
        raise HTTPException(status_code=404, detail="File not found")

    # Normalize so "image/svg+xml; charset=utf-8" still matches the sets below.
    mime = (file_row["mime_type"] or "").split(";", 1)[0].strip().lower()
    is_text = mime in _RAW_PREVIEWABLE_MIMES or mime.startswith("text/")
    is_inline_binary = (
        mime.startswith(_RAW_INLINE_IMAGE_PREFIX) or mime in _RAW_INLINE_BINARY_MIMES
    )
    if not (is_text or is_inline_binary):
        raise HTTPException(status_code=415, detail=f"Preview not supported for mime type: {mime}")

    # Fail closed on unknown size — never stream an unbounded body inline.
    cap = _RAW_TEXT_MAX_BYTES if is_text else _RAW_INLINE_BINARY_MAX_BYTES
    size = file_row["size_bytes"]
    if size is None or size > cap:
        raise HTTPException(status_code=413, detail="File too large or unsized for inline preview, use /download instead")

    # The view was already counted by the metadata GET (one view per page open);
    # /raw only re-checks expiry (enforce_view_cap=False above), so PDF Range
    # requests / reloads don't re-count.

    # Read the whole (capped ≤25MB) object off the event loop and return it
    # buffered. A missing/unreadable object raises StorageError HERE — a clean
    # 502 before any status line — instead of the truncated HTTP 200 a lazy
    # StreamingResponse would emit; buffering also avoids holding an S3
    # connection open across a client disconnect. (publish-hardening.)
    try:
        body = await asyncio.to_thread(file_service.get_object_bytes, file_row["s3_key"], cap)
    except Exception as e:  # StorageError / boto error → the object is unreadable/over-cap
        logger.warning("raw preview storage error for %s: %s", slug, e)
        raise HTTPException(status_code=502, detail="File content is temporarily unavailable")

    headers = {
        "Content-Disposition": "inline",
        # Don't let a served text file be sniffed into active HTML.
        "X-Content-Type-Options": "nosniff",
    }
    # Fail CLOSED on XSS: every /raw body is attacker-uploaded, so sandbox the
    # browsing context by default and only lift it for provably-inert types.
    # allow-same-origin keeps the HTML preview iframe able to read its own
    # contentDocument for fit-scaling; scripts stay blocked, and it has no effect
    # on <img>/<embed> rendering of the inert types.
    if not _is_inert_raw_mime(mime):
        headers["Content-Security-Policy"] = "sandbox allow-same-origin"

    return Response(
        content=body,
        media_type=mime or "application/octet-stream",
        headers=headers,
    )


@router.get(
    "/public/{slug}/download",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                },
                "text/csv": {"schema": {"type": "string"}},
                "text/markdown": {"schema": {"type": "string"}},
            },
            "description": "Downloadable file bytes, CSV, or markdown",
        }
    },
    summary="Force download (file or csv)",
)
async def publication_download(slug: str, request: Request):
    """For files: 302 to presigned URL with attachment disposition.
    For table_query: returns CSV.
    For documents: returns raw markdown.
    """
    try:
        # A valid view-grant means the paired page GET already counted this view,
        # so re-serve WITHOUT counting or re-checking the cap (preview→download
        # is one view; a page at its last allowed view can still download).
        # WITHOUT a grant this is a direct download = its own view: count it and
        # enforce the cap, so a direct /download can't bypass max_views (this is
        # uniform for file, document, and table). Expiry/password enforced either
        # way.
        has_grant = _verify_view_grant(slug, _extract_view_grant(request))
        publication = await _resolve_with_access(
            slug, request, increment_view=not has_grant, enforce_view_cap=not has_grant,
        )
    except PublicationError as e:
        raise _publication_error_to_http(e)

    rt = publication["resource_type"]
    # Counting/capping already happened in _resolve_with_access above, keyed on
    # the view-grant: a granted (preview→download) re-serve is free; a direct
    # download without a grant spent its own view and 410s past the cap. Uniform
    # for file, document, and table — max_views is a hard cap on every path.
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
        # Files can be large, so we STREAM (unlike /raw, which buffers a small
        # ≤25 MB preview). But a lazy stream would send 200 + headers before the
        # first S3 GET runs, so a missing object would silently truncate to an
        # empty 200. HEAD the object first (off the event loop, no body, no held
        # GET connection): a missing/unreadable object becomes a clean 502 before
        # the response is committed. The TOCTOU window (deleted between HEAD and
        # the stream's GET) is negligible and merely truncates, as before.
        try:
            await asyncio.to_thread(file_service.head_object, file_storage["s3_key"])
        except Exception as e:  # noqa: BLE001 — any storage failure → 502
            logger.warning("download storage error for %s: %s", slug, e)
            raise HTTPException(status_code=502, detail="File content is temporarily unavailable")
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
        # Strip OUR control params so they can't collide with a declared bind
        # param (a `:grant`/`:token`/`:format` param would otherwise receive our
        # control value as data). Mirrors the page GET's stripping.
        dl_params = dict(request.query_params)
        for k in ("format", "password", "token", "grant"):
            dl_params.pop(k, None)
        try:
            data = await publication_service.resolve_table_query_publication(
                publication, dl_params
            )
        except PublicationError as e:
            raise _publication_error_to_http(e)
        return await _to_csv_response(data)

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


async def _to_csv_response(data: dict) -> Response:
    columns, rows = _iter_table_cells(data)

    def _render() -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([row.get(c) for c in columns])
        return buf.getvalue()

    # A large query result is CPU-bound to serialize; offload it so building the
    # CSV can't stall the single event loop.
    content = await asyncio.to_thread(_render)
    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": file_service.content_disposition_attachment("query.csv"),
        },
    )


@router.get(
    "/public/{slug}",
    responses={
        200: {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": True,
                    }
                },
                "text/csv": {"schema": {"type": "string"}},
                "text/html": {"schema": {"type": "string"}},
            },
            "description": "Publication content or metadata",
        }
    },
    summary="Resolve and render a public publication",
)
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
    # A fresh page open (no grant) spends exactly one view and hands back a
    # short-lived view-grant (below) that the paired /raw, /download and CSV
    # carry, so those re-serves of THIS view — and a reload within the grant
    # window — don't re-count. WITHOUT a grant this GET (and a direct /raw or
    # /download) spends its own view and is capped, so max_views is a HARD cap on
    # every content path, not a soft page-open counter.
    # (publish-hardening: view-grant hard cap, supersedes the F5 peek.)
    incoming_grant = _extract_view_grant(request)
    has_grant = _verify_view_grant(slug, incoming_grant)
    try:
        publication = await _resolve_with_access(
            slug, request, increment_view=not has_grant, enforce_view_cap=not has_grant,
        )
    except PublicationNotFound as e:
        raise _publication_error_to_http(e)
    except PublicationPasswordRequired:
        raise HTTPException(
            status_code=401,
            detail={"message": "Password required", "password_required": True, "slug": slug},
        )
    except PublicationPasswordInvalid:
        raise HTTPException(
            status_code=401,
            detail={"message": "Invalid password", "password_required": True, "slug": slug},
        )
    except (PublicationExpired, PublicationViewLimitReached) as e:
        raise _publication_error_to_http(e)

    rt = publication["resource_type"]

    # Hand back a short-lived grant so the paired /raw and /download re-serves of
    # THIS view don't re-count (and a page at its last allowed view can still
    # fetch its bytes). Mint a FRESH grant only on a counted (grantless) open; on
    # a grant-carried re-serve, ECHO the same grant so its ORIGINAL 600s TTL
    # stands. Re-minting on every GET would let a viewer refresh before expiry to
    # roll the timestamp forward indefinitely — an unlimited renewable capability
    # from a single counted view, defeating the cap (Codex High). Only the JSON
    # page-open responses carry it — the CSV/HTML format branches are leaf
    # downloads, not the "open the page" call the viewer threads the grant from.
    grant = incoming_grant if has_grant else _make_view_grant(slug)

    if rt == ResourceType.DOCUMENT:
        try:
            data = await publication_service.resolve_document_publication(publication)
        except PublicationError as e:
            raise _publication_error_to_http(e)
        data["view_grant"] = grant
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
        file_data["view_grant"] = grant
        return file_data

    if rt == ResourceType.TABLE_QUERY:
        url_params = dict(request.query_params)
        # Strip our own params
        # Strip OUR control params so a table_query can't declare a bind param
        # that collides with them (e.g. a `:grant` param would otherwise receive
        # the HMAC grant as data and echo it back through applied_params). (Codex.)
        for k in ("format", "password", "token", "grant"):
            url_params.pop(k, None)
        try:
            data = await publication_service.resolve_table_query_publication(publication, url_params)
        except PublicationError as e:
            raise _publication_error_to_http(e)

        # Format negotiation
        accept = request.headers.get("accept", "").lower()
        fmt = format or ("csv" if "text/csv" in accept else "json")
        if fmt == "csv":
            return await _to_csv_response(data)
        if fmt == "html":
            return await _to_html_table_response(data)
        data["view_grant"] = grant
        return data

    raise HTTPException(status_code=400, detail=f"Unknown resource_type: {rt}")


async def _to_html_table_response(data: dict) -> Response:
    columns, rows = _iter_table_cells(data)

    def _render() -> str:
        html_rows = ["<table border='1' cellpadding='4' cellspacing='0'>", "<thead><tr>"]
        html_rows += [f"<th>{_html_escape(c)}</th>" for c in columns]
        html_rows.append("</tr></thead><tbody>")
        for r in rows:
            html_rows.append("<tr>")
            html_rows += [f"<td>{_html_escape(r.get(c))}</td>" for c in columns]
            html_rows.append("</tr>")
        html_rows.append("</tbody></table>")
        return "\n".join(html_rows)

    # Same CPU-bound serialization concern as CSV — offload for large tables.
    content = await asyncio.to_thread(_render)
    return Response(content=content, media_type="text/html")


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

    if publication.get("password_hash"):
        # F1: a password-protected publication must NOT leak its title / subject
        # / filename through an unauthenticated oembed unfurl. The viewer and
        # /embed both hide the title behind the password, so oembed can't be the
        # bypass — return a generic card and skip every DB title lookup.
        title: str | None = "Protected AKB publication"
    else:
        # Resolve a useful title. Documents are found by `publications.
        # document_id` (migration 058); the URI is parsed only for the file
        # branch and for the legacy rows that column could not be backfilled
        # for. An unfurl that titled the card from whatever now occupies the
        # published path would name a document nobody published.
        from app.services.uri_service import parse_uri
        parsed = parse_uri(publication.get("resource_uri") or "")
        title = publication.get("title")
        if not title:
            if rt == ResourceType.DOCUMENT:
                doc_path = parsed.identifier if parsed and parsed.kind == "doc" else None
                uri_vault = parsed.vault if parsed and parsed.kind == "doc" else None
                raw_doc_id = publication.get("document_id")
                doc_uuid = to_uuid(raw_doc_id) if raw_doc_id else None
                if doc_uuid is not None or doc_path is not None:
                    pool = await get_pool()
                    async with pool.acquire() as conn:
                        doc_row = await conn.fetchrow(
                            """
                            SELECT d.title FROM documents d JOIN vaults v ON v.id = d.vault_id
                             -- Same two branches, same order of authority, as
                             -- `resolve_document_publication` — the card and the
                             -- body it previews must not be able to name
                             -- different documents.
                             --
                             -- $2 (document_id) keys bound rows; the composite FK
                             -- pins the vault, so no `v.name` cross-check there.
                             -- The path branch is for backfill-NULL rows only,
                             -- and keeps the cross-check: keyed on the URI's vault
                             -- name alone this returned the title of a document in
                             -- whatever vault the URI named, which need not be this
                             -- publication's vault. F1 above protects the
                             -- publisher, who chose a password; nothing protected
                             -- the third-party vault, which published nothing at
                             -- all. No row leaves `title` None and the response
                             -- falls through to the generic card below — fail
                             -- closed.
                             WHERE d.vault_id = $1
                               AND ( d.id = $2::uuid
                                  OR ($2::uuid IS NULL AND d.path = $3 AND v.name = $4) )
                            """,
                            to_uuid(publication["vault_id"]), doc_uuid, doc_path, uri_vault,
                        )
                        if doc_row:
                            title = doc_row["title"]
            elif rt == ResourceType.FILE and parsed and parsed.kind == "file":
                file_uuid_str = parsed.identifier
                pool = await get_pool()
                async with pool.acquire() as conn:
                    f_row = await conn.fetchrow(
                        "SELECT name FROM vault_files WHERE id = $1 AND vault_id = $2",
                        to_uuid(file_uuid_str), to_uuid(publication["vault_id"]),
                    )
                    if f_row:
                        title = f_row["name"]
            elif rt == ResourceType.TABLE_QUERY:
                title = "Shared query"
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
