"""Cross-origin callers can read the headers that say when to retry.

A browser script on another origin sees only the CORS-safelisted response
headers unless the server exposes the others, and neither `Retry-After` nor
`Date` is safelisted. The SDK's change channel waits at least a refusal's
`Retry-After`, and measures an HTTP-date value from the response's `Date`;
hidden, both read as absent and the channel falls back to its own backoff.
"""

from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from app.config import settings

# Importing app.main builds the shared GitService, whose __init__ creates
# `git_storage_path`. Keep that out of the deployment default.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-cors-expose-test-")

from app.api.deps import get_current_user  # noqa: E402
from app.main import app  # noqa: E402
from app.services import event_tail_service  # noqa: E402

_ORIGIN = {"Origin": "https://app.example"}


class _FakeUser:
    user_id = "u-cors"
    username = "cors-tester"


@pytest.fixture
def client(monkeypatch):
    async def _full_process(**_kwargs):
        raise event_tail_service.EventTailCapacityError()

    monkeypatch.setattr(event_tail_service, "open_tail", _full_process)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _exposed(response) -> set[str]:
    value = response.headers.get("access-control-expose-headers", "")
    return {name.strip().lower() for name in value.split(",") if name.strip()}


def test_a_cross_origin_tail_refusal_exposes_retry_after_and_date(client):
    response = client.get("/api/v1/events/reef", headers=_ORIGIN)

    assert response.status_code == 503
    assert response.json()["code"] == "event_tail_capacity"
    assert response.headers["retry-after"] == "5"
    assert response.headers["access-control-allow-origin"] == "*"
    assert _exposed(response) == {"retry-after", "date"}


def test_every_cross_origin_response_exposes_them(client):
    response = client.get("/livez", headers=_ORIGIN)

    assert response.status_code == 200
    assert _exposed(response) == {"retry-after", "date"}


def test_the_preflight_still_allows_any_method_and_header(client):
    response = client.options(
        "/api/v1/events/reef",
        headers={
            **_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization, last-event-id",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "GET" in response.headers["access-control-allow-methods"]
    assert response.headers["access-control-allow-headers"].lower() == "authorization, last-event-id"
