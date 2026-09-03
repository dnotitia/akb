"""Consume the repository-owned E2E descriptor and prepare one test credential."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx


class RuntimeSetupError(RuntimeError):
    """Raised when a repository-owned runtime cannot be consumed safely."""


def redact_error(error: BaseException, secrets: Iterable[str] = ()) -> str:
    """Return an exception message without credential values."""

    message = str(error) or type(error).__name__
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        message = message.replace(secret, "[redacted]")
    return re.sub(r"Bearer\s+[^\s,}]+", "Bearer [redacted]", message, flags=re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    """Validated, credential-free coordinates from a schema-v2 descriptor."""

    scenario: str
    app_origin: str
    app_health_url: str
    fixture_health_url: str
    reset_url: str
    reset_body: dict[str, Any]
    discovery_url: str
    username_env: str
    password_env: str
    pat_env: str | None
    login_path: str

    @property
    def mcp_url(self) -> str:
        """Return the MCP endpoint under the descriptor's application origin."""

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

        scenario = _required_string(descriptor.get("scenario"), "descriptor scenario")
        services = _required_object(descriptor.get("services"), "descriptor services")
        app = _required_object(services.get("app"), "app service")
        fixture = _required_object(services.get("fixture"), "fixture service")
        app_origin = _origin(app.get("origin"), "app origin")
        fixture_origin = _origin(fixture.get("origin"), "fixture origin")
        app_health_url = _operation_url(app.get("health"), app_origin, "app health", "GET")
        _operation_url(app.get("discovery"), app_origin, "app discovery", "GET")
        fixture_health_url = _operation_url(
            fixture.get("health"), fixture_origin, "fixture health", "GET"
        )
        reset = _required_object(fixture.get("reset"), "fixture reset")
        reset_url = _operation_url(reset, fixture_origin, "fixture reset", "POST")
        reset_body = _required_object(reset.get("body"), "fixture reset body")
        if reset_body.get("scenario") != scenario:
            raise RuntimeSetupError("fixture reset scenario does not match descriptor")
        fixture_discovery_url = _operation_url(
            fixture.get("discovery"), fixture_origin, "fixture discovery", "GET"
        )

        credentials = _required_object(descriptor.get("credentials"), "descriptor credentials")
        username_env = _environment_name(credentials.get("username_env"), "username environment name")
        password_env = _environment_name(credentials.get("password_env"), "password environment name")
        raw_pat_env = credentials.get("pat_env")
        pat_env = _environment_name(raw_pat_env, "PAT environment name") if raw_pat_env is not None else None
        login_path = _path(credentials.get("login_path"), "credential login path")
        return cls(
            scenario=scenario,
            app_origin=app_origin,
            app_health_url=app_health_url,
            fixture_health_url=fixture_health_url,
            reset_url=reset_url,
            reset_body=dict(reset_body),
            discovery_url=fixture_discovery_url,
            username_env=username_env,
            password_env=password_env,
            pat_env=pat_env,
            login_path=login_path,
        )


def _required_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeSetupError(f"{label} must be an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeSetupError(f"{label} is required")
    return value


def _origin(value: Any, label: str) -> str:
    candidate = _required_string(value, label)
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


def _operation_url(value: Any, base: str, label: str, method: str) -> str:
    operation = _required_object(value, label)
    if operation.get("method") != method:
        raise RuntimeSetupError(f"{label} must use {method}")
    raw_url = _required_string(operation.get("url"), label)
    candidate = urljoin(f"{base}/", raw_url)
    try:
        parsed = urlsplit(candidate)
        base_parsed = urlsplit(base)
    except ValueError:
        raise RuntimeSetupError(f"{label} must stay on its declared origin") from None
    if (
        parsed.scheme != base_parsed.scheme
        or parsed.netloc != base_parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise RuntimeSetupError(f"{label} must stay on its declared origin")
    return candidate


def _path(value: Any, label: str) -> str:
    path = _required_string(value, label)
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise RuntimeSetupError(f"{label} is invalid")
    return path


def _environment_name(value: Any, label: str) -> str:
    name = _required_string(value, label)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
        raise RuntimeSetupError(f"{label} is invalid")
    return name


class RuntimeSession:
    """Reset the existing runtime and mint one in-memory MCP read credential."""

    def __init__(self, descriptor: RuntimeDescriptor, client: httpx.Client | None = None) -> None:
        self.descriptor = descriptor
        self.client = client or httpx.Client(timeout=30.0)
        self.pat = ""
        self.secrets: tuple[str, ...] = ()
        self._closed = False

    @classmethod
    def from_json(cls, raw: str, client: httpx.Client | None = None) -> RuntimeSession:
        return cls(RuntimeDescriptor.from_json(raw), client=client)

    @property
    def credential_env_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (self.descriptor.username_env, self.descriptor.password_env, self.descriptor.pat_env)
            if name is not None
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        stage: str,
        body: Mapping[str, Any] | None = None,
        authorization: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": authorization} if authorization else None
        try:
            response = self.client.request(method, url, json=body, headers=headers)
        except httpx.HTTPError:
            raise RuntimeSetupError(f"{stage} request failed") from None
        if response.status_code != 200:
            raise RuntimeSetupError(f"{stage} returned HTTP {response.status_code}")
        try:
            value = response.json()
        except ValueError:
            raise RuntimeSetupError(f"{stage} returned invalid JSON") from None
        if not isinstance(value, dict):
            raise RuntimeSetupError(f"{stage} returned an invalid object")
        return value

    def _ready(self, stage: str) -> None:
        app = self._request_json("GET", self.descriptor.app_health_url, stage=f"{stage} app health")
        fixture = self._request_json(
            "GET", self.descriptor.fixture_health_url, stage=f"{stage} fixture health"
        )
        if app.get("status") != "ready" or fixture.get("status") != "ready":
            raise RuntimeSetupError(f"{stage} runtime is not ready")

    def _validate_discovery(self, discovery: dict[str, Any]) -> str:
        if discovery.get("status") != "ready" or discovery.get("scenario") != self.descriptor.scenario:
            raise RuntimeSetupError("fixture discovery is not ready for this scenario")

        access = _required_object(discovery.get("access"), "fixture access")
        login = _required_object(access.get("login"), "fixture login")
        if login.get("service") != "app" or login.get("method") != "POST":
            raise RuntimeSetupError("fixture login coordinates are invalid")
        if login.get("path") != self.descriptor.login_path:
            raise RuntimeSetupError("descriptor and discovery login paths disagree")

        runtime = _required_object(discovery.get("runtime"), "fixture runtime")
        origins = _required_object(runtime.get("origin"), "fixture runtime origins")
        if origins.get("backend") != self.descriptor.app_origin:
            raise RuntimeSetupError("descriptor and discovery app origins disagree")
        transports = runtime.get("transport")
        if not isinstance(transports, list) or "http" not in transports:
            raise RuntimeSetupError("fixture runtime does not advertise HTTP transport")
        tool_cases = _required_object(runtime.get("tool_cases"), "fixture runtime tool cases")
        if tool_cases.get("read") != "akb_list_vaults":
            raise RuntimeSetupError("fixture runtime does not advertise the list-vaults case")
        credential_env = _required_object(runtime.get("credential_env"), "fixture runtime credentials")
        if (
            credential_env.get("username") != self.descriptor.username_env
            or credential_env.get("password") != self.descriptor.password_env
        ):
            raise RuntimeSetupError("descriptor and discovery credential names disagree")

        pat = _required_object(runtime.get("pat"), "fixture PAT")
        mint = _required_object(pat.get("mint"), "fixture PAT mint")
        if mint.get("service") != "app" or mint.get("method") != "POST":
            raise RuntimeSetupError("fixture PAT mint coordinates are invalid")
        return _path(mint.get("path"), "fixture PAT mint path")

    def prepare(self) -> None:
        self._ready("before reset")
        discovery = self._request_json(
            "GET", self.descriptor.discovery_url, stage="fixture discovery"
        )
        mint_path = self._validate_discovery(discovery)

        reset_result = self._request_json(
            "POST",
            self.descriptor.reset_url,
            stage="fixture reset",
            body=self.descriptor.reset_body,
        )
        if reset_result.get("status") != "ready" or reset_result.get("scenario") != self.descriptor.scenario:
            raise RuntimeSetupError("fixture reset did not return the selected ready scenario")

        self._ready("after reset")
        after_reset = self._request_json(
            "GET", self.descriptor.discovery_url, stage="fixture discovery after reset"
        )
        mint_path = self._validate_discovery(after_reset)

        username = os.environ.get(self.descriptor.username_env, "")
        password = os.environ.get(self.descriptor.password_env, "")
        if not username or not password:
            raise RuntimeSetupError("declared credential environment values are missing")
        self.secrets = (username, password)
        login_response = self._request_json(
            "POST",
            urljoin(f"{self.descriptor.app_origin}/", self.descriptor.login_path.lstrip("/")),
            stage="credential login",
            body={"username": username, "password": password},
        )
        session_token = login_response.get("token")
        if not isinstance(session_token, str) or not session_token:
            raise RuntimeSetupError("credential login returned no session token")
        self.secrets = (*self.secrets, session_token)

        mint_response = self._request_json(
            "POST",
            urljoin(f"{self.descriptor.app_origin}/", mint_path.lstrip("/")),
            stage="credential mint",
            authorization=f"Bearer {session_token}",
            body={"name": "mcp-pytest"},
        )
        token = mint_response.get("token")
        if not isinstance(token, str) or not token.startswith("akb_"):
            raise RuntimeSetupError("credential mint returned an invalid PAT")
        self.pat = token
        self.secrets = (*self.secrets, token)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.client.close()
        self.pat = ""
        self.secrets = ()
