"""Private profile photo normalization, carrier and HTTP contracts."""

from __future__ import annotations

import asyncio
import io
import json
import threading
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import httpx
from PIL import Image

from app.api.deps import get_current_user
from app.api.routes import user_avatar as routes
from app.exceptions import AKBError
from app.models.user_avatar import AvatarState, AvatarWriteResponse
from app.services import image_decode, user_avatar_service as service
from app.services.auth_service import AuthenticatedUser


def principal(**changes):
    values = dict(user_id=str(uuid.uuid4()), username="avatar-test", email="avatar@example.test",
                  display_name="Avatar", is_admin=False, auth_method="jwt")
    return AuthenticatedUser(**(values | changes))


def photo(format="PNG", mode="RGB", size=(64, 80), **kwargs):
    image = Image.new(mode, size, (255, 40, 20, 100) if mode == "RGBA" else 100)
    data = io.BytesIO()
    image.save(data, format=format, **kwargs)
    return data.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [None, "/api/v1/vaults", "/api/v1/auth/me/avatar"])
@pytest.mark.parametrize("retry_after", [None, 5])
async def test_error_handler_scopes_photo_privacy_headers_without_changing_generic_errors(
    monkeypatch, tmp_path, path, retry_after,
):
    from app.config import settings

    monkeypatch.setattr(settings, "git_storage_path", str(tmp_path / "vaults"))
    from app.main import akb_error_handler

    request = None if path is None else Request({
        "type": "http", "method": "PUT", "path": path, "headers": [],
    })
    error = AKBError("Please retry.", 429, code="write_busy")
    if retry_after is not None:
        error.retry_after_secs = retry_after
    response = await akb_error_handler(request, error)
    assert response.status_code == 429
    assert json.loads(response.body)["detail"] == {"message": "Please retry.", "code": "write_busy"}
    assert response.headers.get("Retry-After") == (None if retry_after is None else str(retry_after))
    if path == "/api/v1/auth/me/avatar":
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    else:
        assert "Cache-Control" not in response.headers
        assert "X-Content-Type-Options" not in response.headers


@pytest.mark.parametrize("format,mime,mode", [
    ("PNG", "image/png", "RGB"), ("PNG", "image/png", "RGBA"),
    ("JPEG", "image/jpeg", "RGB"), ("JPEG", "image/jpeg", "CMYK"),
    ("WEBP", "image/webp", "RGB"),
])
def test_normalizes_supported_still_images(format, mime, mode):
    encoded = service.normalize_photo(photo(format, mode), mime)
    assert 0 < len(encoded) <= service.MAX_OUTPUT_BYTES
    with Image.open(io.BytesIO(encoded)) as result:
        assert result.format == "WEBP"
        assert result.size == (512, 512)
        assert not result.getexif()
        assert "exif" not in result.info
        assert "icc_profile" not in result.info


def test_exif_rotation_and_metadata_are_applied_then_removed():
    image = Image.new("RGB", (120, 60), "red")
    image.paste("blue", (60, 0, 120, 60))
    exif = Image.Exif()
    exif[274] = 6  # Clockwise: left/right become top/bottom.
    exif[270] = "private original metadata"
    data = io.BytesIO()
    image.save(data, format="JPEG", exif=exif)
    result = Image.open(io.BytesIO(service.normalize_photo(data.getvalue(), "image/jpeg")))
    top, bottom = result.getpixel((256, 50)), result.getpixel((256, 460))
    assert top[0] > top[2] and bottom[2] > bottom[0]
    assert not result.getexif()


# Keep binary fixtures, especially the 10 MiB body, out of verbose CI test IDs.
@pytest.mark.parametrize("body,mime,code,status", [
    (b"", "image/png", "avatar_unsupported_image", 415),
    (b"<svg><script/></svg>", "image/png", "avatar_unsupported_image", 415),
    (b"<html/>", "image/svg+xml", "avatar_unsupported_image", 415),
    (photo("GIF"), "image/gif", "avatar_unsupported_image", 415),
    (photo(), "image/jpeg", "avatar_unsupported_image", 415),
    (photo()[:32], "image/png", "avatar_unsupported_image", 415),
    (b"x" * (service.MAX_UPLOAD_BYTES + 1), "image/png", "avatar_too_large", 413),
    (photo(size=(8193, 1)), "image/png", "avatar_dimensions_exceeded", 413),
    (photo(size=(4000, 3001)), "image/png", "avatar_dimensions_exceeded", 413),
], ids=[
    "empty-body", "svg-payload", "unsupported-mime", "gif-format", "mime-mismatch",
    "truncated-png", "oversize-body", "oversize-width", "oversize-pixels",
])
def test_rejects_invalid_or_excessive_inputs(body, mime, code, status):
    with pytest.raises(AKBError) as caught:
        service.normalize_photo(body, mime)
    assert caught.value.code == code
    assert caught.value.status_code == status


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("WEBP", "image/webp")])
def test_rejects_animation(format, mime):
    data = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(data, format=format, save_all=True,
        append_images=[Image.new("RGB", (16, 16), "blue")], duration=100)
    with pytest.raises(AKBError, match="Animated"):
        service.normalize_photo(data.getvalue(), mime)


def test_trailing_input_is_not_retained():
    encoded = service.normalize_photo(photo() + b"secret trailing bytes", "image/png")
    assert b"secret trailing bytes" not in encoded


@pytest.mark.parametrize("changes", [
    {"auth_method": "pat"}, {"auth_method": "oauth"}, {"account_kind": "service"},
    {"token_id": str(uuid.uuid4())}, {"key_class": "service"},
    {"token_scopes": frozenset()}, {"oauth_scopes": []},
])
def test_automation_is_never_a_photo_carrier(changes):
    with pytest.raises(AKBError) as caught:
        service.require_carrier(principal(**changes))
    assert caught.value.status_code == 403
    assert not service.capability(principal(**changes)).can_upload


@pytest.mark.parametrize("method", ["jwt", "browser_session"])
def test_both_human_browser_carriers_are_supported(monkeypatch, method):
    monkeypatch.setattr(service.settings, "s3_endpoint_url", "http://storage.test")
    user = principal(auth_method=method)
    service.require_carrier(user)
    assert service.capability(user).can_upload


def test_real_browser_principal_preserves_verified_oidc_scope_without_becoming_oauth_bearer(monkeypatch):
    from app.services import sso_browser_session_service as sessions
    from app.services.sso_session_epoch import SsoSessionAuthority
    session_id, epoch, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(sessions, "current_sso_session_authority", lambda: SsoSessionAuthority(7, epoch))
    actor = sessions._authenticated_user({"id": session_id, "user_id": user_id,
        "session_epoch": epoch, "username": "sso-avatar", "email": "sso@example.test",
        "display_name": None, "is_admin": False}, "openid profile email vault:read")
    service.require_carrier(actor)
    assert actor.oauth_scopes == ["openid", "profile", "email", "vault:read"]
    assert actor.browser_session_id == session_id
    assert actor.browser_session_epoch == epoch


def test_missing_storage_is_not_a_missing_feature(monkeypatch):
    monkeypatch.setattr(service.settings, "s3_endpoint_url", "")
    monkeypatch.setattr(service.settings, "s3_auth_mode", "static")
    capability = service.capability(principal())
    assert capability.supported
    assert not capability.can_upload and not capability.can_remove
    assert capability.reason == "avatar_storage_unavailable"


def test_empty_state_retains_null_revision_and_url_but_no_dimensions():
    assert AvatarWriteResponse(avatar=AvatarState()).model_dump() == {
        "avatar": {"revision": None, "url": None}}
    revision = uuid.uuid4()
    assert service.avatar_state({"revision": revision, "object_key": None}).model_dump(mode="json") == {
        "revision": str(revision), "url": None}


@pytest.mark.parametrize("data,valid", [(b"123", True), (b"12345", False), (b"", False)])
def test_small_object_reads_are_bounded_and_always_close_stream(monkeypatch, data, valid):
    from app.services.adapters import s3_adapter
    stream = Mock(wraps=io.BytesIO(data))
    client = Mock()
    client.get_object.return_value = {"Body": stream}
    monkeypatch.setattr(s3_adapter, "client", lambda: client)
    if valid:
        assert s3_adapter.get_bounded_bytes("private-key", 3) == data
    else:
        with pytest.raises(s3_adapter.StorageError):
            s3_adapter.get_bounded_bytes("private-key", 3)
    stream.read.assert_called_once_with(4)
    stream.close.assert_called_once()


def test_revision_contract_is_strict():
    assert service.parse_expected_revision("none") is None
    revision = uuid.uuid4()
    assert service.parse_expected_revision(str(revision)) == revision
    with pytest.raises(AKBError) as caught:
        service.parse_expected_revision("anything-else")
    assert caught.value.status_code == 422
    with pytest.raises(AKBError) as caught:
        service.require_revision({"revision": revision}, None)
    assert caught.value.code == "avatar_changed"


@pytest.mark.asyncio
async def test_decoder_slot_survives_cancellation_until_thread_settles(monkeypatch):
    monkeypatch.setattr(image_decode, "_slots", asyncio.Semaphore(1))
    started, finish, second_started = threading.Event(), threading.Event(), threading.Event()

    def decode():
        started.set()
        finish.wait(5)
        return "done"

    first = asyncio.create_task(image_decode.run_bounded_decode(decode))
    assert await asyncio.to_thread(started.wait, 2)
    first.cancel()
    second = asyncio.create_task(image_decode.run_bounded_decode(lambda: second_started.set()))
    await asyncio.sleep(0.02)
    assert not second_started.is_set()
    first.cancel()  # A second disconnect must not release the real budget.
    await asyncio.sleep(0.02)
    assert not second_started.is_set()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert second_started.is_set()


def client(monkeypatch, user=None):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user or principal()

    @app.exception_handler(AKBError)
    async def error(_request: Request, exc: AKBError):
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status_code)

    monkeypatch.setattr(service, "preflight_write", AsyncMock())
    return TestClient(app)


def test_http_upload_body_response_and_write_revision(monkeypatch):
    test_client = client(monkeypatch)
    revision = uuid.uuid4()
    save = AsyncMock(return_value=AvatarState(revision=revision, url="/api/v1/auth/me/avatar?v=x",
                                             width=512, height=512))
    monkeypatch.setattr(service, "save_photo", save)
    body = photo()
    response = test_client.put("/api/v1/auth/me/avatar?expected_revision=none", content=body,
                               headers={"content-type": "image/png"})
    assert response.status_code == 200
    assert response.json()["avatar"]["revision"] == str(revision)
    assert response.headers["Cache-Control"] == "private, no-store"
    assert save.await_args.args[1:] == (None, body, "image/png")
    assert test_client.put("/api/v1/auth/me/avatar", content=body).status_code == 422


def test_http_real_length_is_bounded_even_without_content_length(monkeypatch):
    test_client = client(monkeypatch)
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 3)
    save = AsyncMock()
    monkeypatch.setattr(service, "save_photo", save)
    response = test_client.put("/api/v1/auth/me/avatar?expected_revision=none",
                               content=iter([b"ab", b"cd"]), headers={"content-type": "image/png"})
    assert response.status_code == 413
    assert response.json()["code"] == "avatar_too_large"
    save.assert_not_called()


def test_http_get_requires_exact_revision_and_has_no_store(monkeypatch):
    test_client = client(monkeypatch)
    read = AsyncMock(return_value=b"webp")
    monkeypatch.setattr(service, "read_photo", read)
    revision = uuid.uuid4()
    response = test_client.get(f"/api/v1/auth/me/avatar?v={revision}")
    assert response.content == b"webp"
    assert response.headers["Content-Type"] == "image/webp"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert test_client.get("/api/v1/auth/me/avatar?v=none").status_code == 422


def test_http_delete_keeps_null_url_and_omits_dimensions(monkeypatch):
    test_client = client(monkeypatch)
    revision = uuid.uuid4()
    monkeypatch.setattr(service, "remove_photo", AsyncMock(return_value=AvatarState(revision=revision)))
    response = test_client.delete(f"/api/v1/auth/me/avatar?expected_revision={revision}")
    assert response.json() == {"avatar": {"revision": str(revision), "url": None}}


def test_http_delegation_header_is_rejected_even_with_human_jwt(monkeypatch):
    test_client = client(monkeypatch)
    response = test_client.get(f"/api/v1/auth/me/avatar?v={uuid.uuid4()}",
                               headers={"X-Akb-Delegated-Authorization": "Bearer anything"})
    assert response.status_code == 403


def test_openapi_has_binary_body_typed_response_and_explicit_upload_errors(monkeypatch):
    test_client = client(monkeypatch)
    schema = test_client.get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/auth/me/avatar"]["put"]
    assert set(operation["requestBody"]["content"]) == set(service.ALLOWED_CONTENT_TYPES)
    assert {"408", "413", "415", "429"} <= operation["responses"].keys()
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("AvatarWriteResponse")


@pytest.mark.asyncio
async def test_body_timeout_and_admission_wait_are_bounded(monkeypatch):
    from starlette.requests import Request
    from fastapi import Response

    monkeypatch.setattr(service, "preflight_write", AsyncMock())
    save = AsyncMock()
    monkeypatch.setattr(service, "save_photo", save)
    monkeypatch.setattr(routes, "_BODY_TIMEOUT_SECONDS", 0.01)

    async def trickle():
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"x", "more_body": True}

    request = Request({"type": "http", "method": "PUT", "path": "/auth/me/avatar", "headers": []},
                      receive=trickle)
    with pytest.raises(AKBError) as caught:
        await routes.upload_avatar(request, Response(), "none", principal())
    assert caught.value.code == "avatar_upload_timeout"
    monkeypatch.setattr(routes, "_body_slots", asyncio.Semaphore(0))
    monkeypatch.setattr(routes, "_ADMISSION_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(AKBError) as caught:
        await routes.upload_avatar(request, Response(), "none", principal())
    assert caught.value.code == "avatar_busy"
    assert caught.value.retry_after_secs == 5
    save.assert_not_called()


@pytest.mark.asyncio
async def test_sso_cookie_uses_existing_csrf_boundary_and_never_bearer_fallback(monkeypatch):
    from app.api import deps
    from app.exceptions import AuthenticationError

    monkeypatch.setattr(service.settings, "auth_mode", "sso")
    monkeypatch.setattr(service.settings, "public_base_url", "https://akb.example.test")
    monkeypatch.setattr(deps, "sso_browser_session_ready", lambda: True)
    actor = principal(auth_method="browser_session")
    calls = []

    async def resolve(_token, *, require_csrf, csrf_cookie, csrf_header):
        calls.append(require_csrf)
        if require_csrf and (not csrf_header or csrf_cookie != csrf_header):
            raise AuthenticationError()
        return actor

    monkeypatch.setattr(deps, "resolve_sso_browser_session", resolve)
    monkeypatch.setattr(deps, "resolve_rest_user_authorization", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "preflight_write", AsyncMock())
    monkeypatch.setattr(service, "remove_photo", AsyncMock(return_value=AvatarState()))
    app = FastAPI()
    app.include_router(routes.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="https://akb.example.test", cookies={
                "__Host-akb_sso_session": "fixture-browser-session-handle",
                "__Host-akb_sso_csrf": "fixture-csrf"}) as client:
        assert (await client.delete("/auth/me/avatar?expected_revision=none")).status_code == 401
        assert (await client.delete("/auth/me/avatar?expected_revision=none",
            headers={"X-AKB-CSRF": "fixture-csrf"})).status_code == 200
        before = len(calls)
        assert (await client.delete("/auth/me/avatar?expected_revision=none",
            headers={"Authorization": "Bearer rejected", "X-AKB-CSRF": "fixture-csrf"})).status_code == 401
        assert len(calls) == before
    assert calls == [True, True]
