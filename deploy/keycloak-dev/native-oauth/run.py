#!/usr/bin/env python3
"""Exercise the packed proxy against real Keycloak and the existing E2E runtime."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time

import httpx
import jwt
import yaml

from fixture import NativeOAuthFixture, free_port

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts/ci"))
from e2e_runtime import E2ERuntime, PROTOCOL_REVISION, RuntimeConfig  # noqa: E402


class NativeOAuthRuntime(E2ERuntime):
    def __init__(self, config: RuntimeConfig, authority: NativeOAuthFixture):
        super().__init__(config)
        self.authority = authority

    def _write_config(self) -> None:
        super()._write_config()
        path = self.config.config_dir / "app.yaml"
        config = yaml.safe_load(path.read_text())
        config.update({
            "keycloak_enabled": True, "mcp_oauth_enabled": True,
            "keycloak_server_url": self.authority.origin, "keycloak_internal_url": self.authority.origin,
            "keycloak_realm": self.authority.realm, "mcp_oauth_native_client_id": self.authority.client_id,
            "mcp_oauth_direct_client_ids": [], "keycloak_enrollment_mode": "open",
            "keycloak_require_verified_email": True,
        })
        path.write_text(yaml.safe_dump(config))
        path.chmod(0o600)


def public_result(response: dict, *, error: bool = False) -> dict:
    if "error" in response or not isinstance(response.get("result"), dict):
        raise RuntimeError("tool response has a protocol error")
    result = response["result"]
    if bool(result.get("isError", False)) is not error:
        raise RuntimeError("tool response error state differs from the expected outcome")
    public = json.loads(result["content"][0]["text"])
    if not isinstance(public, dict):
        raise RuntimeError("tool response is not a public JSON object")
    return public


async def call(runtime: E2ERuntime, name: str, arguments: dict | None = None, *, error: bool = False) -> dict:
    return public_result(await runtime._stdio_request("tools/call", {"name": name, "arguments": arguments or {}}), error=error)


async def start_oauth_stdio(runtime: E2ERuntime, authority: NativeOAuthFixture, store: Path, opener: Path, capture: Path) -> int:
    await runtime._stop_named_process("stdio")
    managed = await runtime._spawn_interactive_process(
        "stdio", [shutil.which("node"), str(runtime._proxy_executable), "--url", authority.resource],
        "oauth-proxy.log", environment={
            "AKB_PAT": "", "AKB_MCP_AUTH_DIR": str(store),
            "AKB_NATIVE_OAUTH_URL_CAPTURE": str(capture), "PATH": str(opener) + os.pathsep + os.environ["PATH"],
        },
    )
    response = await runtime._stdio_request("initialize", {
        "protocolVersion": PROTOCOL_REVISION, "capabilities": {},
        "clientInfo": {"name": "real-native-oauth-fixture", "version": "1"},
    })
    if "error" in response:
        raise RuntimeError("OAuth stdio initialization failed")
    managed.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
    await managed.stdin.drain()
    return managed.process.pid


async def wait_ready(runtime: E2ERuntime, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await call(runtime, "akb_connection")
        if status.get("authentication") == "ready":
            return {key: status.get(key) for key in ["mode", "authentication", "connectivity"]}
        if status.get("authentication") in {"storage_error", "reauthorization_required", "rejected"}:
            raise RuntimeError("OAuth did not become ready: " + status["authentication"])
        await asyncio.sleep(0.1)
    raise RuntimeError("OAuth readiness timed out")


def read_grant(store: Path, resource: str) -> dict:
    key = hashlib.sha256(resource.encode()).hexdigest()
    return json.loads((store / f"{key}.json").read_text())


async def exercise(args) -> int:
    root = args.runtime_root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    ports: list[int] = []
    while len(ports) < 5:
        port = free_port()
        if port not in ports:
            ports.append(port)
    authority = NativeOAuthFixture(checkout=args.checkout, runtime_root=root / "authority", resource=f"http://127.0.0.1:{ports[0]}/mcp")
    config = RuntimeConfig(
        checkout=args.checkout.resolve(), runtime_root=root / "product", mode="serve",
        compose_file=args.compose_file.resolve(), compose_project="akb-native-product-" + secrets.token_hex(6),
        profile="transport-proxy", app_port=ports[0], embed_port=ports[1], fixture_port=ports[2],
        postgres_port=ports[3], minio_port=ports[4],
    )
    runtime = NativeOAuthRuntime(config, authority)
    report: dict = {"scope": "Real packed proxy browser, MCP read, signed grant, refresh and restart. Files, crash and native hosts remain separate gates.", "stage": "authority", "result": "FAIL", "source_revision": os.environ.get("AKB_E2E_SOURCE_REVISION"), "commands": []}
    store = root / "oauth-state"
    capture = root / "authorization-url.private"
    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    stop_requested = asyncio.Event()

    def request_stop():
        if stop_requested.is_set():
            return
        stop_requested.set()
        authority.request_stop()
        current_task.cancel()

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, request_stop)
    try:
        authority_start = asyncio.create_task(asyncio.to_thread(authority.start))
        try:
            await asyncio.shield(authority_start)
        except asyncio.CancelledError:
            authority.request_stop()
            with contextlib.suppress(Exception):
                await asyncio.shield(authority_start)
            raise
        metadata = await asyncio.to_thread(authority.http.get, f"{authority.issuer}/.well-known/openid-configuration")
        public_metadata = metadata.json()
        report["authorization_server_metadata"] = {key: public_metadata.get(key) for key in [
            "issuer", "token_endpoint_auth_methods_supported", "grant_types_supported",
            "response_types_supported", "code_challenge_methods_supported",
        ]}
        server_info = await asyncio.to_thread(authority._admin, "GET", "serverinfo")
        report["keycloak_version"] = server_info.json().get("systemInfo", {}).get("version")
        report["stage"] = "product"
        await runtime.prepare()
        report["package_artifact"] = runtime.runtime_evidence()["stdio"]["package_artifact"]
        opener = root / "opener"
        opener.mkdir(mode=0o700)
        for name in ["open", "xdg-open"]:
            path = opener / name
            path.write_text('#!/bin/sh\numask 077\nprintf \'%s\\n\' "$1" >> "$AKB_NATIVE_OAUTH_URL_CAPTURE"\n')
            path.chmod(0o700)
        report["stage"] = "pending"
        first_pid = await start_oauth_stdio(runtime, authority, store, opener, capture)
        catalog = await runtime._stdio_request("tools/list", {})
        names = {tool["name"] for tool in catalog["result"]["tools"]}
        assert {"akb_list_vaults", "akb_create_vault", "akb_put_file", "akb_connection"}.issubset(names)
        assert "result" in await runtime._stdio_request("ping", {})
        pending_name = "must-not-execute-" + secrets.token_hex(6)
        pending = await call(runtime, "akb_create_vault", {"name": pending_name}, error=True)
        assert pending.get("execution") == "not_started" and pending.get("code") == "authentication_pending"
        report["pending"] = {"catalog_count": len(names), "ping": True, "execution": "not_started", "browser_calls": 0}
        deadline = time.monotonic() + 30
        while not capture.exists():
            status = await call(runtime, "akb_connection")
            if status.get("authentication") in {"storage_error", "reauthorization_required", "configuration_error"}:
                report["pending_status"] = {key: status.get(key) for key in ["mode", "authentication", "connectivity"]}
                raise RuntimeError("No browser URL: " + status["authentication"])
            if time.monotonic() > deadline:
                status = await call(runtime, "akb_connection")
                raise RuntimeError("No browser URL: " + status.get("authentication", "unknown"))
            await asyncio.sleep(0.1)
        report["stage"] = "browser"
        report["pending"]["browser_calls"] = 1
        browser_command = [shutil.which("node"), str(Path(__file__).with_name("browser-login.mjs")), str(capture), str(root / "browser-report.json")]
        async with asyncio.timeout(90):
            browser_exit = await runtime._run_logged_command(
                browser_command, log_path=root / "browser-command.log", check=False,
                environment={
                    "AKB_NATIVE_OAUTH_USERNAME": authority.username, "AKB_NATIVE_OAUTH_ISSUER": authority.issuer,
                    "AKB_NATIVE_OAUTH_CLIENT_ID": authority.client_id, "AKB_NATIVE_OAUTH_RESOURCE": authority.resource,
                },
            )
        report["commands"].append({"command": browser_command, "exit": browser_exit, "output": str(root / "browser-command.log")})
        assert browser_exit == 0, "real browser flow failed"
        report["stage"] = "signed_claims"
        report["ready"] = await wait_ready(runtime)
        grant = read_grant(store, authority.resource)
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            keys = (await client.get(f"{authority.issuer}/protocol/openid-connect/certs")).json()
            key_id = jwt.get_unverified_header(grant["tokens"]["access_token"])["kid"]
            key = next(key for key in jwt.PyJWKSet.from_dict(keys).keys if key.key_id == key_id)
            claims = jwt.decode(
                grant["tokens"]["access_token"], key.key, algorithms=["RS256"],
                issuer=authority.issuer, audience=authority.resource,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
            report["verified_claim_diagnostic"] = {
                "signature_issuer_audience_expiry_verified": True,
                "present": {name: name in claims for name in ["exp", "iat", "jti", "aud", "iss", "sub", "typ", "azp", "sid", "scope", "email_verified", "preferred_username"]},
                "audience": claims.get("aud"), "client_id": claims.get("azp"),
                "scopes": claims.get("scope"), "token_kind": claims.get("typ"),
                "expected_username": claims.get("preferred_username") == authority.username,
            }
            assert claims["aud"] in [authority.resource, [authority.resource]]
            assert all(isinstance(claims.get(name), str) and claims[name] for name in ["jti", "sub", "sid", "scope"])
            assert claims["azp"] == authority.client_id and claims.get("sub") and claims.get("email_verified") is True
            assert claims.get("preferred_username") == authority.username and claims.get("typ") == "Bearer"
            assert {"akb:vault:read", "akb:vault:write"}.issubset(claims["scope"].split())
            report["signed_claims"] = {"jwks_verified": True, "issuer": True, "exact_audience": True, "native_client": True, "human": True, "scopes": True}
            report["stage"] = "read"
            await runtime._probe_stdio_behavior()
            report["validated_read_observed"] = runtime.runtime_evidence()["stdio"]["read_call_observed"]
            public = await call(runtime, "akb_list_vaults")
            assert isinstance(public.get("vaults"), list) and public.get("returned") == len(public["vaults"])
            assert public.get("total") >= public["returned"] and all(vault.get("name") != pending_name for vault in public["vaults"])
            assert runtime._children["stdio"].process.pid == first_pid
            report["same_stdio_new_read"] = True
            report["pending_mutation_never_executed"] = True
            report["stage"] = "rotation"
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                current = read_grant(store, authority.resource)
                if current.get("generation", -1) > grant["generation"] and not current.get("refreshIntent"):
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("real refresh rotation did not publish a new generation")
            assert current["tokens"]["refresh_token"] != grant["tokens"]["refresh_token"]
            report["rotation"] = {"old_generation": grant["generation"], "new_generation": current["generation"], "replacement_pair": current["tokens"]["access_token"] != grant["tokens"]["access_token"]}
            report["stage"] = "restart"
            openings = len(capture.read_text().splitlines())
            second_pid = await start_oauth_stdio(runtime, authority, store, opener, capture)
            await wait_ready(runtime)
            await call(runtime, "akb_list_vaults")
            assert second_pid != first_pid and len(capture.read_text().splitlines()) == openings
            report["restart_without_browser"] = True
            rejected = await client.post(f"{authority.issuer}/protocol/openid-connect/token", data={
                "grant_type": "refresh_token", "client_id": authority.client_id, "refresh_token": grant["tokens"]["refresh_token"],
            })
            assert rejected.status_code == 400 and rejected.json().get("error") == "invalid_grant"
            report["old_refresh_rejected"] = True
        report["result"] = "PASS"
        report["stage"] = "complete"
    except asyncio.CancelledError:
        report["cancelled"] = True
        report["result"] = "FAIL"
    except Exception as error:
        report["error_type"] = type(error).__name__
        if isinstance(error, RuntimeError):
            report["error"] = str(error)
    finally:
        try:
            await runtime.cleanup()
            report["runtime_cleanup_completed"] = True
        except Exception as error:
            report["runtime_cleanup_error"] = type(error).__name__
            report["result"] = "FAIL"
        try:
            await asyncio.to_thread(authority.close)
        except Exception as error:
            report["authority_cleanup_error"] = type(error).__name__
            report["result"] = "FAIL"
        report["authority"] = authority.receipt()
        report["cleanup"] = {}
        for kind, command in {"containers": ["docker", "ps", "-aq"], "networks": ["docker", "network", "ls", "-q"], "volumes": ["docker", "volume", "ls", "-q"]}.items():
            result = await asyncio.to_thread(subprocess.run, [*command, "--filter", "label=com.docker.compose.project=" + config.compose_project], capture_output=True, text=True, timeout=20)
            report["cleanup"][kind] = result.returncode == 0 and not result.stdout.strip()
        report["cleanup"]["ports_closed"] = True
        for port in ports:
            with socket.socket() as connection:
                connection.settimeout(1)
                report["cleanup"]["ports_closed"] &= connection.connect_ex(("127.0.0.1", port)) != 0
        if not all(report["cleanup"].values()):
            report["result"] = "FAIL"
        path = root / "oauth-report.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
        path.chmod(0o600)
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
    print(json.dumps({"result": report["result"], "stage": report["stage"], "report": str(path)}))
    return 130 if report.get("cancelled") else 0 if report["result"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    return asyncio.run(exercise(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
