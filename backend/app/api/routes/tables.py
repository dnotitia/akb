"""REST API routes for vault tables (structured data)."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response
from pydantic import ConfigDict, Field, TypeAdapter
from pydantic_core import to_json

from app.api.deps import get_current_user
from app.services.access_service import check_vault_access
from app.services.auth_service import AuthenticatedUser, token_has_scope
from app.services import (
    table_migration_service,
    table_row_query,
    table_row_write,
    table_schema_service,
    table_service,
)
from app.util.errors import (
    BULK_TOO_LARGE,
    CONFLICT,
    INVALID_COLUMN_TYPE,
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
    INVALID_COLUMN_TYPE: 400,
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


class TableRequestModel(NFCModel):
    model_config = ConfigDict(extra="allow")


class TableColumnSpec(TableRequestModel):
    name: str
    type: str | None = None
    required: bool | None = None
    default: Any = None
    check: dict[str, Any] | None = None
    enum: list[Any] | None = None
    unique: bool | None = None
    index: bool | None = None
    references: dict[str, Any] | None = None
    on_delete: str | None = None


class TableUniqueKeySpec(TableRequestModel):
    columns: list[str]
    name: str | None = None


class TableIndexColumnSpec(TableRequestModel):
    name: str
    order: Literal["asc", "desc"] | None = None


class TableIndexSpec(TableRequestModel):
    columns: list[str | TableIndexColumnSpec]
    name: str | None = None


class TableAlterColumnSpec(TableRequestModel):
    name: str
    set_default: Any = None
    default: Any = None
    drop_default: bool | None = None
    set_check: dict[str, Any] | None = None
    check: dict[str, Any] | None = None
    drop_check: bool | None = None
    set_not_null: bool | None = None
    drop_not_null: bool | None = None
    set_enum: list[Any] | None = None
    enum: list[Any] | None = None
    rename_enum_values: dict[str, str] | None = None
    enum_renames: dict[str, str] | None = None


class CreateTableRequest(NFCModel):
    name: str
    description: str = ""
    columns: list[TableColumnSpec]
    collection: str | None = None
    # Declarative unique keys / indexes (#215). Optional; mirror the MCP
    # akb_create_table surface so REST/web clients can WRITE them, not
    # just READ them back via list_tables. ValidationError/ConflictError
    # from the service map to 422/409 via the global AKBError handler.
    unique_keys: list[TableUniqueKeySpec] | None = None
    indexes: list[TableIndexSpec] | None = None
    # Opt-in idempotent create: an existing table is reported as
    # created=false instead of raising 409. The STORED schema and the
    # matches_request/mismatches divergence report accompany it only for a
    # caller that also holds READ access to the vault. Only a
    # same-vault (vault, name) row is suppressed — cross-vault physical-name
    # fusion still conflicts, because reporting it as a no-op would disclose
    # another tenant's schema.
    if_not_exists: bool = False


class AlterTableRequest(NFCModel):
    add_columns: list[TableColumnSpec] | None = None
    alter_columns: list[TableAlterColumnSpec] | None = None
    drop_columns: list[str] | None = None
    rename_columns: dict[str, str] | None = None
    add_unique_keys: list[TableUniqueKeySpec] | None = None
    drop_unique_keys: list[str] | None = None
    add_indexes: list[TableIndexSpec] | None = None
    drop_indexes: list[str] | None = None


class TableMigrationBase(TableRequestModel):
    table: str | None = None
    table_name: str | None = None


class TableNamedSpec(TableRequestModel):
    name: str


class TableAddColumnMigration(TableMigrationBase):
    op: Literal["add_column", "add-column"]
    column: TableColumnSpec | str | None = None
    name: str | None = None
    type: str | None = None
    required: bool | None = None
    default: Any = None
    check: dict[str, Any] | None = None
    enum: list[Any] | None = None
    unique: bool | None = None
    index: bool | None = None
    references: dict[str, Any] | None = None
    on_delete: str | None = None


class TableAlterColumnMigration(TableMigrationBase):
    op: Literal["alter_column", "alter-column"]
    column: TableAlterColumnSpec | str | None = None
    name: str | None = None
    set_default: Any = None
    default: Any = None
    drop_default: bool | None = None
    set_check: dict[str, Any] | None = None
    check: dict[str, Any] | None = None
    drop_check: bool | None = None
    set_not_null: bool | None = None
    drop_not_null: bool | None = None
    set_enum: list[Any] | None = None
    enum: list[Any] | None = None
    rename_enum_values: dict[str, str] | None = None
    enum_renames: dict[str, str] | None = None


class TableDropColumnMigration(TableMigrationBase):
    op: Literal["drop_column", "drop-column"]
    name: str | None = None
    column: str | TableNamedSpec | None = None


class TableRenameColumnMigration(TableMigrationBase):
    op: Literal["rename_column", "rename-column"]
    from_: str | TableNamedSpec | None = Field(default=None, alias="from")
    old_name: str | TableNamedSpec | None = None
    from_name: str | TableNamedSpec | None = None
    old: str | TableNamedSpec | None = None
    column: str | TableNamedSpec | None = None
    to: str | TableNamedSpec | None = None
    new_name: str | TableNamedSpec | None = None
    to_name: str | TableNamedSpec | None = None
    new: str | TableNamedSpec | None = None


class TableAddUniqueKeyMigration(TableMigrationBase):
    op: Literal["add_unique_key", "add-unique-key"]
    unique_key: TableUniqueKeySpec | None = None
    name: str | None = None
    columns: list[str] | None = None


class TableDropUniqueKeyMigration(TableMigrationBase):
    op: Literal["drop_unique_key", "drop-unique-key"]
    name: str | None = None
    unique_key: str | TableNamedSpec | None = None


class TableAddIndexMigration(TableMigrationBase):
    op: Literal["add_index", "add-index"]
    index: TableIndexSpec | None = None
    name: str | None = None
    columns: list[str | TableIndexColumnSpec] | None = None


class TableDropIndexMigration(TableMigrationBase):
    op: Literal["drop_index", "drop-index"]
    name: str | None = None
    index: str | TableNamedSpec | None = None


TableMigrationOperation = Annotated[
    TableAddColumnMigration
    | TableAlterColumnMigration
    | TableDropColumnMigration
    | TableRenameColumnMigration
    | TableAddUniqueKeyMigration
    | TableDropUniqueKeyMigration
    | TableAddIndexMigration
    | TableDropIndexMigration,
    Field(discriminator="op"),
]
TableMigrationOperationAdapter: TypeAdapter[TableMigrationOperation] = TypeAdapter(
    TableMigrationOperation
)


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


async def _can_read_vault(user: AuthenticatedUser, vault: str) -> bool:
    """READ authority on `vault` for this credential — see the MCP twin in
    `mcp_server/server.py`. Fail-closed: this gates a projection, never an
    action, so refusing it can only under-disclose.

    BOTH scope systems must be consulted. `token_has_scope(None, ...)` is
    True by design — `None` means an unscoped credential, i.e. a JWT login.
    But an OAuth credential ALSO carries `token_scopes=None` and keeps its
    grants in `oauth_scopes`, so checking only the former waves through an
    OAuth token holding nothing but `akb:vault:write`.
    """
    oauth = getattr(user, "oauth_scopes", None)
    if oauth is not None and "akb:vault:read" not in oauth:
        return False
    if not token_has_scope(getattr(user, "token_scopes", None), "read"):
        return False
    try:
        await check_vault_access(user.user_id, vault, required_role="reader")
    except Exception:  # noqa: BLE001 — any failure means "no read authority"
        return False
    return True


@router.post("/tables/{vault}", summary="Create a table in a vault")
async def create_table(vault: str, req: CreateTableRequest, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    payload = req.model_dump(exclude_unset=True)
    return await table_service.create_table(
        access["vault_id"], req.name, payload["columns"],
        actor_id=user.username, description=payload.get("description", req.description),
        collection=payload.get("collection"),
        unique_keys=payload.get("unique_keys"), indexes=payload.get("indexes"),
        if_not_exists=payload.get("if_not_exists", False),
        # Write authority does not imply read (token_has_scope has no
        # implication, and a managed wildcard grant can authorise a write
        # without reader membership), so the no-op projection is gated on
        # READ authority resolved here. Fail-closed: any failure → False.
        can_read_existing=await _can_read_vault(user, vault),
    )


@router.get("/tables/{vault}", summary="List tables in a vault")
async def list_tables(vault: str, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    tables = await table_service.list_tables(access["vault_id"])
    return {"kind": "table", "vault": vault, "items": tables, "total": len(tables)}


@router.get(
    "/tables/{vault}/schema",
    summary="Inspect all table schemas in a vault",
    operation_id="tablesGetVaultSchema",
)
async def get_vault_schema(vault: str, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    return await table_schema_service.get_vault_schema(access["vault_id"])


@router.post(
    "/tables/{vault}/migrations",
    summary="Apply an idempotent table schema migration",
    operation_id="tablesApplyMigration",
)
async def apply_table_migration(
    vault: str,
    operations: list[TableMigrationOperation] = Body(...),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="writer")
    return await table_migration_service.apply_table_migration(
        access["vault_id"],
        actor_id=user.username,
        idempotency_key=idempotency_key,
        operations=[
            operation.model_dump(exclude_unset=True, by_alias=True)
            for operation in operations
        ],
    )


@router.get(
    "/tables/{vault}/{table}/schema",
    summary="Inspect a table schema",
    operation_id="tablesGetTableSchema",
)
async def get_table_schema(
    vault: str,
    table: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    access = await check_vault_access(user.user_id, vault, required_role="reader")
    return await table_schema_service.get_table_schema(access["vault_id"], table)


@router.patch(
    "/tables/{vault}/{table_name}",
    summary="Alter a table schema",
    operation_id="tablesAlterTable",
)
async def alter_table(
    vault: str,
    table_name: str,
    req: AlterTableRequest,
    user: AuthenticatedUser = Depends(get_current_user),
):
    # Keep REST aligned with MCP's fail-closed policy until alter operations
    # are classified into destructive and non-destructive permission tiers.
    access = await check_vault_access(user.user_id, vault, required_role="admin")
    payload = req.model_dump(exclude_unset=True)
    return await table_service.alter_table(
        access["vault_id"], table_name,
        actor_id=user.username,
        add_columns=payload.get("add_columns"),
        alter_columns=payload.get("alter_columns"),
        drop_columns=payload.get("drop_columns"),
        rename_columns=payload.get("rename_columns"),
        add_unique_keys=payload.get("add_unique_keys"),
        drop_unique_keys=payload.get("drop_unique_keys"),
        add_indexes=payload.get("add_indexes"),
        drop_indexes=payload.get("drop_indexes"),
    )


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

    result = _raise_service_error(
        await table_service.execute_sql(
            vault_names=vaults,
            user_id=user.user_id,
            actor_id=user.username,
            sql=req.sql.strip(),
            params=req.params,
            is_admin=user.is_admin,
        )
    )
    # Serialise with pydantic-core (Rust) instead of FastAPI's default
    # jsonable_encoder + json.dumps: a large `akb_sql` result (rows already
    # coerced to JSON-native types + NaN→null in user_sql_executor) encodes in
    # one fast pass (~0.4s for a 205MB result) that keeps the single event-loop
    # block well under the /livez probe timeout. Errors were raised above,
    # before this point, so we only ever serialise a success envelope.
    # inf_nan_mode="null" so a PG float8 NaN/±Inf in the result serialises to
    # `null` (valid JSON) instead of a bare NaN/Infinity token, in the Rust pass.
    return Response(to_json(result, inf_nan_mode="null"), media_type="application/json")


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
        actor_id=user.username,
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
    status_code=201,
    response_model=TableQueryResponse,
    response_model_exclude_none=True,
    responses={
        204: {"description": "Rows inserted without a response body."},
    },
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
    response_model=TableQueryResponse,
    response_model_exclude_none=True,
    responses={
        204: {"description": "Rows updated without a response body."},
    },
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
        actor_id=user.username,
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
    response_model=TableQueryResponse,
    response_model_exclude_none=True,
    responses={
        204: {"description": "Rows deleted without a response body."},
    },
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
        actor_id=user.username,
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
    responses={
        201: {"model": TableQueryResponse, "description": "Rows inserted by a write AST."},
        204: {"description": "Rows mutated by a write AST without a response body."},
    },
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
    ast = req.model_dump(exclude_none=True)
    if table_row_write.is_write_ast(ast):
        access = await check_vault_access(user.user_id, vault, required_role="writer")
        write_result = await table_row_write.query_rows(
            vault_name=vault,
            vault_id=access["vault_id"],
            table_name=table,
            user_id=user.user_id,
            actor_id=user.username,
            is_admin=user.is_admin,
            ast=ast,
            prefer_header=request.headers.get("prefer"),
        )
        if isinstance(write_result, table_row_write.RowMutationResponse):
            return _apply_row_mutation_response(write_result, response)
        return _raise_service_error(write_result)
    read_result = await table_row_query.query_rows(
        vault_name=vault,
        vault_id=access["vault_id"],
        table_name=table,
        user_id=user.user_id,
        actor_id=user.username,
        is_admin=user.is_admin,
        ast=ast,
        range_header=request.headers.get("range"),
        prefer_header=request.headers.get("prefer"),
    )
    if isinstance(read_result, table_row_query.RowQueryResponse):
        if read_result.content_range is not None:
            response.headers["Content-Range"] = read_result.content_range
        return read_result.body
    return _raise_service_error(read_result)


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


@router.delete(
    "/tables/{vault}/{table_name}",
    summary="Drop a table",
    operation_id="tablesDeleteTableName",
)
async def drop_table(vault: str, table_name: str, user: AuthenticatedUser = Depends(get_current_user)):
    access = await check_vault_access(user.user_id, vault, required_role="admin")
    return await table_service.drop_table(
        access["vault_id"], table_name, actor_id=user.username,
    )
