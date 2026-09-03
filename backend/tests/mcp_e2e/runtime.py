"""Runtime descriptor and authenticated fixture setup for MCP scenarios."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx


class RuntimeSetupError(RuntimeError):
    """Raised when the repository-owned runtime is not consumable."""


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    """Validated, credential-free coordinates from the schema-v2 descriptor."""

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
        return urljoin(f"{self.app_origin}/", "mcp/")

    @classmethod
    def from_json(cls, raw: str) -> "RuntimeDescriptor":
        try:
            descriptor = json.loads(raw)
        except TypeError, ValueError:
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
        app_health_url = _same_origin(app.get("health"), app_origin, "app health")
        fixture_health_url = _same_origin(fixture.get("health"), fixture_origin, "fixture health")
        reset = _required_object(fixture.get("reset"), "fixture reset")
        if reset.get("method") != "POST":
            raise RuntimeSetupError("fixture reset must use POST")
        reset_url = _same_origin(reset, fixture_origin, "fixture reset")
        reset_body = _required_object(reset.get("body"), "fixture reset body")
        if reset_body.get("scenario") != scenario:
            raise RuntimeSetupError("fixture reset scenario does not match descriptor")
        discovery_url = _same_origin(fixture.get("discovery"), fixture_origin, "fixture discovery")
        credentials = _required_object(descriptor.get("credentials"), "descriptor credentials")
        username_env = _environment_name(credentials.get("username_env"), "username environment name")
        password_env = _environment_name(credentials.get("password_env"), "password environment name")
        pat_env_value = credentials.get("pat_env")
        pat_env = _environment_name(pat_env_value, "PAT environment name") if pat_env_value is not None else None
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
    ):
        raise RuntimeSetupError(f"{label} must be an HTTP(S) origin")
    if parsed.path not in {"", "/"}:
        raise RuntimeSetupError(f"{label} must be an HTTP(S) origin")
    return candidate.rstrip("/")


def _same_origin(value: Any, base: str, label: str) -> str:
    item = _required_object(value, label)
    raw_url = _required_string(item.get("url"), label)
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
    if not path.startswith("/") or path.startswith("//") or "#" in path:
        raise RuntimeSetupError(f"{label} is invalid")
    return path


def _environment_name(value: Any, label: str) -> str:
    name = _required_string(value, label)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
        raise RuntimeSetupError(f"{label} is invalid")
    return name


class RuntimeSession:
    """Reset the existing runtime and obtain one in-memory read credential."""

    def __init__(self, descriptor: RuntimeDescriptor, client: httpx.Client | None = None) -> None:
        self.descriptor = descriptor
        self.client = client or httpx.Client(timeout=30.0)
        self.pat = ""
        self.secrets: tuple[str, ...] = ()
        self._closed = False

    @classmethod
    def from_json(cls, raw: str, client: httpx.Client | None = None) -> "RuntimeSession":
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
        fixture = self._request_json("GET", self.descriptor.fixture_health_url, stage=f"{stage} fixture health")
        if app.get("status") != "ready" or fixture.get("status") != "ready":
            raise RuntimeSetupError(f"{stage} runtime is not ready")

    def prepare(self) -> None:
        self._ready("before reset")
        discovery = self._request_json("GET", self.descriptor.discovery_url, stage="fixture discovery")
        if discovery.get("status") != "ready" or discovery.get("scenario") != self.descriptor.scenario:
            raise RuntimeSetupError("fixture discovery is not ready for this scenario")
        access = _required_object(discovery.get("access"), "fixture access")
        login = _required_object(access.get("login"), "fixture login")
        if login.get("path") != self.descriptor.login_path:
            raise RuntimeSetupError("descriptor and discovery login paths disagree")
        runtime = _required_object(discovery.get("runtime"), "fixture runtime")
        pat = _required_object(runtime.get("pat"), "fixture PAT")
        mint = _required_object(pat.get("mint"), "fixture PAT mint")
        mint_path = _path(mint.get("path"), "fixture PAT mint path")

        reset_result = self._request_json(
            "POST",
            self.descriptor.reset_url,
            stage="fixture reset",
            body=self.descriptor.reset_body,
        )
        if reset_result.get("status") != "ready" or reset_result.get("scenario") != self.descriptor.scenario:
            raise RuntimeSetupError("fixture reset did not return the selected ready scenario")
        self._ready("after reset")
        after_reset = self._request_json("GET", self.descriptor.discovery_url, stage="fixture discovery after reset")
        if after_reset.get("status") != "ready" or after_reset.get("scenario") != self.descriptor.scenario:
            raise RuntimeSetupError("fixture discovery is not ready after reset")

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
        session = login_response.get("token")
        if not isinstance(session, str) or not session:
            raise RuntimeSetupError("credential login returned no session token")
        self.secrets = (*self.secrets, session)
        mint_response = self._request_json(
            "POST",
            urljoin(f"{self.descriptor.app_origin}/", mint_path.lstrip("/")),
            stage="credential mint",
            authorization=f"Bearer {session}",
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
