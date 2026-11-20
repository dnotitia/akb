"""Use the public document clocks as exact boundaries through the MCP transport."""
import asyncio
from datetime import UTC, datetime, timedelta, timezone

import httpx
from mcp import Client

from .runtime import RuntimeContext
from .test_product_e2e import _call_json, _create_vault


async def test_document_search_time_windows(mcp_client: Client, runtime_session: RuntimeContext) -> None:
    catalog = await mcp_client.list_tools(cache_mode="bypass")
    schema = next(tool.input_schema for tool in catalog.tools if tool.name == "akb_search")
    assert "time_range" in schema["properties"] and "time_range" not in schema["required"]
    vault = await _create_vault(mcp_client, runtime_session, "dated-search")

    async def call(tool, args):
        return await _call_json(mcp_client, runtime_session, tool, args)

    query = "datewindow"
    uris = []
    for title in ("First", "Second", "Third"):
        doc = await call("akb_put", {"vault": vault, "title": title, "content": f"{query} {title}"})
        uris.append(doc["uri"])
    # Move the first document to the end of the update order.
    await call("akb_update", {"uri": uris[0], "tags": ["edited"]})
    docs = [await call("akb_get", {"uri": uri}) for uri in uris]
    args = {"vault": vault, "source_uris": uris}

    async def search(**options):
        return await call("akb_search", {"query": query, **args, **options})

    def hits(page):
        return {item["uri"] for item in page["results"]}

    for _ in range(90):
        baseline = await search()
        if hits(baseline) == set(uris):
            break
        await asyncio.sleep(2)
    assert hits(baseline) == set(uris)
    assert "time_range" not in baseline
    async with httpx.AsyncClient(base_url=runtime_session.descriptor.app_origin, trust_env=False,
                                 headers={"Authorization": f"Bearer {runtime_session.pat}"}) as rest:
        previous_contract = await rest.get("/api/v1/search", params={"q": query, **args})
        previous_contract.raise_for_status()
        assert baseline == previous_contract.json()

    local_zone = timezone(timedelta(hours=9))
    for field, order in (("created_at", [0, 1, 2]), ("updated_at", [1, 2, 0])):
        start, middle, end = [datetime.fromisoformat(docs[i][field]) for i in order]
        assert start < middle < end
        window = {"field": field, "gte": start.astimezone(local_zone).isoformat(), "lt": end.isoformat()}
        page = await search(source_type="document", time_range=window)
        assert hits(page) == {uris[order[0]], uris[order[1]]}
        assert page["returned"] == page["total_matches"] == 2 and not page["degraded"]
        assert page["time_range"]["field"] == field
        for key, expected in (("gte", start), ("lt", end)):
            echoed = datetime.fromisoformat(page["time_range"][key])
            assert echoed.tzinfo == UTC and echoed == expected

        last = await search(source_type="document", time_range={"field": field, "gte": end.isoformat()})
        assert hits(last) == {uris[order[2]]} and "lt" not in last["time_range"]
        empty = await search(source_type="document", time_range={"field": field, "lt": start.isoformat()})
        assert empty["results"] == [] and empty["total_matches"] == 0 and not empty["degraded"]
        assert "gte" not in empty["time_range"] and datetime.fromisoformat(empty["time_range"]["lt"]) == start

    assert await search() == baseline
    rejected = await _call_json(mcp_client, runtime_session, "akb_search", {
        "query": query, **args, "time_range": {"field": "updated_at", "gte": "2026-01-01T00:00:00Z"},
    }, expect_error=True)
    assert rejected["code"] == "invalid_argument" and rejected["details"]["field"] == "/source_type"
