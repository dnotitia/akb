"""Test-side contract for the two sequential MCP source catalogs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


LOGICAL_OPERATIONS = (
    "akb_list_vaults",
    "akb_create_vault",
    "akb_put",
    "akb_get",
    "akb_update",
    "akb_edit",
    "akb_move",
    "akb_delete",
    "akb_browse",
    "akb_search",
    "akb_grep",
    "akb_drill_down",
    "akb_activity",
    "akb_diff",
    "akb_relations",
    "akb_graph",
    "akb_link",
    "akb_unlink",
    "akb_provenance",
    "akb_create_table",
    "akb_sql",
    "akb_drop_table",
    "akb_alter_table",
    "akb_publish",
    "akb_unpublish",
    "akb_publications",
    "akb_publication_snapshot",
    "akb_vault_info",
    "akb_vault_members",
    "akb_grant",
    "akb_revoke",
    "akb_explain_access",
    "akb_search_users",
    "akb_whoami",
    "akb_transfer_ownership",
    "akb_archive_vault",
    "akb_delete_vault",
    "akb_create_collection",
    "akb_delete_collection",
    "akb_set_public",
    "akb_history",
    "akb_help",
    "akb_export",
    "akb_import",
    "akb_grep_replace",
)

GROUP_ACTIONS: dict[str, dict[str, str]] = {
    "akb_discover": {
        "list_vaults": "akb_list_vaults",
        "vault_info": "akb_vault_info",
        "browse": "akb_browse",
        "search": "akb_search",
        "grep": "akb_grep",
    },
    "akb_document_read": {
        "get": "akb_get",
        "section": "akb_drill_down",
        "activity": "akb_activity",
        "history": "akb_history",
        "diff": "akb_diff",
        "provenance": "akb_provenance",
    },
    "akb_relationships": {
        "relations": "akb_relations",
        "graph": "akb_graph",
    },
    "akb_identity": {
        "whoami": "akb_whoami",
        "search_users": "akb_search_users",
    },
    "akb_vault_access": {
        "members": "akb_vault_members",
        "explain": "akb_explain_access",
    },
}

STDIO_LOCAL_OPERATIONS = (
    "akb_get_file",
    "akb_put_file",
    "akb_update_file",
    "akb_delete_file",
    "akb_put_image",
    "akb_discard_image",
)

# Each backend operation is assigned to a live SDK test that runs through both
# the HTTP client and the repository stdio proxy. The final value is the
# public operation name/action literal the test exercises.
LIVE_OPERATION_CASES: dict[str, tuple[str, str, str]] = {
    "akb_list_vaults": ("test_list_vaults_e2e.py", "test_akb_list_vaults_mcp_e2e", "list_vaults"),
    "akb_create_vault": ("test_product_e2e.py", "test_publication_help_and_vault_lifecycle", "akb_create_vault"),
    "akb_put": ("test_product_e2e.py", "test_documents_browse_search_and_versioned_reads", "akb_put"),
    "akb_get": ("test_product_e2e.py", "test_documents_browse_search_and_versioned_reads", "akb_get"),
    "akb_update": ("test_product_e2e.py", "test_documents_browse_search_and_versioned_reads", "akb_update"),
    "akb_edit": ("test_detail_e2e.py", "test_exact_text_edit_contract_and_permissions", "akb_edit"),
    "akb_move": ("test_product_e2e.py", "test_document_move_preserves_aliases_and_collision_rules", "akb_move"),
    "akb_delete": ("test_product_e2e.py", "test_relations_activity_provenance_and_document_deletion", "akb_delete"),
    "akb_browse": ("test_product_e2e.py", "test_documents_browse_search_and_versioned_reads", "akb_browse"),
    "akb_search": ("test_product_e2e.py", "test_documents_browse_search_and_versioned_reads", "akb_search"),
    "akb_grep": ("test_detail_e2e.py", "test_unicode_graph_grep_and_ownership", "akb_grep"),
    "akb_drill_down": ("test_product_e2e.py", "test_documents_browse_search_and_versioned_reads", "akb_drill_down"),
    "akb_activity": ("test_product_e2e.py", "test_relations_activity_provenance_and_document_deletion", "akb_activity"),
    "akb_diff": ("test_product_e2e.py", "test_relations_activity_provenance_and_document_deletion", "akb_diff"),
    "akb_relations": ("test_product_e2e.py", "test_relations_activity_provenance_and_document_deletion", "akb_relations"),
    "akb_graph": ("test_product_e2e.py", "test_relations_activity_provenance_and_document_deletion", "akb_graph"),
    "akb_link": ("test_detail_e2e.py", "test_unicode_graph_grep_and_ownership", "akb_link"),
    "akb_unlink": ("test_detail_e2e.py", "test_unicode_graph_grep_and_ownership", "akb_unlink"),
    "akb_provenance": ("test_product_e2e.py", "test_relations_activity_provenance_and_document_deletion", "akb_provenance"),
    "akb_create_table": ("test_product_e2e.py", "test_tables_sql_and_ddl", "akb_create_table"),
    "akb_sql": ("test_product_e2e.py", "test_tables_sql_and_ddl", "akb_sql"),
    "akb_drop_table": ("test_product_e2e.py", "test_tables_sql_and_ddl", "akb_drop_table"),
    "akb_alter_table": ("test_security_e2e.py", "test_table_constraints_and_permission_contract", "akb_alter_table"),
    "akb_publish": ("test_publication_okf_e2e.py", "test_publication_sdk_lifecycle_and_rest_oracle", "akb_publish"),
    "akb_unpublish": ("test_publication_okf_e2e.py", "test_publication_sdk_lifecycle_and_rest_oracle", "akb_unpublish"),
    "akb_publications": ("test_publication_okf_e2e.py", "test_publication_sdk_lifecycle_and_rest_oracle", "akb_publications"),
    "akb_publication_snapshot": ("test_publication_okf_e2e.py", "test_publication_sdk_lifecycle_and_rest_oracle", "akb_publication_snapshot"),
    "akb_vault_info": ("test_product_e2e.py", "test_relations_activity_provenance_and_document_deletion", "akb_vault_info"),
    "akb_vault_members": ("test_product_e2e.py", "test_relations_activity_provenance_and_document_deletion", "akb_vault_members"),
    "akb_grant": ("test_product_e2e.py", "test_access_roles_and_public_levels", "akb_grant"),
    "akb_revoke": ("test_product_e2e.py", "test_access_roles_and_public_levels", "akb_revoke"),
    "akb_explain_access": ("test_identity_access_e2e.py", "test_identity_and_self_or_other_access_explanations", "akb_explain_access"),
    "akb_search_users": ("test_product_e2e.py", "test_relations_activity_provenance_and_document_deletion", "akb_search_users"),
    "akb_whoami": ("test_identity_access_e2e.py", "test_identity_and_self_or_other_access_explanations", "akb_whoami"),
    "akb_transfer_ownership": ("test_detail_e2e.py", "test_unicode_graph_grep_and_ownership", "akb_transfer_ownership"),
    "akb_archive_vault": ("test_product_e2e.py", "test_publication_help_and_vault_lifecycle", "akb_archive_vault"),
    "akb_delete_vault": ("test_product_e2e.py", "test_publication_help_and_vault_lifecycle", "akb_delete_vault"),
    "akb_create_collection": ("test_detail_e2e.py", "test_collection_lifecycle_boundaries", "akb_create_collection"),
    "akb_delete_collection": ("test_detail_e2e.py", "test_collection_lifecycle_boundaries", "akb_delete_collection"),
    "akb_set_public": ("test_product_e2e.py", "test_access_roles_and_public_levels", "akb_set_public"),
    "akb_history": ("test_product_e2e.py", "test_documents_browse_search_and_versioned_reads", "akb_history"),
    "akb_help": ("test_product_e2e.py", "test_publication_help_and_vault_lifecycle", "akb_help"),
    "akb_export": ("test_publication_okf_e2e.py", "test_okf_sdk_round_trip_and_import_acl", "akb_export"),
    "akb_import": ("test_publication_okf_e2e.py", "test_okf_sdk_round_trip_and_import_acl", "akb_import"),
    "akb_grep_replace": ("test_detail_e2e.py", "test_unicode_graph_grep_and_ownership", "akb_grep_replace"),
}

_GROUPED_OPERATIONS = frozenset(
    operation
    for actions in GROUP_ACTIONS.values()
    for operation in actions.values()
)
_GROUP_ACTION_FOR_OPERATION = {
    operation: (group, action)
    for group, actions in GROUP_ACTIONS.items()
    for action, operation in actions.items()
}


def assert_catalog_contract(tools: Sequence[Any], *, transport: str) -> str:
    """Check the exact flat or mixed catalog and return the detected source arm."""

    names = [tool.name for tool in tools]
    assert len(names) == len(set(names)), "tools/list contains duplicate names"
    name_set = set(names)
    local = set(STDIO_LOCAL_OPERATIONS)
    expected_local = local if transport == "stdio" else set()
    assert name_set & local == expected_local

    group_names = set(GROUP_ACTIONS)
    is_mixed = bool(name_set & group_names)
    if is_mixed:
        expected_backend = (set(LOGICAL_OPERATIONS) - _GROUPED_OPERATIONS) | group_names
        assert not (name_set & _GROUPED_OPERATIONS), "mixed catalog exposes replaced read tools"
        for group, actions in GROUP_ACTIONS.items():
            tool = next((item for item in tools if item.name == group), None)
            assert tool is not None
            schema = tool.input_schema
            branches = schema.get("oneOf") if isinstance(schema, Mapping) else None
            assert isinstance(branches, list)
            assert len(branches) == len(actions)
            actual: dict[str, Mapping[str, Any]] = {}
            for branch in branches:
                properties = branch.get("properties", {})
                discriminator = properties.get("action", {})
                action = discriminator.get("const")
                assert isinstance(action, str)
                assert action not in actual, f"{group} duplicates action {action}"
                assert branch.get("additionalProperties") is False
                assert "action" in branch.get("required", [])
                actual[action] = branch
            assert set(actual) == set(actions)
        source = "mixed"
    else:
        expected_backend = set(LOGICAL_OPERATIONS)
        assert not (name_set & group_names), "flat catalog exposes mixed-only groups"
        source = "flat"

    actual_backend = name_set - expected_local
    assert actual_backend == expected_backend, (
        f"{source} {transport} catalog mismatch: "
        f"missing={sorted(expected_backend - actual_backend)}, "
        f"extra={sorted(actual_backend - expected_backend)}"
    )
    expected_count = (33 if source == "mixed" else 45) + len(expected_local)
    assert len(names) == expected_count
    by_name = {tool.name: tool for tool in tools}
    for name in ("akb_put", "akb_update"):
        properties = by_name[name].input_schema.get("properties", {})
        if transport == "stdio":
            assert properties.get("file", {}).get("type") == "string"
        else:
            assert "file" not in properties
    return source


def call_target(
    tools: Sequence[Any], operation: str, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Resolve one canonical operation through the observed public catalog."""

    names = {tool.name for tool in tools}
    is_mixed = bool(names & set(GROUP_ACTIONS))
    if operation in names:
        assert not is_mixed or operation not in _GROUPED_OPERATIONS, (
            f"mixed catalog unexpectedly exposes {operation} directly"
        )
        return operation, arguments

    group_action = _GROUP_ACTION_FOR_OPERATION.get(operation)
    assert group_action is not None, f"no public mapping for {operation}"
    group, action = group_action
    assert group in names, f"tools/list is missing group {group} for {operation}"
    return group, {"action": action, **arguments}
