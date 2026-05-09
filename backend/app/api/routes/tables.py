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
        access["vault_id"], req.name, req.columns,
        actor_id=user.username, description=req.description,
    )


@router.get("/tables/{vault}", summary="List tables in a vault")
async def list_tables(vault: str, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    tables = await table_service.list_tables(access["vault_id"])
    return {"kind": "table", "vault": vault, "items": tables, "total": len(tables)}


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
    return await table_service.drop_table(
        access["vault_id"], table_name, actor_id=user.username,
    )
