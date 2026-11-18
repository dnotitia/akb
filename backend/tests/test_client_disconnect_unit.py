"""A client that leaves mid-body is reported once, as a 499, not as a 500.

Each case drives the whole application ASGI callable, middleware included,
with part of a request body followed by ``http.disconnect``. Authorization and
database access are stubbed; nothing here needs PostgreSQL or object storage.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from unittest.mock import AsyncMock

import pytest

from app.api.deps import ToolControlUser
from app.services.auth_service import AuthenticatedUser

_HANDLER_MESSAGE = "Client disconnected before its request body was read: %s %s"
_USER = AuthenticatedUser(
    user_id="00000000-0000-4000-8000-000000000824",
    username="fixture",
    email="fixture@example.invalid",
    display_name="fixture",
    is_admin=False,
    auth_method="pat",
)


@pytest.fixture
def main(monkeypatch, tmp_path):
    # Importing routes constructs the legacy revision service. Keep focused
    # test collection independent of deployment paths and test import order.
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app import main as application

    return application


def _stub_mcp(main, monkeypatch) -> None:
    from mcp_server import http_app

    monkeypatch.setattr(http_app, "resolve_mcp_authorization", AsyncMock(return_value=_USER))


def _stub_asset_upload(main, monkeypatch) -> None:
    from app.api.routes import assets

    monkeypatch.setitem(main.app.dependency_overrides, ToolControlUser("asset.upload"), lambda: _USER)
    monkeypatch.setattr(
        assets,
        "resolve_file_write_context",
        AsyncMock(return_value=({"vault_id": "fixture"}, _USER.user_id, None)),
    )
    monkeypatch.setattr(
        assets.asset_service,
        "create_image_asset",
        AsyncMock(side_effect=AssertionError("an incomplete body must not be stored")),
    )


def _stub_file_upload(main, monkeypatch) -> None:
    from app.api.routes import files
    from app.services import file_service

    monkeypatch.setattr(
        files.file_service,
        "resolve_write_capability",
        AsyncMock(return_value={
            "already_confirmed": False,
            "object_key": "fixture/object",
            "mime_type": "application/octet-stream",
        }),
    )
    def no_object_storage(*args, **kwargs):
        raise AssertionError("object storage must not be called")

    # A unit test never reaches an object store; a call would surface as an
    # ERROR record and fail the assertions below.
    for name in ("put_bytes", "multipart_create", "multipart_part", "multipart_complete", "multipart_abort"):
        monkeypatch.setattr(file_service.s3_adapter, name, no_object_storage)


# (stub, method, request path, query, content type, partial chunk, accepted
# logged paths). A matched route is logged by its template: the File upload
# path carries its capability, which is a credential, as a path segment.
# Whether the MCP mount counts as the matched route depends on the Starlette
# release: 1.10 records a matched Mount in scope["route"] (logged as "/mcp"),
# 1.6 records only FastAPI routes (the literal "/mcp/" is logged). Both are
# the mount's own path and carry nothing from the request.
_CASES = {
    "mcp": (
        _stub_mcp, "POST", "/mcp/", b"", b"application/json", b'{"jsonrpc":"2.0","id":1,',
        frozenset({"/mcp", "/mcp/"}),
    ),
    "asset-upload": (
        _stub_asset_upload, "POST", "/api/v1/assets/fixture", b"filename=fixture.png", b"image/png",
        b"\x89PNG\r\n\x1a\n", frozenset({"/api/v1/assets/{vault}"}),
    ),
    "file-upload": (
        _stub_file_upload, "PUT", "/api/v1/files/upload/fixture-capability", b"",
        b"application/octet-stream", b"partial file bytes", frozenset({"/api/v1/files/upload/{token}"}),
    ),
}


async def _drive(app, method: str, path: str, query: bytes, content_type: bytes, chunk: bytes):
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": "", "query_string": query,
        "headers": [
            (b"authorization", b"Bearer fixture-credential"),
            (b"content-type", content_type),
            (b"accept", b"application/json"),
            (b"content-length", b"1000"),
        ],
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80),
    }
    messages = [
        {"type": "http.request", "body": chunk, "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    escaped: BaseException | None = None
    try:
        await asyncio.wait_for(app(scope, receive, send), 5)
    except BaseException as exc:  # noqa: BLE001 - what escapes the app is the observation
        escaped = exc
    return sent, escaped


@pytest.mark.parametrize("case", sorted(_CASES))
async def test_client_disconnect_mid_body_is_one_info_line_and_a_499(main, monkeypatch, caplog, case):
    stub, method, path, query, content_type, chunk, logged_paths = _CASES[case]
    stub(main, monkeypatch)
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.INFO, logger="akb")

    sent, escaped = await _drive(main.app, method, path, query, content_type, chunk)

    assert escaped is None, "".join(traceback.format_exception(escaped))
    starts = [message["status"] for message in sent if message["type"] == "http.response.start"]
    assert starts == [499]
    assert b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body") == b""

    assert not [r for r in caplog.records if "Unhandled request error" in r.getMessage()]
    errors = [f"{r.name}: {r.getMessage()}" for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []
    handler_records = [r for r in caplog.records if r.name == "akb" and r.msg == _HANDLER_MESSAGE]
    assert len(handler_records) == 1
    record = handler_records[0]
    assert record.levelno == logging.INFO
    logged_method, logged_path = record.args
    assert logged_method == method
    assert logged_path in logged_paths
    # Method and path only: never the query string, body or credential.
    text = record.getMessage()
    assert text == _HANDLER_MESSAGE % (method, logged_path)
    assert "fixture-credential" not in text
    assert "fixture-capability" not in text
    assert chunk.decode("latin-1") not in text
    if query:
        assert query.decode() not in text
