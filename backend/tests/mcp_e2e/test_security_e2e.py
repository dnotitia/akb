"""Read-only SQL through OIDC and the installed stdio proxy."""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urljoin

import asyncpg
import httpx
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from .conftest import _open_mcp_client
from .runtime import RuntimeContext
from .tools import _call_json, _create_vault


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


def _mint_read_pat(runtime_session: RuntimeContext, key_class: str) -> tuple[str, tuple[str, ...]]:
    descriptor = runtime_session.descriptor
    username = os.environ[descriptor.username_env]
    password = os.environ[descriptor.password_env]
    with httpx.Client(timeout=30.0, trust_env=False) as client:
        login = client.post(
            urljoin(f"{descriptor.app_origin}/", descriptor.login_path.lstrip("/")),
            json={"username": username, "password": password},
        )
        login.raise_for_status()
        jwt = login.json()["token"]
        minted = client.post(
            urljoin(f"{descriptor.app_origin}/", "api/v1/auth/tokens"),
            headers={"Authorization": f"Bearer {jwt}"},
            json={"name": "mcp-read-sql", "scopes": ["read"], "key_class": key_class},
        )
        minted.raise_for_status()
        pat = minted.json()["token"]
    return pat, (username, password, jwt, pat)


@pytest.mark.asyncio
@pytest.mark.parametrize("key_class", ["pat", "service"])
async def test_read_pat_sql_keeps_owner_data_unchanged(
    mcp_client: Client, runtime_session: RuntimeContext, key_class: str,
) -> None:
    """Compare read-only SQL behavior through the backend and the installed proxy."""

    vault = await _create_vault(mcp_client, runtime_session, "readsql")
    await _call_json(mcp_client, runtime_session, "akb_create_table", {
        "vault": vault, "name": "items", "columns": [{"name": "value", "type": "text"}],
    })
    await _call_json(mcp_client, runtime_session, "akb_sql", {
        "vault": vault, "sql": "INSERT INTO items (value) VALUES ('original')",
    })
    other = await _create_vault(mcp_client, runtime_session, "readsqljoin")
    await _call_json(mcp_client, runtime_session, "akb_create_table", {
        "vault": other, "name": "labels", "columns": [{"name": "value", "type": "text"}],
    })
    await _call_json(mcp_client, runtime_session, "akb_sql", {
        "vault": other, "sql": "INSERT INTO labels (value) VALUES ('original')",
    })
    pat, secrets = _mint_read_pat(runtime_session, key_class)
    from app.repositories.table_data_repo import pg_table_name

    connection = await asyncpg.connect(
        host="127.0.0.1", port=int(os.environ["AKB_E2E_POSTGRES_PORT"]),
        user="akb", password="akb", database="akb",  # pragma: allowlist secret
    )
    table = pg_table_name(vault, "items")
    function = f"fixture_mutate_{table}"
    # Both identifiers are generated from the runtime's own vault name.
    await connection.execute(
        f'CREATE FUNCTION "{function}"() RETURNS int LANGUAGE plpgsql SECURITY DEFINER '
        f'AS $$ BEGIN UPDATE "{table}" SET value = \'bad\'; RETURN 1; END $$'
    )
    await connection.close()
    async with _open_mcp_client(
        runtime_session, pat=pat, secrets=(*runtime_session.secrets, *secrets),
        scenario="read_sql",
    ) as reader:
        for sql in (
            "SELECT value FROM items",
            "WITH data AS (SELECT value FROM items) SELECT value FROM data",
            "SELECT COUNT(*) AS n FROM items",
        ):
            result = await _call_json(reader, runtime_session, "akb_sql", {"vault": vault, "sql": sql})
            assert result["kind"] == "table_query" and result["total"] == 1
        empty = await _call_json(reader, runtime_session, "akb_sql", {
            "vault": vault, "sql": "SELECT value FROM items WHERE value = 'missing'",
        })
        assert empty["items"] == [] and empty["total"] == 0
        cross = await _call_json(reader, runtime_session, "akb_sql", {
            "vaults": [vault, other],
            "sql": f"SELECT i.value FROM {vault.replace('-', '_')}__items i "
                   f"JOIN {other.replace('-', '_')}__labels l ON i.value = l.value",
        })
        assert cross["items"] == [{"value": "original"}]
        for sql in (
            "INSERT INTO items (value) VALUES ('bad')",
            "UPDATE items SET value = 'bad'",
            "DELETE FROM items",
            "WITH deleted AS (DELETE FROM items RETURNING *) SELECT * FROM deleted",
            "SELECT * INTO unexpected_table FROM items",
            f'SELECT "{function}"()',
        ):
            result = await _call_json(reader, runtime_session, "akb_sql", {"vault": vault, "sql": sql}, expect_error=True)
            assert result["code"] == "permission_denied"
            assert result["details"]["pg_sqlstate"] == "25006"
        result = await _call_json(reader, runtime_session, "akb_sql", {
            "vault": vault, "sql": "SELECT 1; DELETE FROM items",
        }, expect_error=True)
        assert result["code"] == "multi_statement"
    with httpx.Client(timeout=30.0, trust_env=False) as http:
        discovery = http.get(runtime_session.descriptor.discovery_url)
        discovery.raise_for_status()
        stdio = discovery.json()["runtime"].get("stdio")
    if stdio is not None:
        executable = os.path.join(stdio["consumer_root"], "node_modules", ".bin", stdio["executable"])
        params = StdioServerParameters(
            command=executable,
            env={**os.environ, "AKB_MCP_URL": runtime_session.descriptor.mcp_url, "AKB_PAT": pat},
        )
        async with Client(params, cache=None) as client:
            result = await _call_json(client, runtime_session, "akb_sql", {
                "vault": vault, "sql": "SELECT value FROM items",
            })
            assert result["items"] == [{"value": "original"}]
            denied = await _call_json(client, runtime_session, "akb_sql", {
                "vault": vault, "sql": "DELETE FROM items",
            }, expect_error=True)
            assert denied["code"] == "permission_denied"
            assert denied["details"]["pg_sqlstate"] == "25006"
    elif os.environ.get("CI"):
        pytest.fail("CI requires the installed stdio runtime profile")
    connection = await asyncpg.connect(
        host="127.0.0.1", port=int(os.environ["AKB_E2E_POSTGRES_PORT"]),
        user="akb", password="akb", database="akb",  # pragma: allowlist secret
    )
    await connection.execute(f'DROP FUNCTION "{function}"()')
    await connection.close()
    unchanged = await _call_json(mcp_client, runtime_session, "akb_sql", {
        "vault": vault, "sql": "SELECT value FROM items",
    })
    assert unchanged["items"] == [{"value": "original"}]
    schema = await _call_json(mcp_client, runtime_session, "akb_sql", {
        "vault": vault, "sql": "SELECT to_regclass('public.unexpected_table') AS name",
    })
    assert schema["items"] == [{"name": None}]
    await _call_json(mcp_client, runtime_session, "akb_sql", {
        "vault": vault, "sql": "INSERT INTO items (value) VALUES ('after')",
    })


@pytest.mark.asyncio
async def test_read_oauth_sql_as_vault_writer(mcp_client: Client, runtime_session: RuntimeContext) -> None:
    """Verify read-only scopes on a token issued by the runtime OIDC provider."""

    with httpx.Client(timeout=30.0, trust_env=False) as http:
        discovery = http.get(runtime_session.descriptor.discovery_url)
        discovery.raise_for_status()
        oidc = discovery.json().get("oidc")
        if oidc is None:
            if os.environ.get("CI"):
                pytest.fail("CI requires the transport-oidc runtime profile")
            pytest.skip("requires the transport-oidc runtime profile")
        minted = http.post(oidc["token"]["url"], json={"variant": "insufficient_scope"})
        minted.raise_for_status()
        token = minted.json()["access_token"]

    vault = await _create_vault(mcp_client, runtime_session, "oauthsql")
    await _call_json(mcp_client, runtime_session, "akb_create_table", {
        "vault": vault, "name": "items", "columns": [{"name": "value", "type": "text"}],
    })
    await _call_json(mcp_client, runtime_session, "akb_sql", {
        "vault": vault, "sql": "INSERT INTO items (value) VALUES ('original')",
    })
    async with _open_mcp_client(
        runtime_session, pat=token, secrets=(*runtime_session.secrets, token), scenario="oauth_read_sql",
    ) as reader:
        identity = await _call_json(reader, runtime_session, "akb_whoami", {})
        no_access = await _call_json(reader, runtime_session, "akb_sql", {
            "vault": vault, "sql": "SELECT value FROM items",
        }, expect_error=True)
        _assert_permission_denied(no_access)
        await _call_json(mcp_client, runtime_session, "akb_grant", {
            "vault": vault, "user": identity["username"], "role": "reader",
        })
        result = await _call_json(reader, runtime_session, "akb_sql", {
            "vault": vault, "sql": "SELECT value FROM items",
        })
        assert result["items"] == [{"value": "original"}]
        await _call_json(mcp_client, runtime_session, "akb_grant", {
            "vault": vault, "user": identity["username"], "role": "writer",
        })
        result = await _call_json(reader, runtime_session, "akb_sql", {
            "vault": vault, "sql": "SELECT value FROM items",
        })
        assert result["items"] == [{"value": "original"}]
        denied = await _call_json(reader, runtime_session, "akb_sql", {
            "vault": vault, "sql": "DELETE FROM items",
        }, expect_error=True)
        assert denied["code"] == "permission_denied"
        assert denied["details"]["pg_sqlstate"] == "25006"
        await _call_json(mcp_client, runtime_session, "akb_revoke", {
            "vault": vault, "user": identity["username"],
        })
        revoked = await _call_json(reader, runtime_session, "akb_sql", {
            "vault": vault, "sql": "SELECT value FROM items",
        }, expect_error=True)
        _assert_permission_denied(revoked)
        await _call_json(mcp_client, runtime_session, "akb_grant", {
            "vault": vault, "user": identity["username"], "role": "writer",
        })
        await _call_json(mcp_client, runtime_session, "akb_transfer_ownership", {
            "vault": vault, "new_owner": identity["username"],
        })
        denied = await _call_json(reader, runtime_session, "akb_sql", {
            "vault": vault, "sql": "DELETE FROM items",
        }, expect_error=True)
        assert denied["code"] == "permission_denied"
        assert denied["details"]["pg_sqlstate"] == "25006"
        connection = await asyncpg.connect(
            host="127.0.0.1", port=int(os.environ["AKB_E2E_POSTGRES_PORT"]),
            user="akb", password="akb", database="akb",  # pragma: allowlist secret
        )
        await connection.execute("UPDATE users SET is_admin = true WHERE username = $1", identity["username"])
        await connection.close()
        admin = await _call_json(reader, runtime_session, "akb_whoami", {})
        assert admin["is_admin"] is True
        denied = await _call_json(reader, runtime_session, "akb_sql", {
            "vault": vault, "sql": "DELETE FROM items",
        }, expect_error=True)
        assert denied["code"] == "permission_denied"
        assert denied["details"]["pg_sqlstate"] == "25006"
    unchanged = await _call_json(mcp_client, runtime_session, "akb_sql", {
        "vault": vault, "sql": "SELECT value FROM items",
    })
    assert unchanged["items"] == [{"value": "original"}]
