from typing import Any

import asyncpg
import httpx
import pytest

from app.repositories.table_data_repo import pg_table_name
from app.services.role_sync import vault_group_role_name
from tests.rest_integration.helpers import Actor, register_user
from tests.rest_integration.security_helpers import bearer, mcp_json

pytestmark = pytest.mark.live_services


async def create_vault(
    api: httpx.AsyncClient, actor: Actor, vault: str, public_access: str = "none"
) -> str:
    created = await mcp_json(api, actor.pat, "akb_create_vault", name=vault, public_access=public_access)
    return created["vault_id"]


async def create_table(
    api: httpx.AsyncClient, actor: Actor, vault: str, table: str, **columns: str
) -> None:
    created = await mcp_json(
        api,
        actor.pat,
        "akb_create_table",
        vault=vault,
        name=table,
        columns=[{"name": name, "type": kind} for name, kind in columns.items()],
    )
    assert created["name"] == table


async def run_sql(api: httpx.AsyncClient, token: str, vault: str, sql: str) -> dict[str, Any]:
    return await mcp_json(api, token, "akb_sql", vault=vault, sql=sql)


def assert_pg_denial(result: dict[str, Any], statement: str) -> None:
    assert result.get("code") == "permission_denied", (statement, result)
    assert result["details"]["pg_sqlstate"] == "42501", (statement, result)


async def test_private_vault_membership_projects_to_postgres_roles(
    api: httpx.AsyncClient, pg_pool: asyncpg.Pool
) -> None:
    owner = await register_user(api, label="rbac-owner")
    member = await register_user(api, label="rbac-member")
    vault = "rbac-private"
    other_vault = "rbac-delete-role"
    await create_vault(api, owner, vault)
    other_id = await create_vault(api, owner, other_vault)
    await create_table(api, owner, other_vault, "notes", topic="text")
    assert "result" in await run_sql(api, owner.pat, other_vault, "INSERT INTO notes (topic) VALUES ('hello')")
    await create_table(api, owner, vault, "records", label="text", value="text")
    seed = "INSERT INTO records (label, value) VALUES ('one', 'first'), ('two', 'second')"
    assert "result" in await run_sql(api, owner.pat, vault, seed)

    count = "SELECT count(*) AS count FROM records"
    assert (await run_sql(api, owner.pat, vault, count))["items"] == [{"count": 2}]
    owner_insert = "INSERT INTO records (label, value) VALUES ('three', 'third')"
    assert "result" in await run_sql(api, owner.pat, vault, owner_insert)

    async def grant(role: str) -> None:
        reply = await mcp_json(api, owner.pat, "akb_grant", vault=vault, user=member.username, role=role)
        assert reply["granted"] is True

    async def revoke() -> None:
        reply = await mcp_json(api, owner.pat, "akb_revoke", vault=vault, user=member.username)
        assert reply["revoked"] is True
        denied = await run_sql(api, member.pat, vault, count)
        assert denied["code"] == "permission_denied", denied
        assert "Requires 'reader' role" in denied["error"]

    await grant("reader")
    assert (await run_sql(api, member.pat, vault, count))["items"] == [{"count": 3}]

    await grant("writer")
    member_insert = "INSERT INTO records (label, value) VALUES ('four', 'fourth')"
    assert "result" in await run_sql(api, member.pat, vault, member_insert)
    await revoke()

    await grant("reader")
    for statement in (
        "INSERT INTO records (label, value) VALUES ('blocked', 'value')",
        "UPDATE records SET value = 'blocked' WHERE label = 'one'",
        "DELETE FROM records WHERE label = 'one'",
    ):
        assert_pg_denial(await run_sql(api, member.pat, vault, statement), statement)
    truncate = await run_sql(api, member.pat, vault, "TRUNCATE records")
    assert truncate["code"] == "method_not_allowed", truncate
    await revoke()

    await grant("reader")
    assert (await run_sql(api, member.pat, vault, count))["items"] == [{"count": 4}]
    await revoke()

    roles = [vault_group_role_name(other_id, scope) for scope in ("reader", "writer", "admin")]
    role_count = "SELECT count(*) FROM pg_roles WHERE rolname = ANY($1::text[])"
    assert await pg_pool.fetchval(role_count, roles) == 3
    deleted = await mcp_json(api, owner.pat, "akb_delete_vault", vault=other_vault)
    assert deleted["deleted"] is True
    assert await pg_pool.fetchval(role_count, roles) == 0
    missing = await run_sql(api, owner.pat, other_vault, "SELECT 1")
    assert missing["code"] == "not_found", missing


async def test_sql_changes_public_access_for_existing_and_new_users(api: httpx.AsyncClient) -> None:
    owner = await register_user(api, label="rbac-public-owner")
    visitor = await register_user(api, label="rbac-public-visitor")
    vault = "rbac-public"
    await create_vault(api, owner, vault, public_access="reader")
    await create_table(api, owner, vault, "items", label="text")
    seed = "INSERT INTO items (label) VALUES ('one'), ('two')"
    assert "result" in await run_sql(api, owner.pat, vault, seed)

    select = "SELECT label FROM items ORDER BY label"
    assert (await run_sql(api, visitor.pat, vault, select))["items"] == [{"label": "one"}, {"label": "two"}]
    insert = "INSERT INTO items (label) VALUES ('three')"
    assert_pg_denial(await run_sql(api, visitor.pat, vault, insert), insert)

    async def set_public(level: str) -> None:
        response = await mcp_json(api, owner.pat, "akb_set_public", vault=vault, level=level)
        assert response["public_access"] == level

    await set_public("writer")
    assert "result" in await run_sql(api, visitor.pat, vault, insert)
    await set_public("none")
    denied = await run_sql(api, visitor.pat, vault, select)
    assert denied["code"] == "permission_denied", denied
    assert "Requires 'reader' role" in denied["error"]

    await set_public("reader")
    newcomer = await register_user(api, label="rbac-public-newcomer")
    result = await run_sql(api, newcomer.pat, vault, "SELECT count(*) AS count FROM items")
    assert result["items"] == [{"count": 3}]


async def test_postgres_enforces_cross_vault_and_system_acl(api: httpx.AsyncClient) -> None:
    owner = await register_user(api, label="rbac-acl-owner")
    other = await register_user(api, label="rbac-acl-other")
    private = "rbac-acl-private"
    own_vault = "rbac-acl-other"
    await create_vault(api, owner, private)
    await create_vault(api, other, own_vault)
    await create_table(api, owner, private, "records", label="text", value="text")
    seed = "INSERT INTO records (label, value) VALUES ('canary', 'sample')"
    assert "result" in await run_sql(api, owner.pat, private, seed)
    assert (await run_sql(api, other.pat, own_vault, "SELECT 1 AS n"))["items"] == [{"n": 1}]

    physical = pg_table_name(private, "records")
    for statement in (
        f"SELECT * FROM {physical}",
        f"SELECT value FROM {physical} WHERE label = 'canary'",
        f"WITH sample AS (SELECT * FROM {physical}) SELECT * FROM sample",
        f"SELECT * FROM public.{physical}",
        f'SELECT * FROM "{physical}"',
        f"SELECT 1 UNION ALL SELECT 1 FROM {physical}",
        f"SELECT * FROM (SELECT * FROM {physical}) AS sample",
        f"SELECT EXISTS (SELECT 1 FROM {physical})",
        f"SELECT (SELECT value FROM {physical} LIMIT 1) AS sample",
        f"SELECT /* label */ * FROM {physical} /* trailing */",
        f"SELECT *FROM/**/{physical}",
        f"WITH added AS (INSERT INTO {physical} (label, value) VALUES ('blocked','sample') RETURNING id)"
        " SELECT * FROM added",
        "SELECT id FROM users WHERE false",
        "SELECT rolname FROM pg_authid WHERE false",
        "SELECT 1 FROM vault_access WHERE false",
        "SELECT pg_read_file('/nonexistent/akb-test/acl-canary')",
        "SELECT pg_ls_dir('/nonexistent/akb-test')",
        "SELECT lo_export(1, '/nonexistent/akb-test/acl-canary')",
    ):
        assert_pg_denial(await run_sql(api, other.pat, own_vault, statement), statement)

    catalog = await run_sql(
        api, other.pat, own_vault, f"SELECT relname FROM pg_catalog.pg_class WHERE relname = '{physical}'"
    )
    assert catalog["items"] == [{"relname": physical}]


async def test_sql_preflight_rejects_unsupported_and_multiple_statements(api: httpx.AsyncClient) -> None:
    owner = await register_user(api, label="rbac-sql-preflight")
    vault = "rbac-sql-preflight"
    await create_vault(api, owner, vault)
    await create_table(api, owner, vault, "records", label="text")

    for statement in (
        "COPY (SELECT 1) TO PROGRAM 'akb-test-nonexistent-program'",
        "SET ROLE akb_test_nonexistent_role",
        "DO $$ BEGIN PERFORM 1; END $$",
        "CREATE FUNCTION test_callback() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql",
        "TRUNCATE records",
    ):
        rejected = await run_sql(api, owner.pat, vault, statement)
        assert rejected["code"] == "method_not_allowed", (statement, rejected)
        assert "Only SELECT / WITH / INSERT / UPDATE / DELETE" in rejected["error"]

    multiple = await run_sql(api, owner.pat, vault, "SELECT 1; SELECT * FROM records")
    assert multiple["code"] == "multi_statement", multiple
    assert "Multi-statement" in multiple["error"]


async def test_scoped_pat_uses_its_postgres_role_even_when_user_owns_both_vaults(api: httpx.AsyncClient) -> None:
    owner = await register_user(api, label="rbac-scoped-owner")
    response = await api.post(
        "/api/v1/auth/tokens",
        headers=bearer(owner.jwt),
        json={"name": "scoped-sql", "vault_scope": {"prefixes": ["gdn-"], "extra_vaults": []}},
    )
    assert response.status_code == 200, response.text
    scoped_pat = response.json()["token"]
    inside = "gdn-sql-inside"
    outside = "scoped-sql-outside"
    for vault in (inside, outside):
        await create_vault(api, owner, vault)
        await create_table(api, owner, vault, "items", value="text")
        assert "result" in await run_sql(api, owner.pat, vault, "INSERT INTO items (value) VALUES ('seed')")

    select = "SELECT value FROM items ORDER BY value"
    insert = "INSERT INTO items (value) VALUES ('new')"
    assert (await run_sql(api, scoped_pat, inside, select))["items"] == [{"value": "seed"}]
    assert "result" in await run_sql(api, scoped_pat, inside, insert)
    assert_pg_denial(await run_sql(api, scoped_pat, outside, insert), insert)
    assert_pg_denial(await run_sql(api, scoped_pat, outside, select), select)
    assert (await run_sql(api, owner.pat, outside, select))["items"] == [{"value": "seed"}]
    assert "result" in await run_sql(api, owner.pat, outside, insert)


async def test_cross_vault_sql_join_and_row_mutations(api: httpx.AsyncClient) -> None:
    owner = await register_user(api, label="sql-join-owner")
    products = "join-products"
    orders = "join-orders"
    await create_vault(api, owner, products)
    await create_vault(api, owner, orders)
    await create_table(api, owner, products, "products", name="text", price="number")
    await create_table(api, owner, orders, "orders", product="text", qty="number")
    product_seed = "INSERT INTO products (name, price) VALUES ('Widget', 100), ('Gadget', 200)"
    order_seed = "INSERT INTO orders (product, qty) VALUES ('Widget', 5), ('Gadget', 3)"
    assert "result" in await run_sql(api, owner.pat, products, product_seed)
    assert "result" in await run_sql(api, owner.pat, orders, order_seed)

    joined = await mcp_json(
        api,
        owner.pat,
        "akb_sql",
        vaults=[products, orders],
        sql="SELECT p.name, p.price, o.qty FROM join_products__products p "
        "JOIN join_orders__orders o ON p.name = o.product ORDER BY p.name",
    )
    assert joined["items"] == [
        {"name": "Gadget", "price": "200", "qty": "3"},
        {"name": "Widget", "price": "100", "qty": "5"},
    ]

    update = await run_sql(api, owner.pat, products, "UPDATE products SET price = 150 WHERE name = 'Widget'")
    assert update["result"] == "UPDATE 1"
    current = await run_sql(api, owner.pat, products, "SELECT price FROM products WHERE name = 'Widget'")
    assert current["items"] == [{"price": "150"}]

    delete = await run_sql(api, owner.pat, products, "DELETE FROM products WHERE name = 'Gadget'")
    assert delete["result"] == "DELETE 1"
    remaining = await run_sql(api, owner.pat, products, "SELECT count(*) AS count FROM products")
    assert remaining["items"] == [{"count": 1}]
