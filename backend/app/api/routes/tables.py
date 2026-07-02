"""REST API routes for vault tables (structured data)."""

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import ConfigDict

from app.api.deps import get_current_user
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser
from app.services import table_row_query, table_row_write, table_service
from app.util.errors import (
    BULK_TOO_LARGE,
    CONFLICT,
    INVALID_ARGUMENT,
    METHOD_NOT_ALLOWED,
    MULTI_STATEMENT,
    NO_UNIQUE_CONSTRAINT,
    PERMISSION_DENIED,
    SQL_ERROR,
    UNFILTERED_MUTATION,
    UNDEFINED_COLUMN,
    UNDEFINED_TABLE,
    UNIQUE_VIOLATION,
    VAULT_ARCHIVED,
)
from app.util.text import NFCModel

router = APIRouter()

_SERVICE_ERROR_STATUS = {
    INVALID_ARGUMENT: 400,
    METHOD_NOT_ALLOWED: 400,
    MULTI_STATEMENT: 400,
    SQL_ERROR: 400,
    BULK_TOO_LARGE: 400,
    NO_UNIQUE_CONSTRAINT: 400,
    UNFILTERED_MUTATION: 400,
    UNDEFINED_COLUMN: 400,
    UNDEFINED_TABLE: 400,
    PERMISSION_DENIED: 403,
    CONFLICT: 409,
    UNIQUE_VIOLATION: 409,
    VAULT_ARCHIVED: 409,
}


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
    params: list[Any] | None = None
    vaults: list[str] | None = None


class QueryRowsRequest(NFCModel):
    model_config = ConfigDict(extra="allow")

    select: Any | None = None
    filter: Any | None = None
    where: Any | None = None
    order: Any | None = None
    limit: int | None = None
    offset: int | None = None
    page: dict[str, Any] | None = None
    count: bool | str | None = None


class TableQueryResponse(NFCModel):
    kind: Literal["table_query"]
    vault: str | None = None
    table: str | None = None
    vaults: list[str] | None = None
    columns: list[str]
    items: list[dict[str, Any]]
    total: int


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

    return _raise_service_error(
        await table_service.execute_sql(
            vault_names=vaults,
            user_id=user.user_id,
            sql=req.sql.strip(),
            params=req.params,
            is_admin=user.is_admin,
        )
    )


@router.get(
    "/tables/{vault}/{table}/rows",
    summary="Select rows from a vault table",
    operation_id="tablesSelectRows",
    response_model=TableQueryResponse,
    response_model_exclude_none=True,
)
async def select_rows(
    vault: str,
    table: str,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    result = await table_row_query.select_rows(
        vault_name=vault,
        vault_id=access["vault_id"],
        table_name=table,
        user_id=user.user_id,
        is_admin=user.is_admin,
        query_params=list(request.query_params.multi_items()),
        range_header=request.headers.get("range"),
        prefer_header=request.headers.get("prefer"),
    )
    if isinstance(result, table_row_query.RowQueryResponse):
        if result.content_range is not None:
            response.headers["Content-Range"] = result.content_range
        return result.body
    return _raise_service_error(result)


@router.post(
    "/tables/{vault}/{table}/rows",
    summary="Insert rows into a vault table",
    operation_id="tablesInsertRows",
)
async def insert_rows(
    vault: str,
    table: str,
    request: Request,
    response: Response,
    body: Any = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    result = await table_row_write.insert_rows(
        vault_name=vault,
        vault_id=access["vault_id"],
        table_name=table,
        user_id=user.user_id,
        actor_id=user.username,
        body=body,
        is_admin=user.is_admin,
        query_params=list(request.query_params.multi_items()),
        prefer_header=request.headers.get("prefer"),
    )
    if isinstance(result, table_row_write.RowMutationResponse):
        return _apply_row_mutation_response(result, response)
    return _raise_service_error(result)


@router.patch(
    "/tables/{vault}/{table}/rows",
    summary="Update rows in a vault table",
    operation_id="tablesUpdateRows",
)
async def update_rows(
    vault: str,
    table: str,
    request: Request,
    response: Response,
    body: Any = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    result = await table_row_write.update_rows(
        vault_name=vault,
        vault_id=access["vault_id"],
        table_name=table,
        user_id=user.user_id,
        body=body,
        is_admin=user.is_admin,
        query_params=list(request.query_params.multi_items()),
        prefer_header=request.headers.get("prefer"),
    )
    if isinstance(result, table_row_write.RowMutationResponse):
        return _apply_row_mutation_response(result, response)
    return _raise_service_error(result)


@router.delete(
    "/tables/{vault}/{table}/rows",
    summary="Delete rows from a vault table",
    operation_id="tablesDeleteRows",
)
async def delete_rows(
    vault: str,
    table: str,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    result = await table_row_write.delete_rows(
        vault_name=vault,
        vault_id=access["vault_id"],
        table_name=table,
        user_id=user.user_id,
        is_admin=user.is_admin,
        query_params=list(request.query_params.multi_items()),
        prefer_header=request.headers.get("prefer"),
    )
    if isinstance(result, table_row_write.RowMutationResponse):
        return _apply_row_mutation_response(result, response)
    return _raise_service_error(result)


@router.post(
    "/tables/{vault}/{table}/query",
    summary="Select rows from a vault table using JSON AST",
    operation_id="tablesQueryRows",
    response_model=TableQueryResponse,
    response_model_exclude_none=True,
)
async def query_rows(
    vault: str,
    table: str,
    req: QueryRowsRequest,
    request: Request,
    response: Response,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    result = await table_row_query.query_rows(
        vault_name=vault,
        vault_id=access["vault_id"],
        table_name=table,
        user_id=user.user_id,
        is_admin=user.is_admin,
        ast=req.model_dump(exclude_none=True),
        range_header=request.headers.get("range"),
        prefer_header=request.headers.get("prefer"),
    )
    if isinstance(result, table_row_query.RowQueryResponse):
        if result.content_range is not None:
            response.headers["Content-Range"] = result.content_range
        return result.body
    return _raise_service_error(result)


def _apply_row_mutation_response(
    result: table_row_write.RowMutationResponse,
    response: Response,
) -> Any:
    headers = {}
    if result.content_range is not None:
        headers["Content-Range"] = result.content_range
    if result.body is None:
        return Response(status_code=result.status_code, headers=headers)
    response.status_code = result.status_code
    for key, value in headers.items():
        response.headers[key] = value
    return result.body


def _raise_service_error(result: Any) -> Any:
    """Translate legacy service err() dicts to HTTP AkbError responses.

    The MCP surface still passes ``err(...)`` dictionaries through as tool
    output. REST should expose errors through status codes so SDK boundary
    code can map every non-2xx response to the single AkbError contract.
    """
    if not isinstance(result, dict) or "kind" in result:
        return result
    code = result.get("code")
    message = result.get("message") or result.get("error")
    if not isinstance(code, str) or not isinstance(message, str):
        return result
    detail: dict[str, Any] = {"message": message, "code": code}
    if isinstance(result.get("hint"), str):
        detail["hint"] = result["hint"]
    if "details" in result:
        detail["details"] = result["details"]
    raise HTTPException(
        status_code=_SERVICE_ERROR_STATUS.get(code, 400),
        detail=detail,
    )


@router.delete("/tables/{vault}/{table_name}", summary="Drop a table")
async def drop_table(vault: str, table_name: str, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="admin")
    return await table_service.drop_table(
        access["vault_id"], table_name, actor_id=user.username,
    )
