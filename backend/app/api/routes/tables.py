"""REST API routes for vault tables (structured data)."""

from fastapi import APIRouter, Depends

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
    collection: str | None = None
    # Declarative unique keys / indexes (#215). Optional; mirror the MCP
    # akb_create_table surface so REST/web clients can WRITE them, not
    # just READ them back via list_tables. ValidationError/ConflictError
    # from the service map to 422/409 via the global AKBError handler.
    unique_keys: list[dict] | None = None
    indexes: list[dict] | None = None


class SqlRequest(NFCModel):
    sql: str
    vaults: list[str] | None = None


@router.post("/tables/{vault}", summary="Create a table in a vault")
async def create_table(vault: str, req: CreateTableRequest, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    return await table_service.create_table(
        access["vault_id"], req.name, req.columns,
        actor_id=user.username, description=req.description,
        collection=req.collection,
        unique_keys=req.unique_keys, indexes=req.indexes,
    )


@router.get("/tables/{vault}", summary="List tables in a vault")
async def list_tables(vault: str, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    tables = await table_service.list_tables(access["vault_id"])
    return {"kind": "table", "vault": vault, "items": tables, "total": len(tables)}


@router.post("/tables/{vault}/sql", summary="Execute SQL on vault tables")
async def execute_sql(vault: str, req: SqlRequest, user: AuthenticatedUser = Depends(get_current_user)):
    vaults = req.vaults or [vault]

    # Check access — minimum reader. This is the application's friendly
    # 403 gate; if the user has no membership at all on a referenced
    # vault, we fail fast here rather than letting PG return permission-
    # denied later. Per-statement read/write enforcement (no INSERT for
    # reader role, etc.) is handled by PG ACL via the user's role
    # memberships — no explicit read-only TX needed any more.
    for v in vaults:
        await check_vault_access(user.user_id, v, required_role="reader")

    return await table_service.execute_sql(
        vault_names=vaults,
        user_id=user.user_id,
        sql=req.sql.strip(),
        is_admin=user.is_admin,
    )


@router.delete("/tables/{vault}/{table_name}", summary="Drop a table")
async def drop_table(vault: str, table_name: str, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="admin")
    return await table_service.drop_table(
        access["vault_id"], table_name, actor_id=user.username,
    )
