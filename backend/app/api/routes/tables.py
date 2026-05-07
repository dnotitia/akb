"""REST API routes for vault tables (structured data)."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import get_current_user
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services import table_service
from app.util.text import NFCModel

router = APIRouter()


class CreateTableRequest(NFCModel):
    name: str
    description: str = ""
    columns: list[dict]


class SqlRequest(NFCModel):
    sql: str
    vaults: list[str] | None = None


@router.post("/tables/{vault}", summary="Create a table in a vault")
async def create_table(vault: str, req: CreateTableRequest, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    return await table_service.create_table(
        access["vault_id"], req.name, req.columns, req.description, user.username,
    )


@router.get("/tables/{vault}", summary="List tables in a vault")
async def list_tables(vault: str, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    tables = await table_service.list_tables(access["vault_id"])
    return {"tables": tables}


@router.post("/tables/{vault}/sql", summary="Execute SQL on vault tables")
async def execute_sql(vault: str, req: SqlRequest, user: AuthenticatedUser = Depends(get_current_user)):
    vaults = req.vaults or [vault]

    # Check access — minimum reader. Collect role to enforce read-only at DB level.
    read_only = False
    for v in vaults:
        access = await check_vault_access(user.user_id, v, required_role="reader")
        if access["role"] == "reader":
            read_only = True

    return await table_service.execute_sql(vaults, req.sql.strip(), read_only=read_only)


@router.delete("/tables/{vault}/{table_name}", summary="Drop a table")
async def drop_table(vault: str, table_name: str, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="admin")
    from app.db.postgres import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        v = await conn.fetchrow("SELECT name FROM vaults WHERE id = $1", access["vault_id"])
        table = await conn.fetchrow(
            "SELECT id FROM vault_tables WHERE vault_id = $1 AND name = $2",
            access["vault_id"], table_name,
        )
        if not table:
            raise HTTPException(status_code=404, detail=f"Table not found: {table_name}")
        pg_name = table_service._pg_table_name(v["name"], table_name)
        await conn.execute(f"DROP TABLE IF EXISTS {pg_name}")
        await conn.execute("DELETE FROM vault_tables WHERE id = $1", table["id"])
    await table_service.delete_table_index(str(table["id"]))
    return {"dropped": True, "table": table_name}
