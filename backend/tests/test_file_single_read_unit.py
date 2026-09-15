"""Contract for reading one File by id.

Resolving a File used to require listing the vault and searching the page that
came back: ``GET /api/v1/files/{vault}`` returns at most ``limit`` rows (50 by
default, 200 at most), so in a vault holding more than that a File simply could
not be opened — the caller saw it as missing however reachable it actually was.
The failure was silent and scaled with the vault: nothing errored, the File
just fell out of the window as newer ones arrived.

These tests pin the single-File read and the two properties that make it safe
to add: the envelope is the same one the listing emits (so a caller resolving
by id sees exactly what it would have seen from a page that happened to contain
the File), and the literal ``body-placements`` path still matches its own route
rather than being read as a file id.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from starlette.routing import Match

from app.services.file_service import _file_envelope


def _row(**over) -> dict:
    row = {
        "id": uuid.UUID("41326efd-93a3-4af4-8dfb-164c4618361b"),
        "collection": "wiki/arch",
        "name": "f05-read.png",
        "mime_type": "image/png",
        "size_bytes": 159400,
        "content_hash": "0" * 64,
        "hash_algorithm": "sha256",
        "etag": "etag-1",
        "storage_version": None,
        "description": "a figure",
        "created_by": "someone",
        "created_at": datetime(2026, 9, 10, 3, 28, tzinfo=timezone.utc),
    }
    row.update(over)
    return row


# ── envelope ────────────────────────────────────────────────────────

def test_envelope_carries_the_fields_a_reader_needs():
    """`uri` and `collection` in particular: without them a caller has to
    recover placement from the storage locator, which is not a contract."""
    env = _file_envelope(_row(), "dbt")
    assert env["kind"] == "file"
    assert env["uri"] == "akb://dbt/coll/wiki/arch/file/41326efd-93a3-4af4-8dfb-164c4618361b"
    assert env["collection"] == "wiki/arch"
    assert env["name"] == "f05-read.png"
    assert env["mime_type"] == "image/png"
    assert env["size_bytes"] == 159400
    assert env["created_at"] == "2026-09-10T03:28:00+00:00"


def test_envelope_handles_vault_root_and_missing_timestamp():
    env = _file_envelope(_row(collection=None, created_at=None), "dbt")
    assert env["collection"] is None
    assert env["created_at"] is None
    assert env["uri"].endswith("/file/41326efd-93a3-4af4-8dfb-164c4618361b")


def test_envelope_covers_what_a_file_view_renders():
    """The consumer of this envelope needs every one of these to draw a File
    without a second lookup. `uri` gates publishing; `collection`, `created_at`
    and `description` are the placement and provenance shown beside the name."""
    env = _file_envelope(_row(), "dbt")
    required = {
        "uri", "name", "collection", "description",
        "mime_type", "size_bytes", "created_by", "created_at",
    }
    assert required <= env.keys(), sorted(required - env.keys())


# ── routing ─────────────────────────────────────────────────────────

def _match(app, method: str, path: str):
    scope = {
        "type": "http", "method": method, "path": path,
        "headers": [], "query_string": b"", "root_path": "",
    }
    for route in app.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route
    return None


@pytest.fixture(scope="module")
def app():
    from app.main import app as fastapi_app
    return fastapi_app


def test_single_file_get_is_registered(app):
    route = _match(app, "GET", "/api/v1/files/dbt/41326efd-93a3-4af4-8dfb-164c4618361b")
    assert route is not None, "GET on a single file must resolve"
    assert route.path == "/api/v1/files/{vault}/{file_id}"


def test_literal_paths_still_win_over_the_file_id_parameter(app):
    """`{file_id}` would swallow these if it were registered first. Registration
    order is load-bearing here, and nothing else in the file states it."""
    for literal, expected in (
        ("/api/v1/files/dbt/body-placements", "/api/v1/files/{vault}/body-placements"),
        ("/api/v1/files/dbt/upload", "/api/v1/files/{vault}/upload"),
    ):
        method = "GET" if "body-placements" in literal else "POST"
        route = _match(app, method, literal)
        assert route is not None and route.path == expected, literal


def test_download_and_delete_still_resolve(app):
    fid = "41326efd-93a3-4af4-8dfb-164c4618361b"
    dl = _match(app, "GET", f"/api/v1/files/dbt/{fid}/download")
    assert dl is not None and dl.path == "/api/v1/files/{vault}/{file_id}/download"
    rm = _match(app, "DELETE", f"/api/v1/files/dbt/{fid}")
    assert rm is not None and rm.path == "/api/v1/files/{vault}/{file_id}"
