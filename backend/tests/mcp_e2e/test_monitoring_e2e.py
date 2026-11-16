"""Compare the protected dashboard with real direct HTTP and stdio tool completions."""

import asyncio
from datetime import UTC, datetime
import json
import os
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

        refusal = await call(b["token"], "akb_browse", {"vault": "ungranted-monitoring-fixture"})
        assert refusal["isError"]
        refusal_code = json.loads(refusal["content"][0]["text"])["code"]
        assert (await http.get(report_path)).status_code == 401
        assert (await http.get(report_path, headers={"Authorization": "Bearer " + a["token"]})).status_code == 401
        window = {"from": start, "to": datetime.now(UTC).isoformat()}

        async def report(**filters):
            response = await http.get(report_path, params={**window, **filters}, headers=admin_headers)
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
        errors = await report(user_id=actor, history_errors_only="true", tool_sort="errors")
        [failed] = errors["recent_calls"]
        assert failed["error_code"] == refusal_code and failed["error_reason"]
        assert errors["tools"][0]["tool"] == "akb_browse"
        assert "ungranted-monitoring-fixture" not in json.dumps(errors)
        credentials = (await report(user_id=actor))["credentials"]
        assert {r["pat_id"]: r["calls"] for r in credentials if r["pat_id"]} == {a["token_id"]: 2, b["token_id"]: 1}
        assert all(r["credential_status"] == "active" and r["last_observed_at"] for r in credentials if r["pat_id"])
        if sso_calls:
            assert next(r["credential_status"] for r in credentials if r["auth_kind"] == "oauth") == "session_unverified"
        assert {r["tool"]: r["calls"] for r in result["tools"]} == {
            "akb_help": 2,
            "akb_browse": 1,
            **({"akb_whoami": 1} if sso_calls else {}),
        }
        filters = {"user_id": actor, "auth_kind": "pat", "pat_id": a["token_id"], "tool": "akb_help"}
        detail = await report(**filters)
        assert detail["summary"]["calls"] == len(detail["recent_calls"]) == 2
        assert len({c["observation_id"] for c in detail["recent_calls"]}) == 2
        assert all(c["pat_id"] == a["token_id"] and c["tool"] == "akb_help" for c in detail["recent_calls"])
        assert (await report(**{**filters, "tool": "akb_browse"}))["summary"]["calls"] == 0
        for params, status in [({"pat_id": b["token_id"]}, 422), ({**filters, "user_id": str(uuid4())}, 404)]:
            assert (await http.get(report_path, params=params, headers=admin_headers)).status_code == status
        (
            await http.delete(f"/api/v1/admin/users/{actor}/tokens/{b['token_id']}", headers=admin_headers)
        ).raise_for_status()
        revoked = await report(user_id=actor, auth_kind="pat", pat_id=b["token_id"])
        assert revoked["summary"]["calls"] == 1 and revoked["credentials"][0]["label"] is None
        assert revoked["credentials"][0]["credential_status"] == revoked["recent_calls"][0]["credential_status"] == "deleted"
