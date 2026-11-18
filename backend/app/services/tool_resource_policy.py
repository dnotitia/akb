"""Access scope for each operation on the MCP OAuth resource."""

from collections.abc import Mapping

READ_SCOPE = "akb:vault:read"
WRITE_SCOPE = "akb:vault:write"
ADMITTED_SCOPES = frozenset((READ_SCOPE, WRITE_SCOPE))

DCR_ADMISSION_CLAIM = "akb_tool_client"
DCR_ADMISSION_VALUE = "admitted"

MCP_OPERATION_SCOPES: dict[str, str] = {
    # --- read ---
    "akb_help": READ_SCOPE,
    "akb_whoami": READ_SCOPE,
    "akb_list_vaults": READ_SCOPE,
    "akb_vault_info": READ_SCOPE,
    "akb_vault_members": READ_SCOPE,
    # A read: it reports the reasons behind a role, and reporting a reason is
    # never authority to change one.
    "akb_explain_access": READ_SCOPE,
    "akb_search_users": READ_SCOPE,
    "akb_browse": READ_SCOPE,
    "akb_get": READ_SCOPE,
    "akb_search": READ_SCOPE,
    "akb_grep": READ_SCOPE,
    "akb_drill_down": READ_SCOPE,
    "akb_activity": READ_SCOPE,
    "akb_diff": READ_SCOPE,
    "akb_history": READ_SCOPE,
    "akb_relations": READ_SCOPE,
    "akb_graph": READ_SCOPE,
    "akb_provenance": READ_SCOPE,
    "akb_publications": READ_SCOPE,
    "akb_export": READ_SCOPE,
    # --- write ---
    "akb_publication_snapshot": WRITE_SCOPE,
    "akb_put": WRITE_SCOPE,
    "akb_update": WRITE_SCOPE,
    "akb_move": WRITE_SCOPE,
    "akb_edit": WRITE_SCOPE,
    "akb_delete": WRITE_SCOPE,
    "akb_link": WRITE_SCOPE,
    "akb_unlink": WRITE_SCOPE,
    "akb_create_collection": WRITE_SCOPE,
    "akb_delete_collection": WRITE_SCOPE,
    "akb_create_vault": WRITE_SCOPE,
    "akb_delete_vault": WRITE_SCOPE,
    "akb_archive_vault": WRITE_SCOPE,
    "akb_create_table": WRITE_SCOPE,
    "akb_alter_table": WRITE_SCOPE,
    "akb_drop_table": WRITE_SCOPE,
    "akb_sql": WRITE_SCOPE,
    "akb_grant": WRITE_SCOPE,
    "akb_revoke": WRITE_SCOPE,
    "akb_publish": WRITE_SCOPE,
    "akb_unpublish": WRITE_SCOPE,
    "akb_set_public": WRITE_SCOPE,
    "akb_transfer_ownership": WRITE_SCOPE,
    "akb_import": WRITE_SCOPE,
}

REST_OPERATION_SCOPES = {
    "file.reserve_upload": WRITE_SCOPE,
    "file.confirm_upload": WRITE_SCOPE,
    "file.reserve_replacement": WRITE_SCOPE,
    "file.confirm_replacement": WRITE_SCOPE,
    "file.download": READ_SCOPE,
    "file.delete": WRITE_SCOPE,
    "asset.upload": WRITE_SCOPE,
    "asset.discard": WRITE_SCOPE,
}

ARG_WRITE_TRIGGERS = {"akb_grep": ("replace",)}
_OPERATION_SCOPES = MCP_OPERATION_SCOPES | REST_OPERATION_SCOPES


def required_oauth_scope(
    operation: str,
    arguments: Mapping[str, object],
    *,
    read_only_sql: bool = False,
) -> str | None:
    if operation == "akb_sql" and read_only_sql:
        return READ_SCOPE
    if any(arguments.get(arg) is not None for arg in ARG_WRITE_TRIGGERS.get(operation, ())):
        return WRITE_SCOPE
    return _OPERATION_SCOPES.get(operation)


def operation_may_write(operation: str) -> bool:
    return _OPERATION_SCOPES.get(operation) == WRITE_SCOPE or operation in ARG_WRITE_TRIGGERS
