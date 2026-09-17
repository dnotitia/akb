"""Descriptor parsing for the live MCP scenario."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit


class RuntimeSetupError(RuntimeError):
    """Raised when the repository-owned runtime cannot be consumed safely."""


def redact_error(error: BaseException, secrets: Iterable[str] = ()) -> str:
    """Return an exception message without credential values."""

    message = str(error) or type(error).__name__
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        message = message.replace(secret, "[redacted]")
    return re.sub(r"Bearer\s+[^\s,}]+", "Bearer [redacted]", message, flags=re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    """The schema-v2 coordinates needed by the MCP test fixture."""

    scenario: str
    app_origin: str
    app_health_url: str
    fixture_health_url: str
    reset_url: str
    reset_body: dict[str, Any]
    discovery_url: str
    username_env: str
    password_env: str
    login_path: str

    @property
    def mcp_url(self) -> str:
        return urljoin(f"{self.app_origin}/", "mcp/")

    @classmethod
    def from_json(cls, raw: str) -> RuntimeDescriptor:
        try:
            descriptor = json.loads(raw)
        except (TypeError, ValueError):
            raise RuntimeSetupError("runtime descriptor is invalid JSON") from None
        if not isinstance(descriptor, dict):
            raise RuntimeSetupError("runtime descriptor must be an object")
        if descriptor.get("schema_version") != 2 or descriptor.get("status") != "ready":
            raise RuntimeSetupError("runtime descriptor must be a ready schema-v2 descriptor")

        scenario = _string(descriptor.get("scenario"), "descriptor scenario")
        services = _object(descriptor.get("services"), "descriptor services")
        app = _object(services.get("app"), "app service")
        fixture = _object(services.get("fixture"), "fixture service")
        app_origin = _origin(app.get("origin"), "app origin")
        fixture_origin = _origin(fixture.get("origin"), "fixture origin")
        app_health_url = _operation(app.get("health"), app_origin, "app health", "GET")
        _operation(app.get("discovery"), app_origin, "app discovery", "GET")
        fixture_health_url = _operation(fixture.get("health"), fixture_origin, "fixture health", "GET")
        reset = _object(fixture.get("reset"), "fixture reset")
        reset_url = _operation(reset, fixture_origin, "fixture reset", "POST")
        reset_body = _object(reset.get("body"), "fixture reset body")
        if reset_body.get("scenario") != scenario:
            raise RuntimeSetupError("fixture reset scenario does not match descriptor")
        discovery_url = _operation(fixture.get("discovery"), fixture_origin, "fixture discovery", "GET")

        credentials = _object(descriptor.get("credentials"), "descriptor credentials")
        username_env = _environment_name(credentials.get("username_env"), "username environment name")
        password_env = _environment_name(credentials.get("password_env"), "password environment name")
        login_path = _path(credentials.get("login_path"), "credential login path")
        return cls(
            scenario=scenario,
            app_origin=app_origin,
            app_health_url=app_health_url,
            fixture_health_url=fixture_health_url,
            reset_url=reset_url,
            reset_body=dict(reset_body),
            discovery_url=discovery_url,
            username_env=username_env,
            password_env=password_env,
            login_path=login_path,
        )


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    descriptor: RuntimeDescriptor
    pat: str
    secrets: tuple[str, ...]


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeSetupError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeSetupError(f"{label} is required")
    return value


def _origin(value: Any, label: str) -> str:
    candidate = _string(value, label)
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        raise RuntimeSetupError(f"{label} must be an HTTP(S) origin") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeSetupError(f"{label} must be an HTTP(S) origin")
    return candidate.rstrip("/")


def _operation(value: Any, base: str, label: str, method: str) -> str:
    operation = _object(value, label)
    if operation.get("method") != method:
        raise RuntimeSetupError(f"{label} must use {method}")
    candidate = urljoin(f"{base}/", _string(operation.get("url"), label))
    try:
        parsed = urlsplit(candidate)
        origin = urlsplit(base)
    except ValueError:
        raise RuntimeSetupError(f"{label} must stay on its declared origin") from None
    if (
        parsed.scheme != origin.scheme
        or parsed.netloc != origin.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise RuntimeSetupError(f"{label} must stay on its declared origin")
    return candidate


def _path(value: Any, label: str) -> str:
    path = _string(value, label)
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise RuntimeSetupError(f"{label} is invalid")
    return path


def _environment_name(value: Any, label: str) -> str:
    name = _string(value, label)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
        raise RuntimeSetupError(f"{label} is invalid")
    return name
