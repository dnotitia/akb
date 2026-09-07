"""Opt-in real HTTP/DB regression; use an isolated local E2E runtime only.

AKB_SEARCH_TEST_URL=http://127.0.0.1:18080 uv run pytest tests/test_search_filters_http.py
The runtime must enable local registration and its deterministic embedding stub.
"""

import asyncio
import os
import secrets
from urllib.parse import urlparse
import uuid

import httpx
import pytest


async def test_live_login_search_filters_and_vault_isolation():
    origin = os.getenv("AKB_SEARCH_TEST_URL")
    if not origin:
        pytest.skip("requires isolated local AKB_SEARCH_TEST_URL")
    assert urlparse(origin).hostname in {"localhost", "127.0.0.1", "::1"}
    suffix = uuid.uuid4().hex[:12]
    vault = f"search-contract-{suffix}"
    password = secrets.token_urlsafe(24)
    async with httpx.AsyncClient(base_url=f"{origin}/api/v1", timeout=60) as client:
        async def call(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            assert response.is_success, (method, path, response.status_code, response.text[:1000])
            return response.json()

        async def login(name):
            await call("POST", "/auth/register", json={
                "username": name, "email": f"{name}@example.invalid", "password": password,
            })
            session = await call("POST", "/auth/login", json={"username": name, "password": password})
            return {"Authorization": f"Bearer {session['token']}"}

        owner = await login(f"search_{suffix}")
        outsider = await login(f"outside_{suffix}")
        await call("GET", "/auth/me", headers=owner)
        await call("POST", "/vaults", headers=owner, params={"name": vault})
        try:
            for collection in ["guide", "guide/child", "guide-old"]:
                await call("POST", f"/collections/{vault}", headers=owner, json={"path": collection})
            for index in range(30):
                await call("POST", "/documents", headers=owner, json={
                    "vault": vault, "collection": "guide/child" if index == 28 else "guide",
                    "title": f"Filter fixture {index}", "slug": f"fixture-{index}",
                    "content": f"DeploymentNeedle API-223 literal 100%_ item {index}",
                    "type": "report" if index >= 25 else "note",
                    "status": "archived" if index == 29 else "active",
                    "tags": ["ops", f"item-{index}"],
                })
            await call("POST", "/documents", headers=owner, json={
                "vault": vault, "collection": "guide-old", "title": "Sibling excluded",
                "content": "DeploymentNeedle", "type": "report", "status": "active", "tags": ["ops"],
            })
            base = {"q": "DeploymentNeedle", "vault": vault, "collection": "guide", "include_archived": "false"}
            async def grep(**filters):
                return await call("GET", "/grep", headers=owner, params={**base, **filters})

            initial = await grep(limit=25)
            assert initial["total_docs"] == 29 and len(initial["results"]) == 25
            reports = await grep(doc_types="report", tags="ops", limit=2)
            assert reports["total_docs"] == 4 and len(reports["results"]) == 2
            assert reports["truncated"] is True
            assert (await grep(doc_types="report", include_archived="true"))["total_docs"] == 5
            assert (await grep(doc_types="report", tags="missing"))["total_docs"] == 0
            assert (await grep(q="deploymentneedle", case_sensitive="true"))["total_docs"] == 0
            assert (await grep(q="deploymentneedle", case_sensitive="false"))["total_docs"] == 29
            assert (await grep(q="API-[0-9]+", regex="true", case_sensitive="true"))["total_docs"] == 29
            assert (await grep(q="100%_"))["total_docs"] == 29
            invalid = await client.get("/grep", headers=owner, params={**base, "q": "[", "regex": "true"})
            assert invalid.status_code == 422
            hidden = await call("GET", "/grep", headers=outsider, params=base)
            assert hidden["total_docs"] == 0

            # Wait for asynchronous indexing, then identify a result outside the first 25.
            for _ in range(60):
                all_docs = await call("GET", "/search", headers=owner, params={**base, "limit": 100})
                if len(all_docs["results"]) == 29:
                    break
                await asyncio.sleep(1)
            assert len(all_docs["results"]) == 29, all_docs
            top = await call("GET", "/search", headers=owner, params={**base, "limit": 25})
            shown = {item["title"] for item in top["results"]}
            omitted = next(item for item in all_docs["results"] if item["title"] not in shown)
            index = int(omitted["title"].rsplit(" ", 1)[1])
            filtered = await call("GET", "/search", headers=owner, params={**base, "tags": f"item-{index}", "limit": 25})
            assert [item["title"] for item in filtered["results"]] == [omitted["title"]]
            typed = await call("GET", "/search", headers=owner, params={**base, "doc_types": "report", "source_type": "document"})
            assert len(typed["results"]) == 4
            assert all(item["doc_type"] == "report" for item in typed["results"])
            hidden = await call("GET", "/search", headers=outsider, params={**base, "tags": f"item-{index}"})
            assert hidden["results"] == []

            # Real file/table candidates must obey the same collection boundary.
            for number, collection in enumerate(["guide/child", "guide-old"]):
                await call("POST", f"/tables/{vault}", headers=owner, json={
                    "name": f"filter_{suffix}_{number}", "collection": collection,
                    "description": "DeploymentNeedle", "columns": [{"name": "value", "type": "text"}],
                })
                upload = await call("POST", f"/files/{vault}/upload", headers=owner, params={
                    "filename": f"fixture-{number}.txt", "collection": collection,
                    "description": "DeploymentNeedle", "mime_type": "text/plain",
                })
                # The isolated runtime exposes its own local MinIO endpoint.
                assert urlparse(upload["upload_url"]).hostname in {"localhost", "127.0.0.1"}
                uploaded = await client.put(upload["upload_url"], content=b"DeploymentNeedle", headers={"Content-Type": "text/plain"})
                assert uploaded.is_success
                file_id = upload["uri"].rsplit("/", 1)[1]
                await call("POST", f"/files/{vault}/{file_id}/confirm", headers=owner)
            for source in ["file", "table"]:
                for _ in range(60):
                    results = await call("GET", "/search", headers=owner, params={**base, "source_type": source})
                    if results["results"]:
                        break
                    await asyncio.sleep(1)
                assert len(results["results"]) == 1, results
                assert results["results"][0]["source_type"] == source
                contradiction = await call("GET", "/search", headers=owner, params={**base, "source_type": source, "tags": "ops"})
                assert contradiction["results"] == []
        finally:
            response = await client.delete(f"/vaults/{vault}", headers=owner)
            assert response.is_success, response.text
