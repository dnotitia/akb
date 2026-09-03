"""Pytest fixtures for authenticated MCP behavior scenarios."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .runtime import RuntimeSession, RuntimeSetupError, redact_error


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runtime-descriptor",
        default=None,
        help="schema-v2 runtime descriptor path, or '-' to read it from stdin",
    )


def _read_descriptor(source: str, capture_manager: Any = None) -> str:
    if source == "-":
        suspended = False
        try:
            if capture_manager is not None:
                capture_manager.suspend_global_capture(in_=True)
                suspended = True
            with os.fdopen(os.dup(0), "r", encoding="utf-8") as stream:
                return stream.read()
        except OSError:
            raise RuntimeSetupError("runtime descriptor stdin could not be read") from None
        finally:
            if suspended:
                capture_manager.resume_global_capture()
    try:
        return Path(source).read_text(encoding="utf-8")
    except OSError:
        raise RuntimeSetupError("runtime descriptor file could not be read") from None


@pytest.fixture
def runtime_session(request: pytest.FixtureRequest) -> Iterator[RuntimeSession]:
    source = request.config.getoption("--runtime-descriptor")
    if not source:
        pytest.fail("scenario=akb_list_vaults preparation: --runtime-descriptor is required")
    session: RuntimeSession | None = None
    try:
        capture_manager = request.config.pluginmanager.getplugin("capturemanager")
        session = RuntimeSession.from_json(_read_descriptor(source, capture_manager))
        session.prepare()
    except Exception as exc:
        secrets = session.secrets if session is not None else ()
        if session is not None:
            session.close()
        pytest.fail(f"scenario=akb_list_vaults preparation: {redact_error(exc, secrets)}")
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
async def mcp_client(runtime_session: RuntimeSession) -> AsyncIterator[Client]:
    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {runtime_session.pat}"},
        timeout=httpx2.Timeout(30.0, read=300.0),
        follow_redirects=True,
        trust_env=False,
    )
    client = Client(
        streamable_http_client(runtime_session.descriptor.mcp_url, http_client=http_client),
        mode="auto",
        read_timeout_seconds=30.0,
        cache=None,
    )
    entered = False
    try:
        async with client:
            entered = True
            yield client
    except Exception as exc:
        if entered:
            raise
        pytest.fail(
            "scenario=akb_list_vaults SDK connection: "
            + redact_error(exc, runtime_session.secrets)
        )
    finally:
        await http_client.aclose()
