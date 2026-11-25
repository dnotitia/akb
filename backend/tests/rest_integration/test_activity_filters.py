"""Activity limits and window boundaries on the authenticated MCP wire."""

from datetime import datetime, timedelta, timezone

import pytest
from mcp import Client

from .mcp_client import (
    SecondaryMcpSession,
    call_json,
    create_vault,
    mcp_actor as mcp_actor,
    mcp_client as mcp_client,
    secondary_mcp_client as secondary_mcp_client,
)

pytestmark = pytest.mark.live_services


async def test_activity_limits_count_matching_events(
    mcp_client: Client, secondary_mcp_client: SecondaryMcpSession,
) -> None:
    vault = await create_vault(mcp_client, "activity-window")
    member = secondary_mcp_client
    await call_json(mcp_client, "akb_grant", {"vault": vault, "user": member.username, "role": "writer"})
    hashes = []
    for index, client in enumerate([member.client, member.client, mcp_client, mcp_client, mcp_client]):
        written = await call_json(client, "akb_put", {
            "vault": vault, "collection": "logs", "title": f"Entry {index}", "content": "event",
        })
        hashes.append(written["current_commit"][:12])

    async def activity(**opts):
        return await call_json(member.client, "akb_activity", {"vault": vault, "collection": "logs", **opts})

    baseline = await activity()
    assert [e["hash"] for e in baseline["activity"]] == hashes[::-1]
    query = {"author": member.username.upper()}
    limited = await activity(**query, limit=1)
    assert [e["hash"] for e in limited["activity"]] == [hashes[1]]
    assert limited["returned"] == 1 and limited["truncated"] is True
    matched = await activity(**query, limit=2)
    assert [e["hash"] for e in matched["activity"]] == [hashes[1], hashes[0]]
    assert matched["returned"] == 2 and matched["truncated"] is False

    stamp = datetime.fromisoformat(matched["activity"][0]["date"])
    local = stamp.astimezone(timezone(timedelta(hours=9)))
    window = await activity(**query, since=stamp.isoformat(), until=(local + timedelta(microseconds=1)).isoformat())
    assert window["activity"] == [e for e in matched["activity"] if datetime.fromisoformat(e["date"]) == stamp]
    earlier = await activity(**query, until=local.isoformat())
    assert earlier["activity"] == [e for e in matched["activity"] if datetime.fromisoformat(e["date"]) < stamp]
    assert (await activity(since="2000-01-01")) == baseline
    assert (await activity(author="no-such-writer", limit=1)) == {
        "vault": vault, "activity": [], "returned": 0, "truncated": False,
    }
    await call_json(mcp_client, "akb_revoke", {"vault": vault, "user": member.username})
    denied = await call_json(member.client, "akb_activity", {
        "vault": vault, "until": local.isoformat(),
    }, expect_error=True)
    assert denied["code"] == "permission_denied"
