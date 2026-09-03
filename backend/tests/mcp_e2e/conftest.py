"""Fixtures that connect one product scenario to the repository runtime."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from .driver import McpClientDriver
from .inspector_adapter import InspectorAdapterError, InspectorCliAdapter
from .runtime import RuntimeSession, RuntimeSetupError


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--runtime-descriptor",
        default=None,
        help="schema-v2 runtime descriptor path, or '-' to read it from stdin",
    )


def _read_descriptor(source: str) -> str:
    if source == "-":
        try:
            with os.fdopen(os.dup(0), "r", encoding="utf-8") as stream:
                return stream.read()
        except OSError:
            raise RuntimeSetupError("runtime descriptor stdin could not be read") from None
    try:
        return Path(source).read_text(encoding="utf-8")
    except OSError:
        raise RuntimeSetupError("runtime descriptor file could not be read") from None


@pytest.fixture
def runtime_session(request: pytest.FixtureRequest) -> Iterator[RuntimeSession]:
    source = request.config.getoption("--runtime-descriptor")
    if not source:
        pytest.fail("--runtime-descriptor is required for the live MCP scenario")
    session: RuntimeSession | None = None
    try:
        session = RuntimeSession.from_json(_read_descriptor(source))
        session.prepare()
    except RuntimeSetupError as exc:
        if session is not None:
            session.close()
        pytest.fail(str(exc))
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def mcp_driver(runtime_session: RuntimeSession) -> Iterator[McpClientDriver]:
    try:
        driver = InspectorCliAdapter(
            mcp_url=runtime_session.descriptor.mcp_url,
            pat=runtime_session.pat,
            secrets=runtime_session.secrets,
            secret_env_names=runtime_session.credential_env_names,
        )
    except InspectorAdapterError as exc:
        pytest.fail(f"scenario=akb_list_vaults transport=http setup: {exc}")
    try:
        yield driver
    finally:
        driver.close()
