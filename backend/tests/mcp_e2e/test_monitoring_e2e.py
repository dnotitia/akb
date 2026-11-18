"""Compare the protected dashboard with real direct HTTP and stdio tool completions."""

import asyncio
from datetime import UTC, datetime
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urljoin
from uuid import uuid4

import httpx
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from .runtime import RuntimeContext


async def test_mcp_monitoring_by_actor_pat_and_tool(runtime_session: RuntimeContext, tmp_path: Path) -> None:
    descriptor = runtime_session.descriptor
    report_path = "/api/v1/admin/mcp-monitoring"
    async with httpx.AsyncClient(base_url=descriptor.app_origin, timeout=15, trust_env=False) as http:
        admin = await http.post(
            "/api/v1/admin/auth/local/login",
            json={
                "username": os.environ[descriptor.username_env],
                "password": os.environ[descriptor.password_env],
            },
        )
        admin.raise_for_status()
        admin_headers = {"Authorization": "Bearer " + admin.json()["token"]}

        async def call(token, tool, arguments):
            response = await http.post(
                "/mcp/",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
            response.raise_for_status()
            return response.json()["result"]

        start = datetime.now(UTC).isoformat()
        # transport-oidc projects the signed fixture identity through the real
        # JWKS verifier. The tool-only canonical gate exercises the same PAT path.
        discovery = (await http.get(descriptor.discovery_url)).raise_for_status().json()
        sso_calls = int("oidc" in discovery)
        actor = admin.json()["user"]["id"]
        if sso_calls:
            token = (
                (await http.post(urljoin(descriptor.discovery_url, "/oidc/token"), json={"variant": "valid"}))
                .raise_for_status()
                .json()["access_token"]
            )
            who = await call(token, "akb_whoami", {})
            actor = json.loads(who["content"][0]["text"])["user_id"]

        async def mint():
            response = await http.post(
                f"/api/v1/admin/users/{actor}/tokens", headers=admin_headers, json={"name": "Device"}
            )
            return response.raise_for_status().json()

        a, b = await mint(), await mint()
        monitor = "monitor-" + uuid4().hex[:12]
        password = "M1!" + secrets.token_urlsafe(24)
        login = {"username": monitor, "password": password}
        account = (await http.post(
            "/api/v1/auth/register", json={**login, "email": monitor + "@example.org"},
        )).raise_for_status().json()["user_id"]
        access = f"{report_path}/access/{account}"
        (await http.put(access, json={"enabled": True}, headers=admin_headers)).raise_for_status()
        assert (await http.post("/api/v1/auth/login", json=login)).status_code == 401
        assert (await http.get(report_path, headers=admin_headers)).status_code == 401
        assert (await http.get(report_path)).status_code == 401
        assert (await http.get(report_path, headers={"Authorization": "Bearer " + a["token"]})).status_code == 401
        (await http.post(f"{report_path}/auth/local/login", json=login)).raise_for_status()
        monitoring_token = http.cookies["akb_dev_monitor_session"]
        monitor_headers = {"Authorization": "Bearer " + monitoring_token}
        assert (await http.get(report_path, headers={"Authorization": "Invalid"})).status_code == 401
        assert (await http.get(report_path, headers=monitor_headers)).status_code == 200
        token_path = f"/api/v1/admin/users/{actor}/tokens"
        denied = [
            ("GET", "/api/v1/my/vaults", None),
            ("GET", "/api/v1/admin/auth/session", None),
            ("POST", token_path, {"name": "refused-fixture"}),
            ("POST", "/api/v1/auth/tokens", {"name": "refused-fixture"}),
            ("PUT", f"/api/v1/admin/users/{account}/role", {"is_admin": True}),
            ("POST", f"/api/v1/admin/users/{actor}/reset-password", {}),
            ("PUT", access, {"enabled": False}),
            ("POST", "/mcp/", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        ]
        for method, path, body in denied:
            response = await http.request(method, path, json=body, headers=monitor_headers)
            assert response.status_code in (401, 403), (method, path, response.status_code)
        assert (await http.get("/api/v1/my/vaults")).status_code == 401
        assert (await http.post(token_path, json={"name": "denied"}, headers={"Authorization": "Bearer " + a["token"]})).status_code == 403
        assert not (await call(a["token"], "akb_help", {})).get("isError", False)
        cli = Path(__file__).resolve().parents[3] / "packages/akb-mcp-client/bin/akb-mcp.mjs"
        proxy = StdioServerParameters(
            command="node",
            args=[str(cli)],
            env={
                "PATH": os.environ["PATH"],
                "AKB_MCP_URL": descriptor.mcp_url,
                "AKB_PAT": a["token"],
            },
        )
        async with Client(stdio_client(proxy), mode="auto", cache=None, read_timeout_seconds=30) as client:
            await client.list_tools()
            assert not (await client.call_tool("akb_help", {})).is_error
            assert (await client.call_tool("akb_put", {"file": str(tmp_path / "missing")})).is_error

        assert (await call(b["token"], "akb_browse", {"vault": "ungranted-monitoring-fixture"}))["isError"]
        window = {"from": start, "to": datetime.now(UTC).isoformat()}

        async def report(**filters):
            response = await http.get(report_path, params={**window, **filters})
            assert response.headers["Cache-Control"] == "no-store"
            return response.raise_for_status().json()

        total = 3 + sso_calls
        for _ in range(40):
            result = await report()
            if result["summary"]["calls"] >= total:
                break
            await asyncio.sleep(0.25)
        assert len(result["users"]) == 1
        user = result["users"][0]
        assert user["user_id"] == actor
        assert [user[k] for k in ("calls", "sso_calls", "pat_calls", "errors")] == [total, sso_calls, 3, 1]
        assert sum(i["calls"] or 0 for i in result["series"]) == total
        credentials = (await report(user_id=actor))["credentials"]
        pat_rows = [r for r in credentials if r["auth_kind"] == "pat"]
        assert [r["calls"] for r in pat_rows] == [2, 1]
        assert [r["errors"] for r in pat_rows] == [0, 1]
        assert all(r["p95_ms"] is not None and r["credential_status"] == "active" and r["last_observed_at"] for r in pat_rows)
        assert len({r["pat_ref"] for r in pat_rows}) == len({r["label"] for r in pat_rows}) == 2
        assert all(r["label"].startswith("PAT ") for r in pat_rows)
        snapshot = json.dumps([credentials, result])
        for value in ("Device", a["token_id"], b["token_id"], a["token"], b["token"], "pat_id", "token_hash", "token_prefix"):
            assert value not in snapshot
        if sso_calls:
            assert next(r["credential_status"] for r in credentials if r["auth_kind"] == "oauth") == "session_unverified"
        assert {r["tool"]: r["calls"] for r in result["tools"]} == {
            "akb_help": 2,
            "akb_browse": 1,
            **({"akb_whoami": 1} if sso_calls else {}),
        }
        filters = {"user_id": actor, "auth_kind": "pat", "pat_ref": pat_rows[0]["pat_ref"], "tool": "akb_help"}
        detail = await report(**filters)
        assert detail["summary"]["calls"] == len(detail["recent_calls"]) == 2
        assert len({c["observation_id"] for c in detail["recent_calls"]}) == 2
        assert all(c["pat_ref"] == pat_rows[0]["pat_ref"] and c["tool"] == "akb_help" for c in detail["recent_calls"])
        assert (await report(**{**filters, "tool": "akb_browse"}))["summary"]["calls"] == 0
        for params, status in [
            ({"pat_ref": pat_rows[1]["pat_ref"]}, 422),
            ({**filters, "user_id": str(uuid4())}, 404),
            ({**filters, "pat_ref": a["token_id"]}, 404),
        ]:
            assert (await http.get(report_path, params=params)).status_code == status
        (
            await http.delete(f"/api/v1/admin/users/{actor}/tokens/{b['token_id']}", headers=admin_headers)
        ).raise_for_status()
        revoked = await report(user_id=actor, auth_kind="pat", pat_ref=pat_rows[1]["pat_ref"])
        assert revoked["summary"]["calls"] == 1 and revoked["credentials"][0]["label"] == pat_rows[1]["label"]
        assert revoked["credentials"][0]["credential_status"] == revoked["recent_calls"][0]["credential_status"] == "deleted"

        (await http.put(access, json={"enabled": False}, headers=admin_headers)).raise_for_status()
        assert (await http.get(report_path)).status_code == 401
        assert (await http.get(report_path, headers=monitor_headers)).status_code == 401
        (await http.put(access, json={"enabled": True}, headers=admin_headers)).raise_for_status()
        assert (await http.get(report_path, headers=monitor_headers)).status_code == 401
        assert (await http.post("/api/v1/auth/login", json=login)).status_code == 401
