"""Table, permission, token-scope, and security regressions through the SDK."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin

import httpx
import pytest
from mcp import Client

from .conftest import (
    SecondaryMcpSession,
    _open_mcp_client,
)
from .runtime import RuntimeContext
from .test_detail_e2e import _search_until_found
from .test_product_e2e import _call_json, _create_vault, _new_vault_name


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


def _mint_scoped_pat(
    runtime_session: RuntimeContext,
    *,
    prefixes: list[str],
    name: str,
) -> tuple[str, tuple[str, ...]]:
    descriptor = runtime_session.descriptor
    username = os.environ[descriptor.username_env]
    password = os.environ[descriptor.password_env]
    login_url = urljoin(f"{descriptor.app_origin}/", descriptor.login_path.lstrip("/"))
    token_url = urljoin(f"{descriptor.app_origin}/", "api/v1/auth/tokens")

    with httpx.Client(timeout=30.0, trust_env=False) as client:
        login = client.post(login_url, json={"username": username, "password": password})
        if login.status_code != 200:
            raise RuntimeError("scoped PAT login failed")
        jwt = login.json().get("token")
        if not isinstance(jwt, str) or not jwt:
            raise RuntimeError("scoped PAT login returned no token")
        minted = client.post(
            token_url,
            headers={"Authorization": f"Bearer {jwt}"},
            json={"name": name, "vault_scope": {"prefixes": prefixes, "extra_vaults": []}},
        )
        if minted.status_code not in {200, 201}:
            raise RuntimeError("scoped PAT mint failed")
        pat = minted.json().get("token")
        if not isinstance(pat, str) or not pat.startswith("akb_"):
            raise RuntimeError("scoped PAT mint returned no PAT")

    return pat, (username, password, jwt, pat)


@asynccontextmanager
async def _scoped_client(
    runtime_session: RuntimeContext,
    *,
    prefixes: list[str],
    name: str,
) -> AsyncIterator[Client]:
    pat, secrets = _mint_scoped_pat(runtime_session, prefixes=prefixes, name=name)
    async with _open_mcp_client(
        runtime_session,
        pat=pat,
        secrets=(*runtime_session.secrets, *secrets),
        scenario="mcp_security_scoped",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_table_constraints_and_permission_contract(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    runtime_session: RuntimeContext,
) -> None:
    """Exercise declarative constraints, DDL atomicity, and stable permissions."""

    vault = await _create_vault(mcp_client, runtime_session, "constraints")
    events = await _call_json(
        mcp_client,
        runtime_session,
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

    info = await _call_json(mcp_client, runtime_session, "akb_vault_info", {"vault": vault})
    events_info = _table(info, "events")
    assert events_info["unique_keys"][0]["columns"] == ["principal_id", "session_id", "seq"]
    assert events_info["indexes"]

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {
            "vault": vault,
            "sql": "INSERT INTO events (principal_id, session_id, seq) VALUES ('p1','s1',1)",
        },
    )
    duplicate = await _call_json(
        mcp_client,
        runtime_session,
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

    added_key = await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {
            "uri": events_uri,
            "add_unique_keys": [{"name": "events_principal_key", "columns": ["principal_id"]}],
        },
    )
    assert any(item.get("name") == "events_principal_key" for item in added_key["unique_keys"])
    dropped_key = await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {"uri": events_uri, "drop_unique_keys": ["events_principal_key"]},
    )
    assert not any(item.get("name") == "events_principal_key" for item in dropped_key["unique_keys"])

    added_index = await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {"uri": events_uri, "add_indexes": [{"name": "events_seq_idx", "columns": ["seq"]}]},
    )
    assert any(item.get("name") == "events_seq_idx" for item in added_index["indexes"])
    dropped_index = await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {"uri": events_uri, "drop_indexes": ["events_seq_idx"]},
    )
    assert not any(item.get("name") == "events_seq_idx" for item in dropped_index["indexes"])

    duplicates = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_table",
        {"vault": vault, "name": "dups", "columns": [{"name": "email", "type": "text"}]},
    )
    for _ in range(2):
        await _call_json(
            mcp_client,
            runtime_session,
            "akb_sql",
            {"vault": vault, "sql": "INSERT INTO dups (email) VALUES ('a@x.dev')"},
        )
    duplicate_alter = await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {
            "uri": duplicates["uri"],
            "add_unique_keys": [{"name": "dups_email_key", "columns": ["email"]}],
        },
        expect_error=True,
    )
    assert duplicate_alter.get("code") == "invalid_argument"
    info = await _call_json(mcp_client, runtime_session, "akb_vault_info", {"vault": vault})
    assert _table(info, "dups").get("unique_keys") == []
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO dups (email) VALUES ('a@x.dev')"},
    )

    ddl_error = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "CREATE UNIQUE INDEX forbidden_idx ON events (principal_id)"},
        expect_error=True,
    )
    assert ddl_error.get("code") == "method_not_allowed"

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "reader"},
    )
    reader_alter = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_alter_table",
        {"uri": events_uri, "add_columns": [{"name": "reader_probe", "type": "text"}]},
        expect_error=True,
    )
    assert reader_alter.get("code") == "permission_denied", reader_alter
    reader_drop = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_drop_table",
        {"uri": events_uri},
        expect_error=True,
    )
    assert reader_drop.get("code") == "permission_denied", reader_drop
    reader_grant = await _call_json(
        secondary_mcp_client.client,
        runtime_session,
        "akb_grant",
        {"vault": vault, "user": os.environ[runtime_session.descriptor.username_env], "role": "admin"},
        expect_error=True,
    )
    assert reader_grant.get("code") == "permission_denied", reader_grant
    missing_vault = await _call_json(
        mcp_client,
        runtime_session,
        "akb_vault_info",
        {"vault": f"missing-{_new_vault_name('vault')}"},
        expect_error=True,
    )
    assert missing_vault.get("code") == "not_found"
    owner_alter = await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {"uri": events_uri, "add_columns": [{"name": "owner_probe", "type": "text"}]},
    )
    assert any(column.get("name") == "owner_probe" for column in owner_alter["columns"])
    assert not any(column.get("name") == "reader_probe" for column in owner_alter["columns"])

    nulls = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_table",
        {"vault": vault, "name": "nul", "columns": [{"name": "email", "type": "text"}]},
    )
    for _ in range(2):
        await _call_json(
            mcp_client,
            runtime_session,
            "akb_sql",
            {"vault": vault, "sql": "INSERT INTO nul (email) VALUES (NULL)"},
        )
    null_key = await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {
            "uri": nulls["uri"],
            "add_unique_keys": [{"name": "nul_email_key", "columns": ["email"]}],
        },
    )
    assert any(item.get("name") == "nul_email_key" for item in null_key["unique_keys"])
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO nul (email) VALUES ('a@b')"},
    )
    null_duplicate = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO nul (email) VALUES ('a@b')"},
        expect_error=True,
    )
    assert null_duplicate.get("code") == "unique_violation"

    duplicate_column = await _call_json(
        mcp_client,
        runtime_session,
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

    mixed = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_table",
        {"vault": vault, "name": "mc", "columns": [{"name": "principal", "type": "text"}]},
    )
    for _ in range(2):
        await _call_json(
            mcp_client,
            runtime_session,
            "akb_sql",
            {"vault": vault, "sql": "INSERT INTO mc (principal) VALUES ('p')"},
        )
    mixed_alter = await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {
            "uri": mixed["uri"],
            "add_unique_keys": [{"name": "mc_principal_key", "columns": ["PRINCIPAL"]}],
        },
        expect_error=True,
    )
    assert mixed_alter.get("code") == "invalid_argument"
    mixed_create = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_table",
        {
            "vault": vault,
            "name": "mc2",
            "columns": [{"name": "principal", "type": "text"}],
            "unique_keys": [{"columns": ["PRINCIPAL"]}],
        },
    )
    assert mixed_create["unique_keys"][0]["columns"] == ["principal"]

    renamed = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_table",
        {
            "vault": vault,
            "name": "rn",
            "columns": [{"name": "old_c", "type": "text"}],
            "unique_keys": [{"name": "rn_key", "columns": ["old_c"]}],
        },
    )
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {"uri": renamed["uri"], "rename_columns": {"old_c": "new_c"}},
    )
    info = await _call_json(mcp_client, runtime_session, "akb_vault_info", {"vault": vault})
    renamed_key = _table(info, "rn")["unique_keys"]
    assert renamed_key[0]["columns"] == ["new_c"]

    dropped_column = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_table",
        {
            "vault": vault,
            "name": "dpc",
            "columns": [{"name": "keep", "type": "text"}, {"name": "gone", "type": "text"}],
            "unique_keys": [{"name": "dpc_key", "columns": ["gone"]}],
        },
    )
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {"uri": dropped_column["uri"], "drop_columns": ["gone"]},
    )
    info = await _call_json(mcp_client, runtime_session, "akb_vault_info", {"vault": vault})
    assert _table(info, "dpc")["unique_keys"] == []

    atomic = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_table",
        {"vault": vault, "name": "atom", "columns": [{"name": "x", "type": "text"}]},
    )
    for _ in range(2):
        await _call_json(
            mcp_client,
            runtime_session,
            "akb_sql",
            {"vault": vault, "sql": "INSERT INTO atom (x) VALUES ('d')"},
        )
    atomic_error = await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {
            "uri": atomic["uri"],
            "add_indexes": [{"name": "atom_x_idx", "columns": ["x"]}],
            "add_unique_keys": [{"name": "atom_x_key", "columns": ["x"]}],
        },
        expect_error=True,
    )
    assert atomic_error.get("code") == "invalid_argument"
    info = await _call_json(mcp_client, runtime_session, "akb_vault_info", {"vault": vault})
    atom_info = _table(info, "atom")
    assert atom_info["indexes"] == [] and atom_info["unique_keys"] == []

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_alter_table",
        {"uri": nulls["uri"], "drop_unique_keys": ["nul_email_key"]},
    )
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO nul (email) VALUES ('a@b')"},
    )

    updated_at = await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_table",
        {"vault": vault, "name": "ua", "columns": [{"name": "note", "type": "text"}]},
    )
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO ua (note) VALUES ('v1')"},
    )
    equal_stamps = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "SELECT COUNT(*) AS n FROM ua WHERE updated_at = created_at"},
    )
    assert str(equal_stamps["items"][0]["n"]) == "1"
    await asyncio.sleep(1.05)
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "UPDATE ua SET note = 'v2'"},
    )
    bumped = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "SELECT COUNT(*) AS n FROM ua WHERE updated_at > created_at"},
    )
    assert str(bumped["items"][0]["n"]) == "1"
    note = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "SELECT note FROM ua"},
    )
    assert note["items"][0]["note"] == "v2"
    assert updated_at["uri"].endswith("/ua")


@pytest.mark.asyncio
async def test_security_access_and_sql_failure_state(
    mcp_client: Client,
    secondary_mcp_client: SecondaryMcpSession,
    runtime_session: RuntimeContext,
) -> None:
    """Preserve private-data, graph, SQL, and error-envelope boundaries."""

    vault = await _create_vault(mcp_client, runtime_session, "security")
    document = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {
            "vault": vault,
            "collection": "secrets",
            "title": "Private Security Document",
            "content": "# Private\nSECURITY_MARKER_ALPHA",
        },
    )
    other = secondary_mcp_client.client

    denied_grep = await _call_json(
        other,
        runtime_session,
        "akb_grep",
        {"pattern": "SECURITY_MARKER_ALPHA", "vault": vault},
        expect_error=True,
    )
    _assert_permission_denied(denied_grep)
    denied_search = await _call_json(
        other,
        runtime_session,
        "akb_search",
        {"query": "SECURITY_MARKER_ALPHA", "vault": vault},
        expect_error=True,
    )
    _assert_permission_denied(denied_search)
    unscoped_search = await _call_json(
        other,
        runtime_session,
        "akb_search",
        {"query": "SECURITY_MARKER_ALPHA"},
    )
    assert not any(item.get("vault") == vault for item in unscoped_search.get("results", []))

    for tool, arguments in (
        ("akb_relations", {"uri": document["uri"]}),
        ("akb_graph", {"vault": vault}),
        ("akb_provenance", {"uri": document["uri"]}),
    ):
        denied = await _call_json(other, runtime_session, tool, arguments, expect_error=True)
        _assert_permission_denied(denied)

    owner_grep = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grep",
        {"pattern": "SECURITY_MARKER_ALPHA", "vault": vault},
    )
    assert owner_grep.get("total_matches", 0) >= 1
    owner_search = await _search_until_found(
        mcp_client,
        runtime_session,
        "SECURITY_MARKER_ALPHA",
        vault=vault,
    )
    assert int(owner_search.get("total", 0)) >= 1 or owner_search.get("results")
    owner_graph = await _call_json(mcp_client, runtime_session, "akb_graph", {"vault": vault})
    assert "nodes" in owner_graph and "edges" in owner_graph

    invalid_regex = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grep",
        {"pattern": "(invalid[[", "regex": True, "vault": vault},
        expect_error=True,
    )
    assert "regex" in _json_text(invalid_regex)
    valid_regex = await _call_json(
        mcp_client,
        runtime_session,
        "akb_grep",
        {"pattern": "SECURITY_MARKER.*ALPHA", "regex": True, "vault": vault},
    )
    assert valid_regex.get("total_matches", 0) >= 1

    await _call_json(
        mcp_client,
        runtime_session,
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
        await _call_json(
            mcp_client,
            runtime_session,
            "akb_sql",
            {"vault": vault, "sql": f"INSERT INTO finances (item, amount) VALUES ('{item}', {amount})"},
        )
    for sql in (
        "SELECT * FROM finances",
        "INSERT INTO finances (item, amount) VALUES ('Hack', 0)",
    ):
        denied = await _call_json(other, runtime_session, "akb_sql", {"vault": vault, "sql": sql}, expect_error=True)
        _assert_permission_denied(denied)

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "reader"},
    )
    readable = await _call_json(other, runtime_session, "akb_sql", {"vault": vault, "sql": "SELECT * FROM finances"})
    assert len(readable["items"]) == 2
    for sql in (
        "INSERT INTO finances (item, amount) VALUES ('Blocked', 0)",
        "UPDATE finances SET amount = 0 WHERE item = 'Revenue'",
        "DELETE FROM finances WHERE item = 'Revenue'",
        "/* bypass */ INSERT INTO finances (item, amount) VALUES ('Comment', 0)",
        "WITH del AS (DELETE FROM finances RETURNING *) SELECT * FROM del",
        "SELECT 1; DELETE FROM finances",
    ):
        denied = await _call_json(other, runtime_session, "akb_sql", {"vault": vault, "sql": sql}, expect_error=True)
        assert denied.get("code") or denied.get("error"), denied
    unchanged = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "SELECT COUNT(*) AS cnt FROM finances"},
    )
    assert str(unchanged["items"][0]["cnt"]) == "2"

    await _call_json(
        mcp_client,
        runtime_session,
        "akb_grant",
        {"vault": vault, "user": secondary_mcp_client.username, "role": "writer"},
    )
    await _call_json(
        other,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "INSERT INTO finances (item, amount) VALUES ('Writer Added', 500)"},
    )
    count = await _call_json(
        mcp_client,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "SELECT COUNT(*) AS cnt FROM finances"},
    )
    assert str(count["items"][0]["cnt"]) == "3"
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_revoke",
        {"vault": vault, "user": secondary_mcp_client.username},
    )
    revoked = await _call_json(
        other,
        runtime_session,
        "akb_sql",
        {"vault": vault, "sql": "SELECT * FROM finances"},
        expect_error=True,
    )
    _assert_permission_denied(revoked)
    unknown = await _call_json(
        mcp_client,
        runtime_session,
        "akb_activity",
        {"vault": vault, "user": "nobody"},
        expect_error=True,
    )
    assert "unknown argument 'user'" in _json_text(unknown)
    assert "author" in str(unknown.get("hint", ""))
    valid_activity = await _call_json(
        mcp_client,
        runtime_session,
        "akb_activity",
        {"vault": vault, "author": "nobody"},
    )
    assert "returned" in valid_activity


@pytest.mark.asyncio
async def test_pat_vault_scope_write_boundary(
    mcp_client: Client,
    runtime_session: RuntimeContext,
) -> None:
    """Keep PAT write scope enforcement and read behavior on the SDK path."""

    prefix = "mcp-sdk-scope-gdn-"
    scoped_name = _new_vault_name("scope-gdn")
    other_name = _new_vault_name("scope-other")
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_vault",
        {"name": scoped_name},
    )
    await _call_json(
        mcp_client,
        runtime_session,
        "akb_create_vault",
        {"name": other_name},
    )

    descriptor = runtime_session.descriptor
    username = os.environ[descriptor.username_env]
    password = os.environ[descriptor.password_env]
    login_url = urljoin(f"{descriptor.app_origin}/", descriptor.login_path.lstrip("/"))
    token_url = urljoin(f"{descriptor.app_origin}/", "api/v1/auth/tokens")
    with httpx.Client(timeout=30.0, trust_env=False) as client:
        login = client.post(login_url, json={"username": username, "password": password})
        assert login.status_code == 200
        jwt = login.json()["token"]
        malformed = client.post(
            token_url,
            headers={"Authorization": f"Bearer {jwt}"},
            json={"name": "malformed-scope", "vault_scope": {"prefixes": ["BAD UPPER"]}},
        )
        assert malformed.status_code == 422

    async with _scoped_client(
        runtime_session,
        prefixes=[prefix],
        name="mcp-sdk-scoped",
    ) as scoped:
        in_scope = await _call_json(
            scoped,
            runtime_session,
            "akb_put",
            {"vault": scoped_name, "collection": "c", "title": "In", "content": "# in"},
        )
        assert in_scope["uri"]
        denied = await _call_json(
            scoped,
            runtime_session,
            "akb_put",
            {"vault": other_name, "collection": "c", "title": "Out", "content": "# out"},
            expect_error=True,
        )
        _assert_scope_denied(denied)
        readable = await _call_json(scoped, runtime_session, "akb_browse", {"vault": other_name})
        assert "items" in readable
        assert not any(item.get("title") == "Out" for item in readable["items"])

    control = await _call_json(
        mcp_client,
        runtime_session,
        "akb_put",
        {"vault": other_name, "collection": "c", "title": "Control", "content": "# control"},
    )
    assert control["uri"]
