"""MCP authorization, SQL, and constraint contracts over the authenticated ASGI routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest
from mcp import Client
from starlette.types import ASGIApp

from app.repositories.table_data_repo import pg_table_name

from .helpers import Actor
from .mcp_client import (
    SecondaryMcpSession,
    call_json,
    create_vault,
    mcp_actor as mcp_actor,
    mcp_session,
    secondary_mcp_client as secondary_mcp_client,
)
from .search_fixtures import drain_search_index as drain_search_index

pytestmark = pytest.mark.live_services


@pytest.fixture
async def security_admin(mcp_actor: Actor, pg_pool: asyncpg.Pool) -> Actor:
    await pg_pool.execute("UPDATE users SET is_admin = TRUE WHERE id = $1", mcp_actor.id)
    return mcp_actor


@pytest.fixture
async def mcp_client(asgi_app: ASGIApp, security_admin: Actor) -> AsyncIterator[Client]:
    async with mcp_session(asgi_app, security_admin.pat) as client:
        yield client


def _table(result: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in result.get("tables", []) if item.get("name") == name)


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False).lower()


def _assert_permission_denied(result: dict[str, Any]) -> None:
    details = result.get("details") or {}
    assert (
        result.get("code") == "permission_denied"
        or details.get("pg_sqlstate") == "42501"
        or "permission denied" in _json_text(result)
        or "access denied" in _json_text(result)
        or "forbidden" in _json_text(result)
    ), result


def _assert_scope_denied(result: dict[str, Any]) -> None:
    assert (
        result.get("code") == "permission_denied" or "scope" in _json_text(result) or "permission" in _json_text(result)
    ), result


async def _mint_pat(
    api: httpx.AsyncClient,
    jwt: str,
    spec: dict[str, Any],
    *,
    path: str = "/api/v1/auth/tokens",
) -> str:
    minted = await api.post(path, headers={"Authorization": f"Bearer {jwt}"}, json=spec)
    minted.raise_for_status()
    pat = minted.json()["token"]
    assert isinstance(pat, str) and pat.startswith("akb_")
    return pat


@pytest.mark.asyncio
async def test_read_scoped_pat_cannot_reach_other_vault_physical_table(
    mcp_client: Client,
    security_admin: Actor,
    api: httpx.AsyncClient,
    asgi_app: ASGIApp,
) -> None:
    allowed = await create_vault(mcp_client, "sqlallowed")
    forbidden = await create_vault(mcp_client, "sqlforbidden")
    for vault in (allowed, forbidden):
        await call_json(mcp_client, "akb_create_table", {
            "vault": vault, "name": "items", "columns": [{"name": "value", "type": "text"}],
        })
        await call_json(mcp_client, "akb_sql", {
            "vault": vault, "sql": "INSERT INTO items (value) VALUES ('original')",
        })
    pat = await _mint_pat(api, security_admin.jwt, {
        "name": "read-scoped",
        "scopes": ["read"],
        "vault_scope": {"prefixes": [allowed], "extra_vaults": []},
    })
    async with mcp_session(asgi_app, pat) as reader:
        result = await call_json(reader, "akb_sql", {
            "vault": allowed, "sql": "SELECT value FROM items",
        })
        assert result["items"] == [{"value": "original"}]
        for args in (
            {"vault": forbidden, "sql": "SELECT value FROM items"},
            {"vault": allowed, "sql": f'SELECT value FROM "{pg_table_name(forbidden, "items")}"'},
        ):
            denied = await call_json(reader, "akb_sql", args, expect_error=True)
            _assert_permission_denied(denied)


@pytest.mark.asyncio
@pytest.mark.parametrize("key_class", ["pat", "service"])
async def test_read_credential_respects_reader_and_writer_roles(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    api: httpx.AsyncClient,
    asgi_app: ASGIApp,
    security_admin: Actor,
    key_class: str,
) -> None:
    member = secondary_mcp_client.actor
    jwt = member.jwt
    token_path = "/api/v1/auth/tokens"
    if key_class == "service":
        jwt = security_admin.jwt
        token_path = f"/api/v1/admin/users/{member.id}/tokens"
    pat = await _mint_pat(
        api, jwt, {"name": "read-role-sql", "key_class": key_class, "scopes": ["read"]}, path=token_path,
    )
    vault = await create_vault(mcp_client, "sqlroles")
    await call_json(mcp_client, "akb_create_table", {
        "vault": vault, "name": "items", "columns": [{"name": "value", "type": "text"}],
    })
    await call_json(mcp_client, "akb_sql", {
        "vault": vault, "sql": "INSERT INTO items (value) VALUES ('original')",
    })
    async with mcp_session(asgi_app, pat) as reader:
        for role in ("reader", "writer"):
            await call_json(mcp_client, "akb_grant", {
                "vault": vault, "user": member.username, "role": role,
            })
            rows = await call_json(reader, "akb_sql", {
                "vault": vault, "sql": "SELECT value FROM items",
            })
            assert rows["items"] == [{"value": "original"}]
            denied = await call_json(reader, "akb_sql", {
                "vault": vault, "sql": "DELETE FROM items",
            }, expect_error=True)
            _assert_permission_denied(denied)
            if role == "writer":
                assert denied["details"]["pg_sqlstate"] == "25006"
    unchanged = await call_json(mcp_client, "akb_sql", {
        "vault": vault, "sql": "SELECT value FROM items",
    })
    assert unchanged["items"] == [{"value": "original"}]
    owner = secondary_mcp_client.client
    owned = await create_vault(owner, "sqlowner")
    await call_json(owner, "akb_create_table", {
        "vault": owned, "name": "items", "columns": [{"name": "value", "type": "text"}],
    })
    await call_json(owner, "akb_sql", {
        "vault": owned, "sql": "INSERT INTO items (value) VALUES ('original')",
    })
    async with mcp_session(asgi_app, pat) as reader:
        denied = await call_json(reader, "akb_sql", {
            "vault": owned, "sql": "DELETE FROM items",
        }, expect_error=True)
        assert denied["code"] == "permission_denied"
        assert denied["details"]["pg_sqlstate"] == "25006"
    rows = await call_json(owner, "akb_sql", {
        "vault": owned, "sql": "SELECT value FROM items",
    })
    assert rows["items"] == [{"value": "original"}]


@pytest.mark.asyncio
async def test_table_constraints_and_permission_contract(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    security_admin: Actor,
) -> None:
    """Exercise declarative constraints, DDL atomicity, and stable permissions."""

    vault = await create_vault(mcp_client, "constraints")
    events = await call_json(
        mcp_client,
        "akb_create_table",
        {
            "vault": vault,
            "name": "events",
            "columns": [
                {"name": "principal_id", "type": "text", "required": True},
                {"name": "session_id", "type": "text", "required": True},
                {"name": "seq", "type": "number", "required": True},
            ],
            "unique_keys": [{"columns": ["principal_id", "session_id", "seq"]}],
            "indexes": [
                {
                    "columns": [
                        {"name": "principal_id"},
                        {"name": "session_id"},
                        {"name": "seq", "order": "desc"},
                    ]
                }
            ],
        },
    )
    events_uri = events["uri"]
    assert events["unique_keys"] and events["indexes"]

    info = await call_json(mcp_client, "akb_vault_info", {"vault": vault})
    events_info = _table(info, "events")
    assert events_info["unique_keys"][0]["columns"] == ["principal_id", "session_id", "seq"]
    assert events_info["indexes"]

    await call_json(
        mcp_client,
        "akb_sql",
        {
            "vault": vault,
            "sql": "INSERT INTO events (principal_id, session_id, seq) VALUES ('p1','s1',1)",
        },
    )
    duplicate = await call_json(
        mcp_client,
        "akb_sql",
        {
            "vault": vault,
            "sql": "INSERT INTO events (principal_id, session_id, seq) VALUES ('p1','s1',1)",
        },
        expect_error=True,
    )
    assert duplicate.get("code") == "unique_violation" or duplicate.get("details", {}).get("pg_sqlstate") == "23505", (
        duplicate
    )

    added_key = await call_json(
        mcp_client,
        "akb_alter_table",
        {
            "uri": events_uri,
            "add_unique_keys": [{"name": "events_principal_key", "columns": ["principal_id"]}],
        },
    )
    assert any(item.get("name") == "events_principal_key" for item in added_key["unique_keys"])
    dropped_key = await call_json(
        mcp_client,
        "akb_alter_table",
        {"uri": events_uri, "drop_unique_keys": ["events_principal_key"]},
    )
    assert not any(item.get("name") == "events_principal_key" for item in dropped_key["unique_keys"])

    added_index = await call_json(
        mcp_client,
        "akb_alter_table",
        {"uri": events_uri, "add_indexes": [{"name": "events_seq_idx", "columns": ["seq"]}]},
    )
    assert any(item.get("name") == "events_seq_idx" for item in added_index["indexes"])
    dropped_index = await call_json(
        mcp_client,
        "akb_alter_table",
        {"uri": events_uri, "drop_indexes": ["events_seq_idx"]},
    )
    assert not any(item.get("name") == "events_seq_idx" for item in dropped_index["indexes"])

    duplicates = await call_json(
        mcp_client,
        "akb_create_table",
        {"vault": vault, "name": "dups", "columns": [{"name": "email", "type": "text"}]},
    )
    for _ in range(2):
        await call_json(
            mcp_client,
            "akb_sql",
            {"vault": vault, "sql": "INSERT INTO dups (email) VALUES ('a@x.dev')"},
        )
    duplicate_alter = await call_json(
        mcp_client,
        "akb_alter_table",
        {
            "uri": duplicates["uri"],
            "add_unique_keys": [{"name": "dups_email_key", "columns": ["email"]}],
        },
        expect_error=True,
    )
    assert duplicate_alter.get("code") == "invalid_argument"
    info = await call_json(mcp_client, "akb_vault_info", {"vault": vault})
    assert _table(info, "dups").get("unique_keys") == []
    await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO dups (email) VALUES ('a@x.dev')"},
    )

    ddl_error = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "CREATE UNIQUE INDEX forbidden_idx ON events (principal_id)"},
        expect_error=True,
    )
    assert ddl_error.get("code") == "method_not_allowed"

    await call_json(
        mcp_client,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "reader"},
    )
    reader_alter = await call_json(
        secondary_mcp_client.client,
        "akb_alter_table",
        {"uri": events_uri, "add_columns": [{"name": "reader_probe", "type": "text"}]},
        expect_error=True,
    )
    assert reader_alter.get("code") == "permission_denied", reader_alter
    reader_drop = await call_json(
        secondary_mcp_client.client,
        "akb_drop_table",
        {"uri": events_uri},
        expect_error=True,
    )
    assert reader_drop.get("code") == "permission_denied", reader_drop
    reader_grant = await call_json(
        secondary_mcp_client.client,
        "akb_grant",
        {"vault": vault, "user": security_admin.username, "role": "admin"},
        expect_error=True,
    )
    assert reader_grant.get("code") == "permission_denied", reader_grant
    missing_vault = await call_json(
        mcp_client,
        "akb_vault_info",
        {"vault": f"missing-{uuid4().hex[:10]}"},
        expect_error=True,
    )
    assert missing_vault.get("code") == "not_found"
    owner_alter = await call_json(
        mcp_client,
        "akb_alter_table",
        {"uri": events_uri, "add_columns": [{"name": "owner_probe", "type": "text"}]},
    )
    assert any(column.get("name") == "owner_probe" for column in owner_alter["columns"])
    assert not any(column.get("name") == "reader_probe" for column in owner_alter["columns"])

    nulls = await call_json(
        mcp_client,
        "akb_create_table",
        {"vault": vault, "name": "nul", "columns": [{"name": "email", "type": "text"}]},
    )
    for _ in range(2):
        await call_json(
            mcp_client,
            "akb_sql",
            {"vault": vault, "sql": "INSERT INTO nul (email) VALUES (NULL)"},
        )
    null_key = await call_json(
        mcp_client,
        "akb_alter_table",
        {
            "uri": nulls["uri"],
            "add_unique_keys": [{"name": "nul_email_key", "columns": ["email"]}],
        },
    )
    assert any(item.get("name") == "nul_email_key" for item in null_key["unique_keys"])
    await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO nul (email) VALUES ('a@b')"},
    )
    null_duplicate = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO nul (email) VALUES ('a@b')"},
        expect_error=True,
    )
    assert null_duplicate.get("code") == "unique_violation"

    duplicate_column = await call_json(
        mcp_client,
        "akb_create_table",
        {
            "vault": vault,
            "name": "dupcol",
            "columns": [{"name": "a", "type": "text"}],
            "unique_keys": [{"columns": ["a", "a"]}],
        },
        expect_error=True,
    )
    assert duplicate_column.get("code") == "invalid_argument"

    mixed = await call_json(
        mcp_client,
        "akb_create_table",
        {"vault": vault, "name": "mc", "columns": [{"name": "principal", "type": "text"}]},
    )
    for _ in range(2):
        await call_json(
            mcp_client,
            "akb_sql",
            {"vault": vault, "sql": "INSERT INTO mc (principal) VALUES ('p')"},
        )
    mixed_alter = await call_json(
        mcp_client,
        "akb_alter_table",
        {
            "uri": mixed["uri"],
            "add_unique_keys": [{"name": "mc_principal_key", "columns": ["PRINCIPAL"]}],
        },
        expect_error=True,
    )
    assert mixed_alter.get("code") == "invalid_argument"
    mixed_create = await call_json(
        mcp_client,
        "akb_create_table",
        {
            "vault": vault,
            "name": "mc2",
            "columns": [{"name": "principal", "type": "text"}],
            "unique_keys": [{"columns": ["PRINCIPAL"]}],
        },
    )
    assert mixed_create["unique_keys"][0]["columns"] == ["principal"]

    renamed = await call_json(
        mcp_client,
        "akb_create_table",
        {
            "vault": vault,
            "name": "rn",
            "columns": [{"name": "old_c", "type": "text"}],
            "unique_keys": [{"name": "rn_key", "columns": ["old_c"]}],
        },
    )
    await call_json(
        mcp_client,
        "akb_alter_table",
        {"uri": renamed["uri"], "rename_columns": {"old_c": "new_c"}},
    )
    info = await call_json(mcp_client, "akb_vault_info", {"vault": vault})
    renamed_key = _table(info, "rn")["unique_keys"]
    assert renamed_key[0]["columns"] == ["new_c"]

    dropped_column = await call_json(
        mcp_client,
        "akb_create_table",
        {
            "vault": vault,
            "name": "dpc",
            "columns": [{"name": "keep", "type": "text"}, {"name": "gone", "type": "text"}],
            "unique_keys": [{"name": "dpc_key", "columns": ["gone"]}],
        },
    )
    await call_json(
        mcp_client,
        "akb_alter_table",
        {"uri": dropped_column["uri"], "drop_columns": ["gone"]},
    )
    info = await call_json(mcp_client, "akb_vault_info", {"vault": vault})
    assert _table(info, "dpc")["unique_keys"] == []

    atomic = await call_json(
        mcp_client,
        "akb_create_table",
        {"vault": vault, "name": "atom", "columns": [{"name": "x", "type": "text"}]},
    )
    for _ in range(2):
        await call_json(
            mcp_client,
            "akb_sql",
            {"vault": vault, "sql": "INSERT INTO atom (x) VALUES ('d')"},
        )
    atomic_error = await call_json(
        mcp_client,
        "akb_alter_table",
        {
            "uri": atomic["uri"],
            "add_indexes": [{"name": "atom_x_idx", "columns": ["x"]}],
            "add_unique_keys": [{"name": "atom_x_key", "columns": ["x"]}],
        },
        expect_error=True,
    )
    assert atomic_error.get("code") == "invalid_argument"
    info = await call_json(mcp_client, "akb_vault_info", {"vault": vault})
    atom_info = _table(info, "atom")
    assert atom_info["indexes"] == [] and atom_info["unique_keys"] == []

    await call_json(
        mcp_client,
        "akb_alter_table",
        {"uri": nulls["uri"], "drop_unique_keys": ["nul_email_key"]},
    )
    await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO nul (email) VALUES ('a@b')"},
    )

    updated_at = await call_json(
        mcp_client,
        "akb_create_table",
        {"vault": vault, "name": "ua", "columns": [{"name": "note", "type": "text"}]},
    )
    await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO ua (note) VALUES ('v1')"},
    )
    equal_stamps = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "SELECT COUNT(*) AS n FROM ua WHERE updated_at = created_at"},
    )
    assert str(equal_stamps["items"][0]["n"]) == "1"
    await asyncio.sleep(1.05)
    await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "UPDATE ua SET note = 'v2'"},
    )
    bumped = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "SELECT COUNT(*) AS n FROM ua WHERE updated_at > created_at"},
    )
    assert str(bumped["items"][0]["n"]) == "1"
    note = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "SELECT note FROM ua"},
    )
    assert note["items"][0]["note"] == "v2"
    assert updated_at["uri"].endswith("/ua")


@pytest.mark.asyncio
async def test_security_access_and_sql_failure_state(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    drain_search_index: Callable[[], Awaitable[None]],
) -> None:
    """Preserve private-data, graph, SQL, and error-envelope boundaries."""

    vault = await create_vault(mcp_client, "security")
    document = await call_json(
        mcp_client,
        "akb_put",
        {
            "vault": vault,
            "collection": "secrets",
            "title": "Private Security Document",
            "content": "# Private\nSECURITY_MARKER_ALPHA",
        },
    )
    await drain_search_index()
    other = secondary_mcp_client.client

    denied_grep = await call_json(
        other,
        "akb_grep",
        {"pattern": "SECURITY_MARKER_ALPHA", "vault": vault},
        expect_error=True,
    )
    _assert_permission_denied(denied_grep)
    denied_search = await call_json(
        other,
        "akb_search",
        {"query": "SECURITY_MARKER_ALPHA", "vault": vault},
        expect_error=True,
    )
    _assert_permission_denied(denied_search)
    unscoped_search = await call_json(
        other,
        "akb_search",
        {"query": "SECURITY_MARKER_ALPHA"},
    )
    assert not any(item.get("vault") == vault for item in unscoped_search.get("results", []))

    for tool, arguments in (
        ("akb_relations", {"uri": document["uri"]}),
        ("akb_graph", {"vault": vault}),
        ("akb_provenance", {"uri": document["uri"]}),
    ):
        denied = await call_json(other, tool, arguments, expect_error=True)
        _assert_permission_denied(denied)

    owner_grep = await call_json(
        mcp_client,
        "akb_grep",
        {"pattern": "SECURITY_MARKER_ALPHA", "vault": vault},
    )
    assert owner_grep.get("total_matches", 0) >= 1
    owner_search = await call_json(
        mcp_client, "akb_search", {"query": "SECURITY_MARKER_ALPHA", "vault": vault},
    )
    assert int(owner_search.get("total", 0)) >= 1 or owner_search.get("results")
    owner_graph = await call_json(mcp_client, "akb_graph", {"vault": vault})
    assert "nodes" in owner_graph and "edges" in owner_graph

    invalid_regex = await call_json(
        mcp_client,
        "akb_grep",
        {"pattern": "(invalid[[", "regex": True, "vault": vault},
        expect_error=True,
    )
    assert "regex" in _json_text(invalid_regex)
    valid_regex = await call_json(
        mcp_client,
        "akb_grep",
        {"pattern": "SECURITY_MARKER.*ALPHA", "regex": True, "vault": vault},
    )
    assert valid_regex.get("total_matches", 0) >= 1

    await call_json(
        mcp_client,
        "akb_create_table",
        {
            "vault": vault,
            "name": "finances",
            "columns": [
                {"name": "item", "type": "text", "required": True},
                {"name": "amount", "type": "number"},
            ],
        },
    )
    for item, amount in (("Revenue", 1000000), ("Secret Cost", 999)):
        await call_json(
            mcp_client,
            "akb_sql",
            {"vault": vault, "sql": f"INSERT INTO finances (item, amount) VALUES ('{item}', {amount})"},
        )
    for sql in (
        "SELECT * FROM finances",
        "INSERT INTO finances (item, amount) VALUES ('Hack', 0)",
    ):
        denied = await call_json(other, "akb_sql", {"vault": vault, "sql": sql}, expect_error=True)
        _assert_permission_denied(denied)

    await call_json(
        mcp_client,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "reader"},
    )
    readable = await call_json(other, "akb_sql", {"vault": vault, "sql": "SELECT * FROM finances"})
    assert len(readable["items"]) == 2
    for sql in (
        "INSERT INTO finances (item, amount) VALUES ('Blocked', 0)",
        "UPDATE finances SET amount = 0 WHERE item = 'Revenue'",
        "DELETE FROM finances WHERE item = 'Revenue'",
        "/* bypass */ INSERT INTO finances (item, amount) VALUES ('Comment', 0)",
        "WITH del AS (DELETE FROM finances RETURNING *) SELECT * FROM del",
        "SELECT 1; DELETE FROM finances",
    ):
        denied = await call_json(other, "akb_sql", {"vault": vault, "sql": sql}, expect_error=True)
        assert denied.get("code") or denied.get("error"), denied
    unchanged = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "SELECT COUNT(*) AS cnt FROM finances"},
    )
    assert str(unchanged["items"][0]["cnt"]) == "2"

    await call_json(
        mcp_client,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "writer"},
    )
    await call_json(
        other,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO finances (item, amount) VALUES ('Writer Added', 500)"},
    )
    count = await call_json(
        mcp_client,
        "akb_sql",
        {"vault": vault, "sql": "SELECT COUNT(*) AS cnt FROM finances"},
    )
    assert str(count["items"][0]["cnt"]) == "3"
    await call_json(
        mcp_client,
        "akb_revoke",
        {"vault": vault, "user": secondary_mcp_client.username},
    )
    revoked = await call_json(
        other,
        "akb_sql",
        {"vault": vault, "sql": "SELECT * FROM finances"},
        expect_error=True,
    )
    _assert_permission_denied(revoked)
    unknown = await call_json(
        mcp_client,
        "akb_activity",
        {"vault": vault, "user": "nobody"},
        expect_error=True,
    )
    assert "unknown argument 'user'" in _json_text(unknown)
    assert "author" in str(unknown.get("hint", ""))
    valid_activity = await call_json(
        mcp_client,
        "akb_activity",
        {"vault": vault, "author": "nobody"},
    )
    assert "returned" in valid_activity


@pytest.mark.asyncio
async def test_pat_vault_scope_write_boundary(
    mcp_client: Client,
    security_admin: Actor,
    api: httpx.AsyncClient,
    asgi_app: ASGIApp,
) -> None:
    """Keep PAT write scope enforcement and read behavior on the SDK path."""

    scoped_name = await create_vault(mcp_client, "scope-gdn")
    other_name = await create_vault(mcp_client, "scope-other")
    malformed = await api.post(
        "/api/v1/auth/tokens",
        headers={"Authorization": f"Bearer {security_admin.jwt}"},
        json={"name": "malformed-scope", "vault_scope": {"prefixes": ["BAD UPPER"]}},
    )
    assert malformed.status_code == 422

    pat = await _mint_pat(api, security_admin.jwt, {
        "name": "mcp-sdk-scoped",
        "vault_scope": {"prefixes": ["mcp-sdk-scope-gdn-"], "extra_vaults": []},
    })
    async with mcp_session(asgi_app, pat) as scoped:
        in_scope = await call_json(
            scoped,
            "akb_put",
            {"vault": scoped_name, "collection": "c", "title": "In", "content": "# in"},
        )
        assert in_scope["uri"]
        denied = await call_json(
            scoped,
            "akb_put",
            {"vault": other_name, "collection": "c", "title": "Out", "content": "# out"},
            expect_error=True,
        )
        _assert_scope_denied(denied)
        readable = await call_json(scoped, "akb_browse", {"vault": other_name})
        assert "items" in readable
        assert not any(item.get("title") == "Out" for item in readable["items"])

    control = await call_json(
        mcp_client,
        "akb_put",
        {"vault": other_name, "collection": "c", "title": "Control", "content": "# control"},
    )
    assert control["uri"]
