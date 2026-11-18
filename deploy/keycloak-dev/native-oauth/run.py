#!/usr/bin/env python3
"""Exercise the packed CLI and its noninteractive stdio process with real Keycloak."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
import traceback

import httpx
import jwt
import yaml

from fixture import NativeOAuthFixture, SCOPES, free_port
from file_flow import exercise_files
from relay import AuthorityRelay, ResourceRelay

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts/ci"))
from e2e_runtime import E2ERuntime, PROTOCOL_REVISION, RuntimeConfig  # noqa: E402


class NativeOAuthRuntime(E2ERuntime):
    def __init__(self, config: RuntimeConfig, authority: NativeOAuthFixture):
        super().__init__(config)
        self.authority = authority
        self.package_artifact = {}

    async def _spawn_host_process(self, name, command, log_name, *, environment=None, cwd=None):
        if name == "backend":
            command = [*command, "--app", "quiet_backend:app"]
            environment = {
                **(environment or {}),
                "PYTHONPATH": str(Path(__file__).parent),
            }
        return await super()._spawn_host_process(name, command, log_name, environment=environment, cwd=cwd)

    def _write_config(self) -> None:
        super()._write_config()
        path = self.config.config_dir / "app.yaml"
        config = yaml.safe_load(path.read_text())
        config.update({
            "public_base_url": self.authority.resource.removesuffix("/mcp"),
            "keycloak_enabled": True, "mcp_oauth_enabled": True,
            "keycloak_server_url": self.authority.origin, "keycloak_internal_url": self.authority.origin,
            "keycloak_realm": self.authority.realm, "keycloak_enrollment_mode": "open",
            "keycloak_require_verified_email": True,
        })
        path.write_text(yaml.safe_dump(config))

    async def _install_stdio_proxy(self) -> Path:
        consumer = self.config.proxy_consumer_dir
        consumer.mkdir(mode=0o700)
        package_dir = self.config.proxy_package_dir
        environment = {}
        for kind in ("user", "global"):
            settings = consumer / f"{kind}.npmrc"
            settings.touch(mode=0o600)
            environment[f"NPM_CONFIG_{kind.upper()}CONFIG"] = str(settings)
        await self._run_logged_command(
            ["npm", "pack", "--ignore-scripts", "--pack-destination", str(consumer), str(package_dir)],
            log_path=self.config.logs_dir / "stdio-pack.log", environment=environment,
        )
        archive, = consumer.glob("*.tgz")
        await self._run_logged_command(
            ["npm", "install", "--prefix", str(consumer), "--omit=dev", "--ignore-scripts",
             "--no-audit", "--no-fund", str(archive)],
            log_path=self.config.logs_dir / "stdio-install.log", environment=environment,
        )
        manifest = json.loads((consumer / "node_modules/akb-mcp/package.json").read_text())
        assert "@modelcontextprotocol/client" in manifest.get("dependencies", {}), "packed SDK must be a production dependency"
        assert (consumer / "node_modules/@modelcontextprotocol/client/package.json").is_file(), "packed SDK installed"
        self._proxy_version = manifest["version"]
        self.package_artifact = {
            "path": str(archive), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "version": self._proxy_version, "production_install": True,
        }
        return consumer / "node_modules/.bin/akb-mcp"


class StdioSession:
    def __init__(self, runtime: NativeOAuthRuntime, name: str, process):
        self.runtime = runtime
        self.name = name
        self.process = process
        self.next_id = 0

    @classmethod
    async def start(cls, runtime, name, cli, environment):
        process = await runtime._spawn_interactive_process(name, cli, f"{name}.log", environment=environment)
        return cls(runtime, name, process)

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.next_id += 1
        await self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        async with asyncio.timeout(20):
            while line := await self.process.stdout.readline():
                response = json.loads(line)
                if response.get("id") == self.next_id:
                    return response
        raise RuntimeError(f"{self.name} closed before responding to {method}")

    async def initialize(self):
        response = await self.request("initialize", {
            "protocolVersion": PROTOCOL_REVISION, "capabilities": {},
            "clientInfo": {"name": "packed-oauth-fixture", "version": "1"},
        })
        assert response.get("result", {}).get("protocolVersion"), "stdio handshake"
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        catalog = await self.request("tools/list", {})
        return {tool["name"] for tool in catalog["result"]["tools"]}

    async def call(self, name, arguments=None, *, error=False):
        params = {"name": name, "arguments": arguments or {}}
        response = await self.request("tools/call", params)
        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        if payload.get("code") == "vault_skill_required":
            guide = payload["vault_skill"]
            assert guide["body"] and guide.get("truncated") is False, "fixture reads complete Vault write guide"
            params["arguments"] = {**params["arguments"], "_vault_skill_ack": guide["ack_token"]}
            result = (await self.request("tools/call", params))["result"]
            payload = json.loads(result["content"][0]["text"])
        if bool(result.get("isError", False)) is not error:
            code = payload.get("code")
            diagnostic = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,60}", code) else "unknown"
            raise RuntimeError(f"{self.name} {name} unexpected tool outcome ({diagnostic})")
        return payload

    async def close(self):
        await self.runtime._stop_named_process(self.name)


def credentials(store: Path, resource: str) -> dict:
    key = hashlib.sha256(resource.encode()).hexdigest()
    return json.loads((store / f"{key}.json").read_text())


def access_digest(grant: dict) -> str:
    return hashlib.sha256(grant["accessToken"].encode()).hexdigest()


def assert_read(result: dict):
    assert isinstance(result.get("vaults"), list), "ordinary AKB read returned a Vault list"
    assert result["returned"] == len(result["vaults"]) <= result["total"]


class CliAcceptance:
    def __init__(self, runtime, authority, resource_relay, issuer_relay, root):
        self.runtime = runtime
        self.authority = authority
        self.resource = authority.resource
        self.resource_relay = resource_relay
        self.issuer_relay = issuer_relay
        self.root = root
        self.store = root / "oauth-state"
        self.capture = root / "authorization-url.private"
        opener = root / "opener"
        opener.mkdir(mode=0o700)
        for name in ("open", "xdg-open"):
            path = opener / name
            path.write_text('#!/bin/sh\numask 077\nprintf \'%s\\n\' "$1" >> "$AKB_NATIVE_OAUTH_URL_CAPTURE"\n')
            path.chmod(0o700)
        self.cli = [shutil.which("node"), str(runtime._proxy_executable)]
        self.environment = {
            "AKB_PAT": "", "AKB_MCP_URL": "", "AKB_MCP_AUTH_DIR": str(self.store),
            "AKB_NATIVE_OAUTH_URL_CAPTURE": str(self.capture),
            "PATH": str(opener) + os.pathsep + os.environ["PATH"],
        }

    async def status_or_logout(self, command: str, name: str | None = None) -> str:
        process = await StdioSession.start(
            self.runtime, name or command, [*self.cli, command, "--url", self.resource], self.environment,
        )
        try:
            async with asyncio.timeout(10):
                output, _ = await process.process.process.communicate()
            assert process.process.process.returncode == 0 and output.strip(), f"CLI {command} command succeeded"
            output = output.decode()
            assert self.resource in output, f"{command} reports the requested installation"
            return output
        finally:
            await process.close()

    async def cold_start(self, name="cold-start"):
        start = time.monotonic()
        before = (len(self.resource_relay.events), len(self.issuer_relay.events))
        browser_launches = len(self.capture.read_text().splitlines()) if self.capture.exists() else 0
        process = await self.stdio(name)
        try:
            async with asyncio.timeout(7):
                assert await process.process.process.wait() != 0, "cold OAuth stdio rejects missing grant"
                assert not await process.process.stdout.read(), "cold stdio keeps JSON-RPC output empty"
            stderr = (self.runtime.config.logs_dir / f"{name}.log").read_text()
            assert f"login --url {self.resource}" in stderr, "cold startup gives the explicit login command"
            assert before == (len(self.resource_relay.events), len(self.issuer_relay.events)), "cold startup never contacts AKB or Keycloak"
            launches_now = len(self.capture.read_text().splitlines()) if self.capture.exists() else 0
            assert launches_now == browser_launches, "cold startup never opens a browser"
            return round(time.monotonic() - start, 3)
        finally:
            await process.close()

    async def login(self, index: int, pause: float = 0) -> str:
        login = [*self.cli, "login", "--url", self.resource]
        first = await StdioSession.start(self.runtime, f"login-{index}", login, self.environment)
        before_resource = len(self.resource_relay.events)
        try:
            async with asyncio.timeout(20):
                while not self.capture.exists():
                    if first.process.process.returncode is not None:
                        raise RuntimeError("explicit login exited before launching a browser")
                    await asyncio.sleep(0.05)
            registration = next(event for event in self.issuer_relay.events if event["kind"] == "dcr")
            client_id = registration["client_id"]
            if index == 1:
                contender = await StdioSession.start(self.runtime, "login-contender", login, self.environment)
                try:
                    async with asyncio.timeout(7):
                        assert await contender.process.process.wait() != 0, "only one login owns the interaction"
                    assert len(self.capture.read_text().splitlines()) == 1, "competing login did not open a browser"
                    await self.cold_start("cold-during-login")
                finally:
                    await contender.close()
                await asyncio.sleep(pause)
            assert not any(event.get("rpc_method") == "initialize"
                           for event in self.resource_relay.events[before_resource:]), "explicit login initiated no MCP session"
            args = [
                self.cli[0], str(Path(__file__).with_name("browser-login.mjs")),
                str(self.capture), str(self.root / f"browser-{index}.json"),
                str(self.runtime.config.checkout / "frontend/package.json"),
            ]
            async with asyncio.timeout(75):
                await self.runtime._run_logged_command(
                    args, log_path=self.root / f"browser-{index}.log", environment={
                        "AKB_NATIVE_OAUTH_USERNAME": self.authority.username,
                        "AKB_NATIVE_OAUTH_PASSWORD": self.authority.password,
                        "AKB_NATIVE_OAUTH_ISSUER": self.authority.issuer,
                        "AKB_NATIVE_OAUTH_CLIENT_ID": client_id,
                        "AKB_NATIVE_OAUTH_RESOURCE": self.resource,
                    },
                )
            async with asyncio.timeout(10):
                output, _ = await first.process.process.communicate()
            assert first.process.process.returncode == 0 and output.strip(), "packed login returned successfully after PKCE"
            assert len(self.capture.read_text().splitlines()) == 1, "login launched a single browser"
            assert not any(event.get("rpc_method") == "initialize"
                           for event in self.resource_relay.events[before_resource:]), "completed login initiated no MCP session"
            return client_id
        finally:
            await first.close()
            self.capture.unlink(missing_ok=True)
            for name in (f"login-{index}", "login-contender"):
                path = self.runtime.config.logs_dir / f"{name}.log"
                if path.is_file():
                    lines = [line for line in path.read_text().splitlines() if self.authority.issuer not in line or "?" not in line]
                    path.write_text("\n".join(lines) + "\n")

    async def stdio(self, name: str) -> StdioSession:
        command = [*self.cli, "--url", self.resource]
        return await StdioSession.start(self.runtime, name, command, self.environment)

    async def verify_grant(self, client_id: str) -> dict:
        saved = credentials(self.store, self.resource)
        assert saved["binding"]["resource"] == self.resource and saved["binding"]["issuer"] == self.authority.issuer
        assert saved["registration"]["client_id"] == client_id and not saved["registration"].get("client_secret")
        grant = saved["grant"]
        assert grant["state"] == "active"
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            keys = (await client.get(f"{self.authority.issuer}/protocol/openid-connect/certs")).json()
        key_id = jwt.get_unverified_header(grant["accessToken"])["kid"]
        key = next(key for key in jwt.PyJWKSet.from_dict(keys).keys if key.key_id == key_id)
        claims = jwt.decode(
            grant["accessToken"], key.key, algorithms=["RS256"],
            issuer=self.authority.issuer, audience=self.resource,
            options={"require": ["exp", "iat", "iss", "aud", "azp", "sub", "typ", "scope"]},
        )
        assert claims["aud"] in [self.resource, [self.resource]], "exact signed resource audience"
        assert claims["azp"] == client_id and claims["typ"] == "Bearer", "signed public client and access token"
        assert claims["akb_tool_client"] == "admitted", "signed DCR admission evidence"
        assert claims["preferred_username"] == self.authority.username and claims["email_verified"] is True, "verified human"
        assert SCOPES.issubset(claims["scope"].split()), "both signed AKB scopes"
        return grant

    async def rotate(self, first, peer, grant: dict) -> dict:
        deadline = time.monotonic() + 105
        while time.monotonic() < deadline:
            before = len(self.resource_relay.events)
            results = await asyncio.gather(first.call("akb_list_vaults"), peer.call("akb_list_vaults"))
            for result in results:
                assert_read(result)
            current = credentials(self.store, self.resource)["grant"]
            if current["state"] == "active" and current["generation"] > grant["generation"]:
                assert current["id"] == grant["id"] and current["refreshToken"] != grant["refreshToken"]
                assert current["accessToken"] != grant["accessToken"], "rotation produced a new access and refresh token"
                await asyncio.gather(first.call("akb_list_vaults"), peer.call("akb_list_vaults"))
                replacement_requests = [event for event in self.resource_relay.events[before:]
                                        if event.get("rpc_tool") == "akb_list_vaults"
                                        and event["bearer_sha256"] == access_digest(current)
                                        and event.get("status") == 200]
                assert len(replacement_requests) >= 2, "two stdio processes used the same rotated access token"
                return current
            await asyncio.sleep(1)
        raise RuntimeError("two stdio processes did not adopt a genuine rotated grant")


async def exercise(args) -> int:
    root = args.runtime_root.resolve()
    checkout = args.checkout.resolve()
    if root == checkout or root.is_relative_to(checkout) or (root.exists() and any(root.iterdir())):
        raise ValueError("runtime root must be an empty directory outside the checkout")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    ports = set()
    while len(ports) < 7:
        ports.add(free_port())
    app, embed, fixture, postgres, minio, resource, issuer = sorted(ports)
    relay = ResourceRelay(port=resource, target=f"http://127.0.0.1:{app}", prefix=args.prefix)
    authority = NativeOAuthFixture(checkout=checkout, runtime_root=root / "authority", resource=relay.base_url + "/mcp", relay_port=issuer)
    issuer_relay = AuthorityRelay(port=issuer, target=authority.service_origin, realm=authority.realm)
    config = RuntimeConfig(
        checkout=checkout, runtime_root=root / "product", mode="serve",
        compose_file=args.compose_file.resolve(), compose_project="akb-native-product-" + secrets.token_hex(6),
        profile="transport-proxy", app_port=app, embed_port=embed, fixture_port=fixture,
        postgres_port=postgres, minio_port=minio,
    )
    os.environ[config.credentials.username_env] = "local-" + secrets.token_hex(6)
    os.environ[config.credentials.password_env] = secrets.token_urlsafe(32)
    runtime = NativeOAuthRuntime(config, authority)
    report = {"stage": "authority", "result": "FAIL"}
    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()

    def request_stop():
        authority.request_stop()
        current_task.cancel()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, request_stop)
    try:
        await relay.start()
        await issuer_relay.start()
        authority_start = asyncio.create_task(asyncio.to_thread(authority.start))
        try:
            await asyncio.shield(authority_start)
        except asyncio.CancelledError:
            authority.request_stop()
            with contextlib.suppress(Exception):
                await asyncio.shield(authority_start)
            raise
        server_info = await asyncio.to_thread(authority._admin, "GET", "serverinfo")
        report["keycloak_version"] = server_info.json()["systemInfo"]["version"]
        report["stage"] = "package"
        await runtime.prepare()
        report["source_revision"] = runtime.runtime_evidence()["source_revision"]
        report["package_artifact"] = runtime.package_artifact
        await runtime._stop_named_process("stdio")
        acceptance = CliAcceptance(runtime, authority, relay, issuer_relay, root)
        report["stage"] = "cold_start"
        report["cold_start_seconds"] = await acceptance.cold_start()
        report["stage"] = "login"
        client_id = await acceptance.login(1, args.consent_pause_seconds)
        report["dcr"] = await asyncio.to_thread(authority.verify_registration, client_id)
        assert client_id == await acceptance.login(2), "repeated login used the same DCR client"
        assert sum(event["kind"] == "dcr" for event in issuer_relay.events) == 1, "exactly one real anonymous registration"
        assert any(event["kind"] == "dcr" and event["anonymous"] and event["public_client"]
                   for event in issuer_relay.events), "DCR returned a public client without a secret"
        exchanges = [event for event in issuer_relay.events if event["kind"] == "authorization_code"]
        assert len(exchanges) == 2 and all(
            event["status"] == 200 and event["public_client"] and event["verifier_matches"] for event in exchanges
        ), "both browser callbacks exchanged with public S256"
        report["login"] = {"explicit_browser_flows": 2, "anonymous_registrations": 1, "same_registration": True,
                           "one_login_owner": True, "no_mcp_during_login": True}
        report["stage"] = "signed_grant_and_stdio"
        grant = await acceptance.verify_grant(client_id)
        status = await acceptance.status_or_logout("status")
        assert grant["accessToken"] not in status and grant["refreshToken"] not in status, "status protects secrets"
        public_status = json.loads(status)
        assert public_status["clientId"] == client_id and public_status["issuer"] == authority.issuer
        assert public_status["mode"] == "oauth" and public_status["state"] == "ready", "status reports readiness"
        assert not acceptance.capture.exists(), "status did not start authentication"
        report["signed_grant"] = {"jwks_verified": True, "audience": authority.resource, "admitted_human_client": True}
        first = await acceptance.stdio("stdio")
        peer = await acceptance.stdio("peer")
        first_catalog, peer_catalog = await asyncio.gather(first.initialize(), peer.initialize())
        assert first_catalog == peer_catalog
        assert {"akb_list_vaults", "akb_put_file", "akb_update_file", "akb_discard_image"}.issubset(first_catalog)
        assert_read(await first.call("akb_list_vaults"))
        report["stdio"] = {"catalog_count": len(first_catalog), "business_read": True}
        report["stage"] = "rotation"
        rotated = await acceptance.rotate(first, peer, grant)
        refreshes = [event for event in issuer_relay.events if event["kind"] == "refresh_token"]
        assert len(refreshes) == 1 and refreshes[0]["status"] == 200, "one real refresh for both processes"
        assert sum(event["kind"] == "dcr" for event in issuer_relay.events) == 1
        report["rotation"] = {"old_generation": grant["generation"], "new_generation": rotated["generation"],
                              "refresh_requests": 1, "two_processes_adopted": True}
        if args.hosts:
            from native_hosts import exercise_hosts

            report["stage"] = "native_hosts"
            report["native_hosts"] = await exercise_hosts(acceptance)
        if args.files:
            report["stage"] = "files"
            report["files"] = await exercise_files(
                runtime, acceptance, first, lambda: credentials(acceptance.store, authority.resource)["grant"], access_digest,
            )
        report["stage"] = "restart"
        await peer.close()
        restarted = await acceptance.stdio("restarted")
        assert first_catalog == await restarted.initialize()
        assert_read(await restarted.call("akb_list_vaults"))
        await restarted.close()
        assert not acceptance.capture.exists() and sum(event["kind"] == "dcr" for event in issuer_relay.events) == 1
        report["restart_without_browser_or_dcr"] = True
        report["stage"] = "logout"
        await acceptance.status_or_logout("logout")
        assert credentials(acceptance.store, authority.resource)["grant"] is None, "logout cleared the grant"
        await acceptance.cold_start("cold-after-logout")
        before_calls = len(relay.events)
        denied_vault = "reauth-denied-" + secrets.token_hex(6)
        denied = await first.call("akb_create_vault", {"name": denied_vault}, error=True)
        assert denied["details"]["operation_outcome"] == "not_started", "running proxy reports work did not start"
        assert not any(event.get("rpc_tool") == "akb_create_vault" for event in relay.events[before_calls:])
        assert client_id == await acceptance.login(3), "logout retained the saved registration"
        after_login = await acceptance.stdio("after-login")
        await after_login.initialize()
        reread = await after_login.call("akb_list_vaults", {"filter": denied_vault})
        assert_read(reread)
        assert all(vault["name"] != denied_vault for vault in reread["vaults"]), "later login did not execute rejected work"
        await after_login.close()
        assert not any(event.get("rpc_tool") == "akb_create_vault" for event in relay.events[before_calls:])
        await acceptance.status_or_logout("logout", "final-logout")
        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            rejected = await client.post(f"{authority.issuer}/protocol/openid-connect/token", data={
                "grant_type": "refresh_token", "client_id": client_id, "refresh_token": grant["refreshToken"],
            })
        assert rejected.status_code == 400 and rejected.json().get("error") == "invalid_grant", "Keycloak rejected consumed refresh token"
        report["logout"] = {"shared_grant_cleared": True, "peer_execution": "not_started", "later_login_did_not_replay": True}
        report["old_refresh_rejected"] = True
        report["result"] = "PASS"
        report["stage"] = "complete"
    except asyncio.CancelledError:
        report["cancelled"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["error_frames"] = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                                  for frame in traceback.extract_tb(error.__traceback__)]
        if isinstance(error, (AssertionError, RuntimeError)):
            report["error"] = str(error)
    finally:
        for label, cleanup in (
            ("product", runtime.cleanup),
            ("resource_relay", relay.close),
            ("issuer_relay", issuer_relay.close),
            ("authority", lambda: asyncio.to_thread(authority.close)),
        ):
            try:
                await cleanup()
                report[f"{label}_cleanup_completed"] = True
            except Exception as error:
                report[f"{label}_cleanup_error"] = type(error).__name__
                report["result"] = "FAIL"
        report["authority"] = authority.receipt()
        report["cleanup"] = await verify_cleanup(config.compose_project, ports)
        if not all(report["cleanup"].values()):
            report["result"] = "FAIL"
        write_report(root / "resource-observations.json", relay.events)
        write_report(root / "issuer-observations.json", issuer_relay.events)
        path = root / "oauth-report.json"
        write_report(path, report)
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
    print(json.dumps({"result": report["result"], "stage": report["stage"], "report": str(path)}))
    return 130 if report.get("cancelled") else 0 if report["result"] == "PASS" else 1


def write_report(path: Path, data: dict | list):
    path.touch(mode=0o600)
    path.write_text(json.dumps(data, indent=2) + "\n")


async def verify_cleanup(project: str, ports: set[int]) -> dict[str, bool]:
    cleanup = {}
    for kind, command in {"containers": ["docker", "ps", "-aq"], "networks": ["docker", "network", "ls", "-q"],
                          "volumes": ["docker", "volume", "ls", "-q"]}.items():
        result = await asyncio.to_thread(subprocess.run, [*command, "--filter", f"label=com.docker.compose.project={project}"],
                                         capture_output=True, text=True, timeout=20)
        cleanup[kind] = result.returncode == 0 and not result.stdout.strip()
    cleanup["ports_closed"] = True
    for port in ports:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1)
        except OSError:
            continue
        writer.close()
        await writer.wait_closed()
        cleanup["ports_closed"] = False
    return cleanup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--files", action="store_true", help="reuse existing file suites and observe OAuth controls")
    parser.add_argument("--hosts", action="store_true", help="invoke the real Codex and Claude Code CLIs in private test homes")
    parser.add_argument("--prefix", default="", help="loopback installation prefix")
    parser.add_argument("--consent-pause-seconds", type=float, default=0, help="delay before driving the real browser")
    return asyncio.run(exercise(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
