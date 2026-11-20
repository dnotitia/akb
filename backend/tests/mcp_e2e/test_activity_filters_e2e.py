"""Activity limits and window boundaries on the authenticated MCP wire."""
from datetime import datetime, timedelta, timezone

from mcp import Client

from .conftest import SecondaryMcpSession
from .runtime import RuntimeContext
from .test_product_e2e import _call_json, _create_vault


async def test_activity_limits_count_matching_events(
    mcp_client: Client, runtime_session: RuntimeContext, secondary_mcp_client: SecondaryMcpSession,
) -> None:
    vault = await _create_vault(mcp_client, runtime_session, "activity-window")
    member = secondary_mcp_client
    await _call_json(mcp_client, runtime_session, "akb_grant",
                     {"vault": vault, "user": member.username, "role": "writer"})
    hashes = []
    for index, client in enumerate([member.client, member.client, mcp_client, mcp_client, mcp_client]):
        written = await _call_json(client, runtime_session, "akb_put", {
            "vault": vault, "collection": "logs", "title": f"Entry {index}", "content": "event",
        })
        hashes.append(written["current_commit"][:12])

    async def activity(**opts):
        return await _call_json(member.client, runtime_session, "akb_activity",
                                {"vault": vault, "collection": "logs", **opts})

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
    await _call_json(mcp_client, runtime_session, "akb_revoke", {"vault": vault, "user": member.username})
    denied = await _call_json(member.client, runtime_session, "akb_activity", {
        "vault": vault, "until": local.isoformat(),
    }, expect_error=True)
    assert denied["code"] == "permission_denied"
