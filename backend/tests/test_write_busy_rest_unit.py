"""REST answers a refused write with the `write_busy` error MCP returns.

The Vault write admission and the Bare Git write lane refuse a write that
waited past its deadline with `WriteBusyError`. REST answers it with HTTP 429
and `Retry-After`, and a body whose `code` is `write_busy`, whose `details`
carry `retry_after_secs`, and `partially_applied` when part of the operation
had committed, and whose hint says whether the same request can be sent
again. The body's code used to be the generic `internal`.
"""

from __future__ import annotations

import tempfile
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings

# Importing the API builds a GitService at module load, which creates
# `git_storage_path` — redirect it first.
settings.git_storage_path = tempfile.mkdtemp(prefix="akb-write-busy-rest-")

from app import main  # noqa: E402
from app.api.deps import get_current_user  # noqa: E402
from app.api.routes import collections  # noqa: E402
from app.exceptions import WRITE_BUSY_HINT, WRITE_BUSY_PARTIAL_HINT, WriteBusyError  # noqa: E402
from app.services.auth_service import AuthenticatedUser  # noqa: E402


@pytest.fixture
def refused_delete(monkeypatch):
    """Send a recursive collection delete that the service refuses."""
    app = FastAPI(exception_handlers=dict(main.app.exception_handlers))
    app.include_router(collections.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id=str(uuid.uuid4()), username="tester", email="tester@example.invalid",
        display_name=None, is_admin=False, auth_method="pat",
    )
    monkeypatch.setattr(collections, "check_vault_access", AsyncMock(
        return_value={"vault_id": uuid.uuid4(), "role": "writer"},
    ))
    client = TestClient(app, raise_server_exceptions=False)

    def send(error: WriteBusyError):
        monkeypatch.setattr(collections.collection_service, "delete", AsyncMock(side_effect=error))
        return client.delete("/api/v1/collections/busy-vault/notes", params={"recursive": "true"})

    return send


def test_a_refused_write_is_write_busy_over_rest(refused_delete):
    response = refused_delete(WriteBusyError("busy-vault", 10.0, retry_after_secs=1))
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    body = response.json()
    assert body["code"] == "write_busy"
    assert body["details"] == {"retry_after_secs": 1}
    assert body["hint"] == WRITE_BUSY_HINT


def test_a_write_refused_part_way_says_it_partly_completed_over_rest(refused_delete):
    response = refused_delete(
        WriteBusyError("busy-vault", 10.0, retry_after_secs=1, partially_applied=True),
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    body = response.json()
    assert body["code"] == "write_busy"
    assert body["details"] == {"retry_after_secs": 1, "partially_applied": True}
    assert body["hint"] == WRITE_BUSY_PARTIAL_HINT
