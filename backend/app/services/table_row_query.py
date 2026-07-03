"""PostgREST-style row-read entry points for vault tables.

Thin orchestration facade: resolve the table, delegate query compilation
to the querystring pipeline (`row_query_string`) or the JSON-AST pipeline
(`row_query_ast`), run the compiled SELECT through the user-scoped SQL
executor, and shape the result (`row_query_shape`). Shared compile
primitives live in `row_query_base`.

`RowQueryResponse`, `compile_row_query`, and `compile_ast_row_query` are
re-exported here so existing callers and tests keep importing them from
`table_row_query`.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping, Sequence

import asyncpg

from app.db.postgres import get_pool
from app.exceptions import NotFoundError
from app.repositories import table_registry_repo
from app.services.user_sql_executor import PermissionDeniedError, get_user_sql_executor
from app.util.errors import PERMISSION_DENIED, SQL_ERROR, err
from app.services.row_query_ast import compile_ast_row_query
from app.services.row_query_base import RowQueryResponse
from app.services.row_query_shape import _shape_result
from app.services.row_query_string import compile_row_query


async def select_rows(
    *,
    vault_name: str,
    vault_id: uuid.UUID,
    table_name: str,
    user_id: uuid.UUID | str,
    is_admin: bool = False,
    query_params: Sequence[tuple[str, str]] = (),
    range_header: str | None = None,
    prefer_header: str | None = None,
) -> RowQueryResponse | dict[str, Any]:
    """Compile URL query params into one parameterized SELECT and execute it."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        if not vault:
            raise NotFoundError("Vault", str(vault_id))
        table = await table_registry_repo.find_by_name(conn, vault_id, table_name)
        if not table:
            raise NotFoundError("Table", table_name)
        columns = table_registry_repo.parse_columns(table["columns"])

    compiled_or_error = compile_row_query(
        vault_name=vault_name,
        table_name=table_name,
        columns=columns,
        query_params=query_params,
        range_header=range_header,
        prefer_header=prefer_header,
    )
    if isinstance(compiled_or_error, dict) and "error" in compiled_or_error:
        return compiled_or_error
    compiled = compiled_or_error

    try:
        result = await get_user_sql_executor().execute(
            user_id=user_id,
            sql=compiled["sql"],
            params=compiled["params"],
            is_admin=is_admin,
            vault_names=[vault_name],
        )
    except PermissionDeniedError as e:
        return err(str(e), code=PERMISSION_DENIED, pg_sqlstate=e.pg_sqlstate)
    except asyncpg.PostgresError as e:
        return err(str(e), code=SQL_ERROR, pg_sqlstate=getattr(e, "sqlstate", None))

    body, content_range = _shape_result(
        result,
        vault_name=vault_name,
        table_name=table_name,
        projections=compiled["projections"],
        count_exact=compiled["count_exact"],
        offset=compiled["page"].offset,
    )
    return RowQueryResponse(body=body, content_range=content_range)


async def query_rows(
    *,
    vault_name: str,
    vault_id: uuid.UUID,
    table_name: str,
    user_id: uuid.UUID | str,
    is_admin: bool = False,
    ast: Mapping[str, Any],
    range_header: str | None = None,
    prefer_header: str | None = None,
) -> RowQueryResponse | dict[str, Any]:
    """Compile a JSON read AST into one parameterized SELECT and execute it."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        vault = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", vault_id)
        if not vault:
            raise NotFoundError("Vault", str(vault_id))
        table = await table_registry_repo.find_by_name(conn, vault_id, table_name)
        if not table:
            raise NotFoundError("Table", table_name)
        columns = table_registry_repo.parse_columns(table["columns"])

    compiled_or_error = compile_ast_row_query(
        vault_name=vault_name,
        table_name=table_name,
        columns=columns,
        ast=ast,
        range_header=range_header,
        prefer_header=prefer_header,
    )
    if isinstance(compiled_or_error, dict) and "error" in compiled_or_error:
        return compiled_or_error
    compiled = compiled_or_error

    try:
        result = await get_user_sql_executor().execute(
            user_id=user_id,
            sql=compiled["sql"],
            params=compiled["params"],
            is_admin=is_admin,
            vault_names=[vault_name],
        )
    except PermissionDeniedError as e:
        return err(str(e), code=PERMISSION_DENIED, pg_sqlstate=e.pg_sqlstate)
    except asyncpg.PostgresError as e:
        return err(str(e), code=SQL_ERROR, pg_sqlstate=getattr(e, "sqlstate", None))

    body, content_range = _shape_result(
        result,
        vault_name=vault_name,
        table_name=table_name,
        projections=compiled["projections"],
        count_exact=compiled["count_exact"],
        offset=compiled["page"].offset,
    )
    return RowQueryResponse(body=body, content_range=content_range)
