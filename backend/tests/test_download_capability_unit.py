"""`download_url` points at AKB, not at the object store.

A presigned URL carried the storage endpoint, the bucket and the key. That made
the object-store layout part of this API's public contract: renaming a bucket
broke every URL the API had handed out, and nothing in the tree tied the two
together. A capability URL names only this service and an opaque token.

What must NOT change is how a holder uses it — absolute, no Authorization
header, bounded lifetime — because every non-browser consumer is written
against exactly those properties.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest
from starlette.routing import Match

from app.services.file_service import (
    _capability_digest,
    _capability_url,
    _new_capability_token,
)
from app.services.raw_mime_policy import is_inert_raw_mime


# ── the URL a holder receives ───────────────────────────────────────

def test_capability_url_is_absolute_and_carries_no_locator(monkeypatch):
    """Absolute because consumers parse it with `new URL()` / `httpx.URL()`
    and a relative path raises there. No locator because that is the point."""
    from app.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com", raising=False)
    # A real token, not a placeholder: the assertions below are about what a
    # holder actually receives. They check the parts we control — the origin
    # and the path — rather than scanning the whole string, because a random
    # 43-char token contains arbitrary substrings by chance.
    token = _new_capability_token()
    url = _capability_url(token)
    assert url == f"https://akb.example.com/api/v1/files/download/{token}"

    origin, _, path = url.partition("/api/v1/")
    assert origin == "https://akb.example.com"
    assert path == f"files/download/{token}"
    assert "?" not in url, "a capability carries no query — nothing to sign"
    for leaked in ("X-Amz-", "akb-files"):
        assert leaked not in url, leaked


def test_capability_url_tolerates_a_trailing_slash_in_the_base(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://akb.example.com/", raising=False)
    assert _capability_url("t") == "https://akb.example.com/api/v1/files/download/t"


# ── the token itself ────────────────────────────────────────────────

def test_token_is_url_safe_and_unguessable():
    token = _new_capability_token()
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token), token
    # 32 random bytes, base64url without padding.
    assert len(token) == 43
    assert _new_capability_token() != _new_capability_token()


def test_only_the_digest_would_be_stored():
    """The token is the capability, so it is stored the way a password is."""
    token = _new_capability_token()
    digest = _capability_digest(token)
    assert digest != token
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert _capability_digest(token) == digest


# ── serving policy ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("mime", "inert"),
    [
        ("image/png", True),
        ("image/jpeg", True),
        ("application/pdf", True),
        ("text/plain", True),
        # Active documents, and everything unrecognised — fail closed.
        ("image/svg+xml", False),
        ("text/html", False),
        ("application/json", False),
        ("text/x-diff", False),
        ("application/octet-stream", False),
    ],
)
def test_sandbox_decision_is_shared_with_the_publication_route(mime, inert):
    """One policy module answers this for every route that serves stored bytes.
    Two answers would mean the same File is inert on one path and active on
    another."""
    assert is_inert_raw_mime(mime) is inert


# ── routing ─────────────────────────────────────────────────────────

def _match(app, method: str, path: str):
    scope = {
        "type": "http", "method": method, "path": path,
        "headers": [], "query_string": b"", "root_path": "",
    }
    for route in app.routes:
        if route.matches(scope)[0] is Match.FULL:
            return route
    return None


@pytest.fixture(scope="module")
def app():
    from app.main import app as fastapi_app
    return fastapi_app


def test_capability_route_wins_over_the_vault_parameter(app):
    """Registered after `/files/{vault}/{file_id}` this would never be reached:
    `download` would read as a vault and the token as a file id."""
    route = _match(app, "GET", "/api/v1/files/download/sometoken")
    assert route is not None
    assert route.path == "/api/v1/files/download/{token}"


def test_capability_route_is_not_in_the_public_schema(app):
    """The contract is the `download_url` field. Publishing the path would
    invite callers to mint their own."""
    route = _match(app, "GET", "/api/v1/files/download/sometoken")
    assert getattr(route, "include_in_schema", True) is False


def test_head_is_not_registered_and_the_guard_stays_anyway(app, monkeypatch):
    """Measured, not assumed: FastAPI's APIRoute keeps `methods` as declared,
    so a GET route does not also answer HEAD — unlike Starlette's plain Route.
    Nothing reaches the HEAD branch today.

    The branch stays because the cost of being wrong is asymmetric: adding
    HEAD to `methods` later would make StreamingResponse drain the whole
    object to send no body, and the largest stored File is multi-gigabyte."""
    route = _match(app, "GET", "/api/v1/files/download/sometoken")
    assert sorted(route.methods) == ["GET"]
    assert _match(app, "HEAD", "/api/v1/files/download/sometoken") is None

    # Asserting on the source text would pass for `if ...: pass`. Assert the
    # behaviour instead: a HEAD reaching the handler answers without touching
    # the object store, so a store that raises on read must not be consulted.
    import app.api.routes.files as files_route

    async def _must_not_read(*_a, **_k):
        raise AssertionError("HEAD must not read object bytes")

    monkeypatch.setattr(files_route, "iter_object_chunks", _must_not_read)
    monkeypatch.setattr(files_route, "head_object", lambda *_a, **_k: {})

    async def _resolve(_self, _token):
        return {
            "s3_key": "v/k", "name": "x.png", "mime_type": "image/png",
            "size_bytes": 7, "kind": "file", "upload_state": "confirmed",
        }

    monkeypatch.setattr(
        type(files_route.file_service), "resolve_download_capability", _resolve,
    )
    scope_req = SimpleNamespace(method="HEAD", query_params={})
    resp = asyncio.run(files_route.download_by_capability("tok", scope_req))
    assert resp.status_code == 200
    assert resp.headers["content-length"] == "7"
    assert resp.body == b""


# ── range ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        ("bytes=0-99", 1000, (0, 99)),
        ("bytes=100-", 1000, (100, 999)),
        ("bytes=-100", 1000, (900, 999)),
        ("bytes=0-", 1000, (0, 999)),
        # Clamped to the last byte rather than refused: a client asking past
        # the end still gets what exists.
        ("bytes=990-5000", 1000, (990, 999)),
        ("bytes=999-999", 1000, (999, 999)),
        (" bytes=0-0 ", 1000, (0, 0)),
    ],
)
def test_single_range_resolves_against_the_stored_size(header, size, expected):
    from app.api.routes.files import _parse_single_range

    assert _parse_single_range(header, size) == expected


@pytest.mark.parametrize(
    ("header", "size"),
    [
        (None, 1000),
        ("", 1000),
        ("bytes=", 1000),
        ("bytes=-", 1000),
        ("bytes=abc-def", 1000),
        # Multipart ranges need a multipart/byteranges body. Answering 200 is
        # the correct fallback; answering 206 with one span would be a lie.
        ("bytes=0-10,20-30", 1000),
        ("items=0-10", 1000),
        # Start past the end, and an inverted span.
        ("bytes=1000-1200", 1000),
        ("bytes=500-100", 1000),
        # A zero-length suffix asks for nothing.
        ("bytes=-0", 1000),
        # Size unknown — we cannot resolve a range against it.
        ("bytes=0-99", 0),
    ],
)
def test_ranges_we_cannot_honour_fall_back_to_the_whole_object(header, size):
    """Falling back to 200 is safe; a wrong 206 sends truncated bytes that the
    client believes are complete."""
    from app.api.routes.files import _parse_single_range

    assert _parse_single_range(header, size) is None
