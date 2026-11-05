#!/usr/bin/env python3
"""Own one disposable real Keycloak native-client fixture."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

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
    def __init__(self, *, checkout: Path, runtime_root: Path, resource: str, provisioner: Path | None = None) -> None:
        self.checkout = checkout.resolve()
        self.root = runtime_root.resolve()
        if self.root == self.checkout or self.root.is_relative_to(self.checkout):
            raise ValueError("native OAuth runtime state must be outside the checkout")
        self.resource = validate_resource(resource)
        self.provisioner = (provisioner or self.checkout / "scripts/keycloak/setup-akb-mcp-oauth.py").resolve()
        self.port = free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.realm = f"akb-native-{secrets.token_hex(6)}"
        self.client_id = "akb-native-fixture"
        self.project = f"akb-native-oauth-{secrets.token_hex(6)}"
        self.compose_file = Path(__file__).with_name("compose.yaml")
        username = os.environ.get("AKB_E2E_USERNAME", "")
        self.password = os.environ.get("AKB_E2E_PASSWORD", "")
        if not self.password or not username:
            raise ValueError("AKB_E2E_USERNAME and AKB_E2E_PASSWORD must be nonempty")
        self.username = "oauth-" + username
        self.admin_username = "fixture-admin"
        self.admin_password = secrets.token_urlsafe(32)
        self.environment = {
            **os.environ,
            "AKB_NATIVE_OAUTH_ADMIN_USERNAME": self.admin_username,
            "AKB_NATIVE_OAUTH_ADMIN_PASSWORD": self.admin_password,
            "AKB_NATIVE_OAUTH_KEYCLOAK_PORT": str(self.port),
        }
        self.http = httpx.Client(trust_env=False, follow_redirects=False, timeout=10)
        self.commands: list[dict[str, object]] = []
        self.registration: dict[str, object] = {}
        self.cleanup: dict[str, object] = {}
        self.admin_token = ""
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

    def _admin(self, method: str, path: str, *, body: object = None, expected: int = 200) -> httpx.Response:
        if self._stop_requested.is_set():
            raise InterruptedError("native authority startup interrupted")
        response = self.http.request(
            method, f"{self.origin}/admin/{path}", json=body,
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
                ready = self.http.get(f"{self.origin}/realms/master/.well-known/openid-configuration")
                if ready.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("real Keycloak readiness timed out")
        response = self.http.post(f"{self.origin}/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli",
            "username": self.admin_username, "password": self.admin_password,
        })
        if response.status_code != 200:
            raise RuntimeError(f"fixture admin authentication failed with HTTP {response.status_code}")
        self.admin_token = response.json()["access_token"]
        self._admin("POST", "realms", expected=201, body={
            "realm": self.realm, "enabled": True, "sslRequired": "none",
            "registrationAllowed": False, "accessTokenLifespan": 60,
        })
        self._admin("POST", f"realms/{self.realm}/users", expected=201, body={
            "username": self.username, "enabled": True, "emailVerified": True,
            "email": f"{self.username}@example.test", "firstName": "Native", "lastName": "Fixture",
            "credentials": [{"type": "password", "temporary": False, "value": self.password}],
        })
        self.provision()

    def provision(self) -> None:
        environment = {key: value for key, value in self.environment.items() if not key.startswith("KC_ADMIN_")}
        environment.update({"KC_ADMIN_USER": self.admin_username, "KC_ADMIN_PASS": self.admin_password})
        self._command([
            sys.executable, str(self.provisioner), "--kc", self.origin, "--realm", self.realm,
            "--audience", self.resource, "--native-client-id", self.client_id,
        ], "native-provision", environment=environment)
        self.registration = self.verify_registration()

    def verify_registration(self) -> dict[str, object]:
        realm = self._admin("GET", f"realms/{self.realm}").json()
        clients = self._admin("GET", f"realms/{self.realm}/clients?clientId={self.client_id}").json()
        if len(clients) != 1:
            raise RuntimeError("native client registration is not unique")
        client = self._admin("GET", f"realms/{self.realm}/clients/{clients[0]['id']}").json()
        expected_flags = {
            "publicClient": True, "standardFlowEnabled": True, "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False, "fullScopeAllowed": False,
        }
        if any(client.get(key) is not value for key, value in expected_flags.items()):
            raise RuntimeError("native client grant or public-client flags differ from the contract")
        if client.get("redirectUris") != ["http://127.0.0.1/oauth/callback"]:
            raise RuntimeError("native client callback registration differs from the exact loopback path")
        if client.get("attributes", {}).get("pkce.code.challenge.method") != "S256":
            raise RuntimeError("native client does not require S256")
        if realm.get("revokeRefreshToken") is not True or realm.get("refreshTokenMaxReuse") != 0:
            raise RuntimeError("realm does not require zero-reuse refresh rotation")
        if set(client.get("defaultClientScopes", [])) != {"basic", "profile", "email"}:
            raise RuntimeError("native default scopes differ from human profile claims")
        if set(client.get("optionalClientScopes", [])) != SCOPES:
            raise RuntimeError("native optional scopes differ from the two AKB scopes")
        audience = [mapper for mapper in client.get("protocolMappers", [])
                    if mapper.get("protocolMapper") == "oidc-audience-mapper"]
        if len(audience) != 1 or audience[0].get("config", {}).get("included.custom.audience") != self.resource:
            raise RuntimeError("native audience mapper does not target exactly the MCP resource")
        return {
            "public_code_s256": True, "exact_loopback_callback": True, "human_claim_scopes": True,
            "exact_resource_mapper": True, "zero_reuse_refresh_rotation": True,
            "password_implicit_service_grants_disabled": True,
        }

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
            "resource": self.resource, "native_client_id": self.client_id, "compose_project": self.project,
            "provisioner": str(self.provisioner),
            "provisioner_sha256": hashlib.sha256(self.provisioner.read_bytes()).hexdigest(),
            "registration": self.registration, "commands": self.commands, "cleanup": self.cleanup,
            "acceptance_scope": "Real native-client provisioning only. No proxy OAuth, browser, signed token, or refresh exchange proof.",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--provisioner", type=Path, help="explicit immutable production provisioning artifact")
    args = parser.parse_args()
    root = args.runtime_root or Path(tempfile.mkdtemp(prefix="akb-native-oauth-"))
    fixture = NativeOAuthFixture(
        checkout=args.checkout, runtime_root=root, resource=args.resource, provisioner=args.provisioner,
    )

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    previous_handlers = {signum: signal.signal(signum, stop) for signum in (signal.SIGINT, signal.SIGTERM)}
    error = None
    try:
        fixture.start()
        first = fixture.registration
        fixture.provision()
        if first != fixture.registration:
            raise RuntimeError("native provisioning did not converge on the same verified registration")
    except (RuntimeError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    except KeyboardInterrupt:
        error = "interrupted"
    finally:
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        try:
            fixture.close()
        except (RuntimeError, subprocess.SubprocessError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    receipt = fixture.receipt()
    receipt["result"] = "FAIL" if error else "PASS"
    if error:
        receipt["error"] = error
    report = fixture.root / "provisioning-report.json"
    report.write_text(json.dumps(receipt, indent=2) + "\n")
    report.chmod(0o600)
    print(json.dumps({"result": receipt["result"], "report": str(report)}))
    return 130 if error == "interrupted" else 1 if error else 0


if __name__ == "__main__":
    raise SystemExit(main())
