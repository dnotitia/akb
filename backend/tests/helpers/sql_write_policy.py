"""SQL write-policy assertions shared by the HTTP and stdio SDK suites."""

import json
from uuid import uuid4

from mcp import types

from app.repositories.table_data_repo import pg_table_name


def _text_content(result):
    text = next((item.text for item in result.content if isinstance(item, types.TextContent)), None)
    assert text is not None, "MCP tool returned no JSON text"
    return text


async def _call_json(client, name, arguments):
    result = await client.call_tool(name, arguments)
    payload = json.loads(_text_content(result))
    assert isinstance(payload, dict)
    assert not result.is_error and "error" not in payload, payload
    return payload


async def _create_vault(client, prefix):
    name = f"mcp-sdk-{prefix}-{uuid4().hex[:10]}"
    result = await _call_json(client, "akb_create_vault", {"name": name, "description": "MCP SDK behavior test"})
    assert result.get("name") == name
    assert isinstance(result.get("vault_id"), str)
    return name


async def _sql(client, vaults: list[str], statement: str, *, denied: str | None = None):
    scope = {"vault": vaults[0]} if len(vaults) == 1 else {"vaults": vaults}
    args = {**scope, "sql": statement}
    result = await client.call_tool("akb_sql", args)
    payload = json.loads(_text_content(result))
    if payload.get("code") == "vault_skill_required":
        guide = payload["vault_skill"]
        assert guide["body"]
        result = await client.call_tool("akb_sql", {**args, "_vault_skill_ack": guide["ack_token"]})
        payload = json.loads(_text_content(result))
    if denied:
        assert result.is_error and payload["code"] == denied, payload
    else:
        assert not result.is_error and "error" not in payload, payload
        assert payload["vaults"] == vaults
    return payload


async def _stored(database, vault):
    table = pg_table_name(vault, "items")
    return [tuple(row) for row in await database.fetch(f'SELECT amount, row_commit FROM "{table}" ORDER BY id')]


async def _seed(owner, prefix, username, role):
    vault = await _create_vault(owner, prefix)
    await _call_json(owner, "akb_create_table", {
        "vault": vault, "name": "items", "columns": [{"name": "amount", "type": "int"}],
    })
    await _sql(owner, [vault], "INSERT INTO items (amount) VALUES (1)")
    await _call_json(owner, "akb_grant", {"vault": vault, "user": username, "role": role})
    return vault


async def _read_amount(client, vault, expected):
    query = await _sql(client, [vault], "SELECT amount FROM items ORDER BY id")
    assert query["kind"] == "table_query"
    assert query["items"] == [{"amount": amount} for amount, _ in expected]
    assert query["total"] == len(expected)


async def _change_amount(client, vault, database, *, additional_vaults=()):
    before, = await _stored(database, vault)
    name = vault.replace("-", "_") + "__items" if additional_vaults else "items"
    receipt = await _sql(client, [*additional_vaults, vault], f"UPDATE {name} SET amount = amount + 1")
    after, = await _stored(database, vault)
    assert (receipt["kind"], receipt["result"], receipt["affected_rows"]) == ("table_sql", "UPDATE 1", 1)
    assert after[0] == before[0] + 1 and after[1] != before[1]


async def assert_ordinary_archived_and_revoked_sql(owner, database, member_session, username, pat):
    normal = await _seed(owner, "sql-open", username, "reader")
    archived = await _seed(owner, "sql-arch", username, "writer")
    await _call_json(owner, "akb_archive_vault", {"vault": archived})
    original = await _stored(database, normal)
    frozen = await _stored(database, archived)

    async with member_session(pat) as member:
        await _read_amount(member, normal, original)
        await _sql(member, [normal], "UPDATE items SET amount = 2", denied="permission_denied")
        assert await _stored(database, normal) == original
        await _call_json(owner, "akb_grant", {"vault": normal, "user": username, "role": "writer"})
        await _change_amount(member, normal, database)
        committed = await _stored(database, normal)
        await _read_amount(member, archived, frozen)
        await _sql(member, [archived], "UPDATE items SET amount = 3", denied="vault_archived")

        # Exercise the public multi-vault aliases and check both underlying tables.
        active_items = normal.replace("-", "_") + "__items"
        archived_items = archived.replace("-", "_") + "__items"
        both = [normal, archived]
        joined = await _sql(member, both, f"SELECT a.amount AS active, b.amount AS archived "
                                          f"FROM {active_items} a CROSS JOIN {archived_items} b")
        assert joined["items"] == [{"active": committed[0][0], "archived": frozen[0][0]}]
        await _sql(member, both, f"UPDATE {archived_items} SET amount = 3", denied="vault_archived")
        assert await _stored(database, archived) == frozen and await _stored(database, normal) == committed

        await _call_json(owner, "akb_revoke", {"vault": normal, "user": username})
        await _sql(member, [normal], "UPDATE items SET amount = 4", denied="permission_denied")
        await _sql(member, both, f"SELECT amount FROM {active_items}", denied="permission_denied")
        await _read_amount(member, archived, frozen)
        assert await _stored(database, normal) == committed

        mode = await _sql(member, [archived], "SELECT current_setting('transaction_read_only') AS mode")
        assert mode["items"] == [{"mode": "on"}]


async def assert_managed_sql_token_grant_and_revoke(owner, database, member_session, username, pat, admin_session):
    managed = await _seed(owner, "sql-managed", username, "writer")
    normal = await _seed(owner, "sql-peer", username, "writer")
    original = await _stored(database, managed)
    other = await _stored(database, normal)
    async with admin_session() as admin:
        token_path = f"/api/v1/admin/users/{username}@example.test/tokens"
        read_pat = await admin.post(token_path, json={"name": "query-only", "key_class": "pat", "scopes": ["read"]})
        read_pat.raise_for_status()
        issue = await admin.post(token_path, json={
            "name": "managed-sql", "key_class": "service", "scopes": ["read", "write"],
        })
        issue.raise_for_status()
        token = issue.json()
        policy = f"/api/v1/admin/vaults/{managed}/write-policy"
        (await admin.put(policy, json={"managed_by": "sql-test"})).raise_for_status()
        # Even a policy grant cannot elevate a read-only credential on a writer account.
        read_token = read_pat.json()
        (await admin.put(f"{policy}/grants/{read_token['token_id']}")).raise_for_status()
        async with member_session(read_token["token"]) as reader:
            for vault in (normal, managed):
                before = await _stored(database, vault)
                await _read_amount(reader, vault, before)
                denied = await _sql(reader, [vault], "UPDATE items SET amount = 3", denied="permission_denied")
                assert denied["details"]["pg_sqlstate"] == "25006"
                assert await _stored(database, vault) == before

        grant = f"{policy}/grants/{token['token_id']}"
        (await admin.put(grant)).raise_for_status()
        async with (
            member_session(pat) as writer,
            member_session(token["token"]) as service,
        ):
            await _read_amount(writer, managed, original)
            await _sql(writer, [managed], "UPDATE items SET amount = 3", denied="vault_write_managed")
            assert await _stored(database, managed) == original
            await _change_amount(service, managed, database, additional_vaults=[normal])
            assert await _stored(database, normal) == other
            committed = await _stored(database, managed)
            await _read_amount(service, managed, committed)

            (await admin.delete(grant)).raise_for_status()
            await _sql(service, [managed], "UPDATE items SET amount = 3", denied="vault_write_managed")
            await _read_amount(service, managed, committed)
            assert await _stored(database, managed) == committed
            await _sql(service, [normal, managed],
                       f"UPDATE {managed.replace('-', '_')}__items SET amount = 3", denied="vault_write_managed")
            assert await _stored(database, managed) == committed and await _stored(database, normal) == other
            mode = await _sql(service, [normal, managed], "SELECT current_setting('transaction_read_only') AS mode")
            assert mode["items"] == [{"mode": "on"}]
