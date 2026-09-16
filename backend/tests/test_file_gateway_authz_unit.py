"""The authorization subrequest the byte gateway makes before serving bytes.

This route decides, for every file transfer, both whether it happens and what
headers it happens under. It carries no user token — the capability and a
shared key are the whole of its authorization — so what it refuses, and how
indistinguishably, is the contract.
"""

from __future__ import annotations

import pytest
from starlette.routing import Match

from app.exceptions import NotFoundError
from app.services.adapters.s3_adapter import PresignedURL
from app.services.file_service import FileService


@pytest.fixture
def route():
    from app.api.routes import files
    return files


def _match(app, method: str, path: str):
    scope = {
        "type": "http", "method": method, "path": path,
        "headers": [], "query_string": b"", "root_path": "",
    }
    for r in app.routes:
        if r.matches(scope)[0] is Match.FULL:
            return r
    return None


@pytest.fixture(scope="module")
def app():
    from app.main import app as fastapi_app
    return fastapi_app


class _Request:
    def __init__(self, key: str | None = None):
        self.headers = {} if key is None else {"x-akb-gateway-key": key}


# --- reachability -------------------------------------------------------

def test_the_internal_route_is_not_under_the_public_api_prefix(app):
    """`/api` is routed to this service by every ingress in the deployment;
    `/internal` is routed by none of them. That is the first lock, and it is
    a property of the path, so it is worth asserting on the path."""
    r = _match(app, "GET", "/internal/files/download/sometoken")
    assert r is not None
    assert r.path == "/internal/files/download/{token}"
    assert not r.path.startswith("/api")
    assert getattr(r, "include_in_schema", True) is False


def test_the_public_download_route_still_answers_its_own_path(app):
    """Adding the internal route must not shadow the one clients use."""
    r = _match(app, "GET", "/api/v1/files/download/sometoken")
    assert r is not None
    assert r.path == "/api/v1/files/download/{token}"


# --- the shared key -----------------------------------------------------

async def test_an_unset_key_makes_the_route_answer_404(route, monkeypatch):
    """A deployment with no gateway has nothing here to find.

    This is what keeps the route inert in the OSS packaging, where the
    prefix might one day be routed by someone's own reverse proxy."""
    from app.config import settings

    monkeypatch.setattr(settings, "file_gateway_key", "", raising=False)
    monkeypatch.setattr(
        FileService, "resolve_download_capability",
        _never("an unkeyed request must not resolve a capability"),
    )

    response = await route.authorize_gateway_download("A" * 43, _Request("anything"))
    assert response.status_code == 404


async def test_a_wrong_key_answers_404_and_never_looks_up_the_token(route, monkeypatch):
    """404 rather than 401 on purpose: the gateway turns 401 into the
    client-facing 404 of a refused capability, so a misconfigured key must
    not be able to impersonate one. It becomes a 503 instead — every
    download failing at once, which is how a wrong shared secret should
    announce itself."""
    from app.config import settings

    monkeypatch.setattr(settings, "file_gateway_key", "correct-horse", raising=False)
    monkeypatch.setattr(
        FileService, "resolve_download_capability",
        _never("a wrong key must not reach the capability table"),
    )

    response = await route.authorize_gateway_download("A" * 43, _Request("wrong"))
    assert response.status_code == 404


async def test_the_key_is_compared_without_leaking_its_length(route, monkeypatch):
    """`hmac.compare_digest`, not `==`. Asserted by behaviour on a prefix:
    a plain comparison would also reject it, so this pins the call itself."""
    import inspect

    source = inspect.getsource(route.authorize_gateway_download)
    assert "hmac.compare_digest" in source
    assert "==" not in source.split("compare_digest")[0].split("presented")[-1]


# --- what it refuses ----------------------------------------------------

async def test_every_capability_refusal_is_the_same_401(route, monkeypatch):
    """Expired, never-issued, malformed and since-deleted must stay one
    answer. The resolver already collapses them into `NotFoundError`; this
    asserts the route does not pull them back apart."""
    from app.config import settings

    monkeypatch.setattr(settings, "file_gateway_key", "k", raising=False)

    async def _refuse(_self, _token):
        raise NotFoundError("File", "capability")

    monkeypatch.setattr(FileService, "resolve_download_capability", _refuse)

    response = await route.authorize_gateway_download("A" * 43, _Request("k"))
    assert response.status_code == 401
    assert response.body == b""


# --- what it authorizes -------------------------------------------------

async def test_a_grant_returns_a_signed_url_and_no_body(route, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "file_gateway_key", "k", raising=False)
    monkeypatch.setattr(settings, "file_gateway_presign_ttl", 60, raising=False)
    captured: dict = {}

    async def _resolve(_self, _token):
        return {
            "s3_key": "team/coll/abc_report.pdf",
            "name": "report.pdf",
            "mime_type": "application/pdf",
        }

    def _sign(key, *, ttl, content_type, content_disposition, cache_control):
        captured.update(
            key=key, ttl=ttl, content_type=content_type,
            content_disposition=content_disposition, cache_control=cache_control,
        )
        return PresignedURL(
            "http://10.0.0.1:8080/bucket/team/coll/abc_report.pdf?X-Amz-Signature=x",
            ttl,
        )

    monkeypatch.setattr(FileService, "resolve_download_capability", _resolve)
    monkeypatch.setattr(route, "presign_internal_get", _sign)

    response = await route.authorize_gateway_download("A" * 43, _Request("k"))

    assert response.status_code == 204
    assert response.body == b""
    assert response.headers["X-AKB-S3"].startswith("http://10.0.0.1:8080/")
    assert captured["key"] == "team/coll/abc_report.pdf"
    assert captured["ttl"] == 60
    # The granted lifetime, not the requested one — they differ when a
    # temporary session expires first.
    assert response.headers["X-AKB-Expires-In"] == "60"


async def test_the_policy_travels_in_the_signature(route, monkeypatch):
    """The gateway adds no `add_header` of its own for these, because one
    `add_header` inside an nginx location drops every header inherited from
    the server block — where the unconditional security headers live. So the
    object store has to emit them, which means they have to be signed."""
    from app.config import settings

    monkeypatch.setattr(settings, "file_gateway_key", "k", raising=False)
    captured: dict = {}

    async def _resolve(_self, _token):
        return {
            "s3_key": "team/evil.svg",
            "name": "evil.svg",
            "mime_type": "image/svg+xml",
        }

    def _sign(key, **kwargs):
        captured.update(kwargs)
        return PresignedURL("http://10.0.0.1:8080/b/k?X-Amz-Signature=x", 60)

    monkeypatch.setattr(FileService, "resolve_download_capability", _resolve)
    monkeypatch.setattr(route, "presign_internal_get", _sign)

    await route.authorize_gateway_download("A" * 43, _Request("k"))

    assert captured["content_type"] == "image/svg+xml"
    assert "evil.svg" in captured["content_disposition"]
    assert captured["cache_control"] == "private, no-store"


async def test_both_byte_paths_decide_policy_from_one_function(route):
    """An SVG is not inert, so it must be sandboxed — and it must be
    sandboxed the same way whether this process streams it or the gateway
    carries it. Two copies of this decision would drift."""
    headers = route._raw_download_policy(
        {"mime_type": "image/svg+xml", "name": "evil.svg"},
    )
    assert headers["Content-Security-Policy"] == "sandbox allow-same-origin"
    assert headers["X-Content-Type-Options"] == "nosniff"

    inert = route._raw_download_policy(
        {"mime_type": "application/pdf", "name": "report.pdf"},
    )
    assert "Content-Security-Policy" not in inert


def _never(message: str):
    async def _fail(*_a, **_k):
        pytest.fail(message)
    return _fail


async def test_the_granted_lifetime_is_reported_not_the_requested_one(route, monkeypatch):
    """A temporary session can run out before the lifetime that was asked
    for, and the signer clamps to whichever is shorter. Reporting the clamped
    value is what makes that bound observable — the first version of the
    internal signer dropped it, and with it the clamp itself."""
    from app.config import settings

    monkeypatch.setattr(settings, "file_gateway_key", "k", raising=False)
    monkeypatch.setattr(settings, "file_gateway_presign_ttl", 600, raising=False)

    async def _resolve(_self, _token):
        return {"s3_key": "v/k", "name": "x.pdf", "mime_type": "application/pdf"}

    def _sign(_key, **_kwargs):
        # The signer clamped 600 down to 45.
        return PresignedURL("http://10.0.0.1:8080/b/k?X-Amz-Signature=x", 45)

    monkeypatch.setattr(FileService, "resolve_download_capability", _resolve)
    monkeypatch.setattr(route, "presign_internal_get", _sign)

    response = await route.authorize_gateway_download("A" * 43, _Request("k"))

    assert response.headers["X-AKB-Expires-In"] == "45"
