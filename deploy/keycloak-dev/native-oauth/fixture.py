#!/usr/bin/env python3
"""Own one disposable Keycloak realm for the packed CLI acceptance."""

from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote, urlsplit

import httpx


KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak@sha256:0f198be292568439d700cdbfb893e69a6009bb43a94a06a945b1d3d506c76b13"
SCOPES = {"akb:vault:read", "akb:vault:write"}


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def validate_resource(resource: str) -> str:
    parsed = urlsplit(resource)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or not parsed.path.endswith("/mcp")):
        raise ValueError("fixture resource must be an exact HTTP 127.0.0.1 MCP URL with an explicit port")
    return resource


class NativeOAuthFixture:
    def __init__(self, *, checkout: Path, runtime_root: Path, resource: str, relay_port: int) -> None:
        self.checkout = checkout.resolve()
        self.root = runtime_root.resolve()
        if self.root == self.checkout or self.root.is_relative_to(self.checkout):
            raise ValueError("native OAuth runtime state must be outside the checkout")
        self.resource = validate_resource(resource)
        self.provisioner = self.checkout / "scripts/keycloak/setup-akb-mcp-oauth.py"
        self.port = free_port()
        self.service_origin = f"http://127.0.0.1:{self.port}"
        self.origin = f"http://127.0.0.1:{relay_port}"
        self.realm = f"akb-native-{secrets.token_hex(6)}"
        self.project = f"akb-native-oauth-{secrets.token_hex(6)}"
        self.compose_file = Path(__file__).with_name("compose.yaml")
        self.username = "oauth-" + secrets.token_hex(6)
        self.password = secrets.token_urlsafe(32)
        self.admin_username = "fixture-admin"
        self.admin_password = secrets.token_urlsafe(32)
        self.environment = {
            **os.environ,
            "AKB_NATIVE_OAUTH_ADMIN_USERNAME": self.admin_username,
            "AKB_NATIVE_OAUTH_ADMIN_PASSWORD": self.admin_password,
            "AKB_NATIVE_OAUTH_KEYCLOAK_PORT": str(self.port),
            "AKB_NATIVE_OAUTH_KEYCLOAK_ORIGIN": self.origin,
        }
        self.http = httpx.Client(trust_env=False, follow_redirects=False, timeout=10)
        self.commands: list[dict[str, object]] = []
        self.registration: dict[str, object] = {}
        self.cleanup: dict[str, object] = {}
        self.admin_token = ""
        self.admin_expires_at = 0.0
        self._stop_requested = threading.Event()
        self._active_process: subprocess.Popen | None = None

    @property
    def issuer(self) -> str:
        return f"{self.origin}/realms/{self.realm}"

    def _compose(self, *arguments: str) -> list[str]:
        return ["docker", "compose", "--project-name", self.project, "--file", str(self.compose_file), *arguments]

    def _command(self, command: list[str], label: str, *, environment: dict[str, str] | None = None) -> None:
        if self._stop_requested.is_set() and label != "compose-down":
            raise InterruptedError("native authority startup interrupted")
        log = self.root / f"{label}.log"
        entry: dict[str, object] = {"command": command, "exit": None, "output": str(log)}
        self.commands.append(entry)
        with log.open("ab") as output:
            log.chmod(0o600)
            process = subprocess.Popen(
                command, env=environment or self.environment, cwd=self.root,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )
            self._active_process = process
            if self._stop_requested.is_set() and label != "compose-down":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=180)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                raise
            finally:
                entry["exit"] = process.returncode
                self._active_process = None
        if process.returncode:
            raise RuntimeError(f"{label} failed with exit {process.returncode}")

    def _authenticate_admin(self) -> None:
        response = self.http.post(f"{self.service_origin}/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli",
            "username": self.admin_username, "password": self.admin_password,
        })
        if response.status_code != 200:
            raise RuntimeError(f"fixture admin authentication failed with HTTP {response.status_code}")
        token = response.json()
        self.admin_token = token["access_token"]
        self.admin_expires_at = time.monotonic() + max(0, token["expires_in"] - 5)

    def _admin(self, method: str, path: str, *, body: object = None, expected: int = 200) -> httpx.Response:
        if self._stop_requested.is_set():
            raise InterruptedError("native authority startup interrupted")
        if time.monotonic() >= self.admin_expires_at:
            self._authenticate_admin()
        response = self.http.request(
            method, f"{self.service_origin}/admin/{path}", json=body,
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        if response.status_code != expected:
            raise RuntimeError(f"fixture admin operation failed with HTTP {response.status_code}")
        return response

    def request_stop(self) -> None:
        self._stop_requested.set()
        process = self._active_process
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self._command(["docker", "image", "inspect", KEYCLOAK_IMAGE, "--format", "{{.Id}}"], "image")
        self._command(self._compose("up", "--detach", "--pull", "never"), "compose-up")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self._stop_requested.is_set():
                raise InterruptedError("native authority startup interrupted")
            try:
                ready = self.http.get(f"{self.service_origin}/realms/master/.well-known/openid-configuration")
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("real Keycloak readiness timed out")
        self._admin("POST", "realms", expected=201, body={
            "realm": self.realm, "enabled": True, "sslRequired": "none",
            "registrationAllowed": False, "accessTokenLifespan": 60,
        })
        self._admin("POST", f"realms/{self.realm}/users", expected=201, body={
            "username": self.username, "enabled": True, "emailVerified": True,
            "email": f"{self.username}@example.test", "firstName": "Native", "lastName": "Fixture",
            "credentials": [{"type": "password", "temporary": False, "value": self.password}],
        })
        for _ in range(2):
            self.provision()

    def provision(self) -> None:
        environment = {key: value for key, value in self.environment.items() if not key.startswith("KC_ADMIN_")}
        environment.update({"KC_ADMIN_USER": self.admin_username, "KC_ADMIN_PASS": self.admin_password})
        self._command([
            sys.executable, str(self.provisioner), "--kc", self.service_origin, "--realm", self.realm,
            "--audience", self.resource,
        ], "provision", environment=environment)

    def verify_registration(self, client_id: str) -> dict[str, bool]:
        realm = self._admin("GET", f"realms/{self.realm}").json()
        clients = self._admin("GET", f"realms/{self.realm}/clients?clientId={quote(client_id, safe='')}").json()
        assert len(clients) == 1 and clients[0]["clientId"] == client_id, "DCR created one client"
        client = clients[0]
        expected_flags = {
            "publicClient": True, "standardFlowEnabled": True, "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False,
        }
        assert all(client.get(key) is value for key, value in expected_flags.items()), "public code-only grant policy"
        assert client.get("redirectUris") == ["http://127.0.0.1/oauth/callback"], "exact loopback callback template"
        assert client.get("attributes", {}).get("pkce.code.challenge.method") == "S256", "Keycloak enforces S256"
        assert realm.get("revokeRefreshToken") is True and realm.get("refreshTokenMaxReuse") == 0, "zero reuse refresh policy"
        assert SCOPES.issubset(set(client.get("optionalClientScopes", []))), "both AKB scopes admitted"
        self.registration = {key: True for key in [
            "anonymous_dcr", "public_code_s256", "exact_loopback_template",
            "password_implicit_service_grants_disabled", "both_akb_scopes_admitted", "zero_reuse_refresh_rotation",
        ]}
        return self.registration

    def close(self) -> None:
        try:
            self._command(self._compose("down", "--volumes", "--remove-orphans"), "compose-down")
            for kind, command in {
                "containers": ["docker", "ps", "--all", "--quiet"],
                "networks": ["docker", "network", "ls", "--quiet"],
                "volumes": ["docker", "volume", "ls", "--quiet"],
            }.items():
                result = subprocess.run(
                    [*command, "--filter", f"label=com.docker.compose.project={self.project}"],
                    capture_output=True, text=True, timeout=20,
                )
                self.cleanup[kind] = result.returncode == 0 and not result.stdout.strip()
            with socket.socket() as connection:
                connection.settimeout(1)
                self.cleanup["port_closed"] = connection.connect_ex(("127.0.0.1", self.port)) != 0
            if not all(self.cleanup.values()):
                raise RuntimeError("native OAuth fixture cleanup could not be verified")
        finally:
            self.http.close()
            self.admin_token = ""

    def receipt(self) -> dict[str, object]:
        return {
            "keycloak_image": KEYCLOAK_IMAGE, "origin": self.origin, "realm": self.realm,
            "resource": self.resource, "compose_project": self.project,
            "provisioner": str(self.provisioner),
            "provisioner_sha256": hashlib.sha256(self.provisioner.read_bytes()).hexdigest() if self.provisioner.is_file() else None,
            "registration": self.registration, "commands": self.commands, "cleanup": self.cleanup,
        }
