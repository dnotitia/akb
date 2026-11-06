"""Reuse real file workflows and observe the shared OAuth control credential."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3

import httpx


CONTROLS = [
    ("reserve_upload", "POST", r"/api/v1/files/[^/]+/upload"),
    ("confirm_upload", "POST", r"/api/v1/files/[^/]+/[^/]+/confirm"),
    ("reserve_replacement", "POST", r"/api/v1/files/[^/]+/[^/]+/replace"),
    ("confirm_replacement", "POST", r"/api/v1/files/[^/]+/[^/]+/replace/[^/]+/confirm"),
    ("download_capability", "GET", r"/api/v1/files/[^/]+/[^/]+/download"),
    ("delete_file", "DELETE", r"/api/v1/files/[^/]+/[^/]+"),
    ("upload_image", "POST", r"/api/v1/assets/[^/]+"),
    ("discard_image", "DELETE", r"/api/v1/assets/[^/]+/[^/]+"),
]


async def exercise_files(runtime, authority, relay, store, root, tool_call):
    source_root = Path(__file__).resolve().parents[3]
    start = len(relay.events)
    store_record = store / (hashlib.sha256(authority.resource.encode()).hexdigest() + ".json")
    known_generations: dict[str, int] = {}
    sampler_stop = asyncio.Event()

    async def sample_generations():
        database = sqlite3.connect(store_record.with_suffix(".sqlite").as_uri() + "?mode=ro", uri=True, timeout=0, isolation_level=None)
        try:
            while not sampler_stop.is_set():
                record = json.loads(store_record.read_text())
                try:
                    row = database.execute("SELECT publication_id FROM publication WHERE id=1").fetchone()
                except sqlite3.OperationalError as error:
                    if error.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                        raise
                    row = None
                token = record.get("tokens", {}).get("access_token")
                if token and row and row[0] == record.get("publicationId"):
                    known_generations[hashlib.sha256(token.encode()).hexdigest()] = record["generation"]
                await asyncio.sleep(0.1)
        finally:
            database.close()

    sampler = asyncio.create_task(sample_generations(), name="observe-published-generations")
    report = {"suites": [], "prefix": relay.prefix}
    openings = len((root / "authorization-url.private").read_text().splitlines())
    try:
        await asyncio.sleep(0)
        await tool_call("akb_list_vaults")
        temporary = root / "file-suite-temporary"
        temporary.mkdir(mode=0o700)
        for index, name in enumerate(["test_stdio_files_e2e.sh", "test_put_file_param_e2e.sh"]):
            command = ["bash", str(source_root / "backend/tests" / name)]
            log = root / f"oauth-file-suite-{index}.log"
            async with asyncio.timeout(180):
                code = await runtime._run_logged_command(command, log_path=log, check=False, environment={
                    "AKB_URL": relay.base_url, "AKB_E2E_AUTH_MODE": "oauth", "AKB_PAT": "",
                    "AKB_MCP_BIN": str(runtime._proxy_executable), "AKB_MCP_AUTH_DIR": str(store),
                    "AKB_NATIVE_OAUTH_URL_CAPTURE": str(root / "authorization-url.private"),
                    "PATH": str(root / "opener") + os.pathsep + os.environ["PATH"], "TMPDIR": str(temporary),
                })
            matches = re.findall(r"Results:\s*(\d+) passed,\s*(\d+) failed", log.read_text())
            report["suites"].append({"command": command, "exit": code, "summary": matches[-1] if matches else None, "output": str(log)})
            if code or not matches or int(matches[-1][0]) == 0 or int(matches[-1][1]) != 0:
                raise RuntimeError("existing OAuth file workflow failed")
        vault = "native-images-" + secrets.token_hex(6)
        await tool_call("akb_create_vault", {"name": vault})
        image_path = source_root / "eval/mcp-catalog/fixtures/sample-image.png"
        image_bytes = image_path.read_bytes()
        try:
            image = await tool_call("akb_put_image", {"vault": vault, "file_path": str(image_path)})
            assert image["mime_type"] == "image/png" and image["width"] == image["height"] == 400
            assert image["size_bytes"] == len(image_bytes)
            discarded = await tool_call("akb_discard_image", {"vault": vault, "url": image["url"]})
            assert discarded["discarded"] is True
            image = await tool_call("akb_put_image", {"vault": vault, "file_path": str(image_path)})
            await tool_call("akb_put", {"vault": vault, "title": "Image bytes", "slug": "image-bytes", "content": "![Fixture image](" + image["url"] + ")"})
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20) as client:
                response = await client.get(relay.base_url + image["url"], params={"vault": vault}, headers={"Authorization": "Bearer " + runtime._pat_value})
                assert response.status_code == 200 and response.headers["content-type"].startswith("image/png"), "committed image byte response"
                assert hashlib.sha256(response.content).digest() == hashlib.sha256(image_bytes).digest(), "committed image byte hash"
            report["image"] = {"bytes_match": True, "sha256": hashlib.sha256(image_bytes).hexdigest(), "mime": "image/png", "dimensions": [400, 400], "discarded": True, "byte_oracle_credential": "existing runtime PAT, independently from the OAuth proxy"}
        finally:
            await tool_call("akb_delete_vault", {"vault": vault})
        assert len((root / "authorization-url.private").read_text().splitlines()) == openings
        report["peer_proxies_reused_grant_without_browser"] = True
    finally:
        sampler_stop.set()
        sampler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sampler
    events = relay.events[start:]
    controls = {}
    for operation, method, pattern in CONTROLS:
        observed = [event for event in events if event["method"] == method and re.fullmatch(pattern, event["inner_path"]) and 200 <= event.get("status", 0) < 300]
        assert observed, "missing successful real control: " + operation
        assert all(event["authorization_present"] and event["bearer_sha256"] in known_generations for event in observed)
        assert all(event["path"].startswith(relay.prefix + "/api/v1/") for event in observed)
        controls[operation] = {"requests": len(observed), "generations": sorted({known_generations[event["bearer_sha256"]] for event in observed})}
    mcp = [event for event in events if event["inner_path"] == "/mcp/" and event.get("status") == 200]
    assert mcp and all(event["bearer_sha256"] in known_generations for event in mcp)
    capabilities = [event for event in events if event["inner_path"].endswith("/[capability]") and event.get("status") == 200]
    assert capabilities and all(not event["authorization_present"] for event in capabilities)
    uploaded = {event["request_sha256"] for event in capabilities if event["method"] == "PUT"}
    downloaded = {event["response_sha256"] for event in capabilities if event["method"] == "GET"}
    assert uploaded and downloaded and downloaded.issubset(uploaded)
    report.update({"controls": controls, "published_generations": sorted(set(known_generations.values())), "mcp_uses_published_bearer": True, "capability_requests": len(capabilities), "capabilities_without_bearer": True, "downloaded_bytes_match_uploaded": True})
    metadata_path = "/.well-known/oauth-protected-resource" + relay.prefix + "/mcp"
    assert any(event["path"] == metadata_path and event.get("status") == 200 and not event["authorization_present"] for event in relay.events)
    report["origin_root_path_aware_metadata"] = metadata_path
    return report
