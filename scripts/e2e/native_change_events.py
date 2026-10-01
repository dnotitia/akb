"""Explicit live acceptance: REST Native documents -> real Redis events.

Requires AKB_URL, AKB_PAT (can create/delete a disposable Vault), and
AKB_REDIS_URL (operator stream access). Prints safe evidence only. Redis is
read-only; cleanup removes only the Vault created by this invocation.
"""
import asyncio
import json
import os
import time
import uuid

import httpx
import redis.asyncio as redis


async def main():
    origin = os.environ["AKB_URL"].rstrip("/")
    token = os.environ["AKB_PAT"]
    client = redis.from_url(os.environ["AKB_REDIS_URL"], decode_responses=True)
    stream = os.environ.get("AKB_REDIS_STREAM", "akb:events")
    vault = "event-acceptance-" + uuid.uuid4().hex[:12]
    receipt = {"checks": {}, "events": []}
    created_vault = False
    async with httpx.AsyncClient(
        base_url=origin + "/api/v1/", headers={"Authorization": "Bearer " + token},
        timeout=30, follow_redirects=False,
    ) as api:
        async def call(method, path, body=None, expected=200):
            response = await api.request(method, path, json=body)
            if response.status_code != expected:
                raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
            return response.json()
        try:
            response = await call("POST", "vaults?name=" + vault)
            created_vault = True
            vault_id = response["vault_id"]
            tail = await client.xrevrange(stream, count=1)
            cursor = tail[0][0] if tail else "0-0"
            created = await call("POST", "documents", {
                "vault": vault, "collection": "source", "title": "Event acceptance",
                "content": "Disposable first body", "status": "active",
            })
            path = created["path"]
            await call("PATCH", f"documents/{vault}/{path}", {
                "content": "Disposable changed body", "status": "archived",
                "expected_commit": created["current_commit"],
            })
            await call("PATCH", f"documents/{vault}/{path}", {
                "content": "Must not land", "expected_commit": created["current_commit"],
            }, expected=409)
            receipt["checks"]["obsolete_head_rejected"] = True
            moved = await call("POST", f"documents/{vault}/{path}/move", {
                "collection": "target/nested", "slug": "final",
            })
            await call("PATCH", f"documents/{vault}/{moved['path']}", {"status": "active"})
            current = await call("GET", f"documents/{vault}/{moved['path']}")
            assert current["content"] == "Disposable changed body" and current["status"] == "active"
            receipt["checks"]["current_akb_content"] = True
            await call("DELETE", f"documents/{vault}/{moved['path']}")
            await call("GET", f"documents/{vault}/{moved['path']}", expected=404)
            receipt["checks"]["deleted_document_not_found"] = True
            deadline = time.monotonic() + 90
            seen = {}
            while time.monotonic() < deadline and len(seen) < 5:
                for _, entries in await client.xread({stream: cursor}, count=200, block=1000):
                    for transport_id, event in entries:
                        cursor = transport_id
                        if event.get("vault_id") != vault_id:
                            continue
                        payload = json.loads(event.get("payload", "{}"))
                        if payload.get("path") not in {path, moved["path"]}:
                            continue
                        seen[event["id"]] = {"id": event["id"], "kind": event["kind"],
                            "resource_uri": event["resource_uri"], "payload": payload}
            rows = sorted(seen.values(), key=lambda event: int(event["id"]))
            assert [event["kind"] for event in rows] == [
                "document.put", "document.update", "document.move", "document.update", "document.delete",
            ], "missing or unexpected committed document events"
            assert len({event["payload"]["resource_id"] for event in rows}) == 1
            assert rows[2]["payload"]["old_uri"] == created["uri"]
            assert rows[2]["payload"]["old_collection"] == "source"
            assert rows[2]["payload"]["collection"] == "target/nested"
            assert rows[-1]["resource_uri"] == moved["uri"]
            assert rows[-1]["payload"]["resource_id"] == rows[0]["payload"]["resource_id"]
            receipt["checks"].update(redis_delivery=True, stable_identity=True, both_move_scopes=True)
            receipt["events"] = rows
        finally:
            if created_vault:
                await call("DELETE", "vaults/" + vault)
                receipt["checks"]["fixture_removed"] = True
            await client.aclose()
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
