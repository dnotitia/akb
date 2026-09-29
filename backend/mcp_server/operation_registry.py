"""Registry for the bounded MCP capabilities and their operation actions.

Legacy handlers remain implementation units, but their public names are not
candidate operations: the registry binds each implementation to one explicit
capability action.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator
from mcp.types import Tool, ToolAnnotations

from app.util.text import fuzzy_hint


READ_SCOPE = "akb:vault:read"
WRITE_SCOPE = "akb:vault:write"

Risk = Literal["read", "write", "destructive"]
VaultRole = Literal["reader", "writer", "admin", "owner", "explain_target"]
TargetRule = Literal[
    "none",
    "vault",
    "optional_vault",
    "many_vaults",
    "uri",
    "document_uri",
    "table_uri",
    "browse",
    "parent",
    "source",
    "publication",
    "publication_target",
    "publication_slug",
    "resource_or_vault",
]


class OperationValidationError(ValueError):
    """A candidate action failed its registry-owned input contract."""

    def __init__(self, message: str, *, code: str = "invalid_argument", **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """The complete contract for one public capability action."""

    public_tool: str
    action: str
    input_schema: dict[str, Any]
    handler: str
    required_scope: str
    vault_role: VaultRole | None
    target: TargetRule
    risk: Risk
    logical_audit_operation: str
    description: str


_CANDIDATE_OPERATIONS: tuple[tuple[str, str, str, TargetRule, str], ...] = (
    # public tool, action, legacy handler, target rule, action description
    (
        "akb_discover",
        "list_vaults",
        "akb_list_vaults",
        "none",
        "List vaults visible to the caller. Returns the existing vaults/total/returned pagination envelope.",
    ),
    (
        "akb_discover",
        "vault_info",
        "akb_vault_info",
        "vault",
        "Read metadata and counts for one named vault. Requires reader access to that vault.",
    ),
    (
        "akb_discover",
        "browse",
        "akb_browse",
        "browse",
        "Browse a vault root or collection using `uri` or `vault`; preserve the existing items/total/returned pagination envelope.",
    ),
    (
        "akb_discover",
        "search",
        "akb_search",
        "optional_vault",
        "Run hybrid document search. Use `vault` for a scoped search; omit it to search the caller's accessible vaults.",
    ),
    (
        "akb_discover",
        "grep",
        "akb_grep",
        "many_vaults",
        "Find exact text or regex matches. This candidate action is read-only: replacement arguments are not accepted.",
    ),
    (
        "akb_document_read",
        "get",
        "akb_get",
        "uri",
        "Read the current document, or pass `version` to read one hexadecimal historical revision.",
    ),
    (
        "akb_document_read",
        "section",
        "akb_drill_down",
        "uri",
        "Read matching document sections or its outline with the existing section response shape.",
    ),
    (
        "akb_document_read",
        "activity",
        "akb_activity",
        "vault",
        "Read vault Git activity with the existing author, collection, since, and pagination filters.",
    ),
    (
        "akb_document_read",
        "history",
        "akb_history",
        "uri",
        "Read a document's revision history and use returned commits with `get` or `diff`.",
    ),
    (
        "akb_document_read",
        "diff",
        "akb_diff",
        "uri",
        "Read the content diff for one document at a specific hexadecimal commit.",
    ),
    (
        "akb_document_read",
        "provenance",
        "akb_provenance",
        "uri",
        "Read document provenance and visible same-vault relations without mutation.",
    ),
    (
        "akb_relationships",
        "relations",
        "akb_relations",
        "uri",
        "Read the existing incoming, outgoing, or both relations for one resource URI, with the optional relation-type filter.",
    ),
    (
        "akb_relationships",
        "graph",
        "akb_graph",
        "resource_or_vault",
        "Read a bounded graph around a resource URI, or the full graph for a named vault; `hops` controls URI traversal.",
    ),
    (
        "akb_vault_access",
        "members",
        "akb_vault_members",
        "vault",
        "List the existing member roster and roles for one vault. Requires reader access.",
    ),
    (
        "akb_vault_access",
        "explain",
        "akb_explain_access",
        "vault",
        "Explain a user's access bases for one vault. Readers may explain themselves; explaining another user requires admin access.",
    ),
    (
        "akb_identity",
        "whoami",
        "akb_whoami",
        "none",
        "Read the authenticated caller's existing profile.",
    ),
    (
        "akb_identity",
        "search_users",
        "akb_search_users",
        "none",
        "Search users by the existing username, display-name, or email query and limit.",
    ),
    (
        "akb_publication_read",
        "list",
        "akb_publications",
        "vault",
        "List the publications for one vault, optionally filtered by resource type.",
    ),
    (
        "akb_export_read",
        "export",
        "akb_export",
        "vault",
        "Export one readable vault as the existing inline OKF {path: content} bundle.",
    ),
)

_CANDIDATE_WRITES: tuple[
    tuple[str, str, str, str, TargetRule, VaultRole | None, Risk, str], ...
] = (
    # public tool, action, logical operation, legacy handler, target, role, risk, description
    (
        "akb_document_write", "put", "akb_put", "akb_put", "parent", "writer", "write",
        "Create a document using either `parent` or the `vault` plus optional `collection` coordinates.",
    ),
    (
        "akb_document_write", "update", "akb_update", "akb_update", "document_uri", "writer", "write",
        "Update document content or metadata at its URI, with the existing optional OCC pins.",
    ),
    (
        "akb_document_write", "edit", "akb_edit", "akb_edit", "document_uri", "writer", "write",
        "Replace an exact document-body string, preserving the existing uniqueness, OCC, and error behavior.",
    ),
    (
        "akb_document_write", "move", "akb_move", "akb_move", "document_uri", "writer", "write",
        "Move or rename one document within its existing vault.",
    ),
    (
        "akb_document_write", "delete", "akb_delete", "akb_delete", "document_uri", "writer", "destructive",
        "Delete one document by URI.",
    ),
    (
        "akb_document_write", "grep_replace", "akb_grep_replace", "akb_grep", "many_vaults", "writer", "destructive",
        "Replace every matching document in an explicit vault scope. `replace` is required; an empty string deletes the matches.",
    ),
    (
        "akb_collection_manage", "create", "akb_create_collection", "akb_create_collection", "vault", "writer", "write",
        "Create a collection at the requested path in one vault.",
    ),
    (
        "akb_collection_manage", "delete", "akb_delete_collection", "akb_delete_collection", "vault", "writer", "destructive",
        "Delete a collection, retaining the existing recursive and contained-table permission checks.",
    ),
    (
        "akb_relationship_manage", "link", "akb_link", "akb_link", "source", "writer", "write",
        "Create one explicit relation between two resources in the same vault.",
    ),
    (
        "akb_relationship_manage", "unlink", "akb_unlink", "akb_unlink", "source", "writer", "destructive",
        "Remove the selected explicit relation, or all explicit relations between the resources when `relation` is omitted.",
    ),
    (
        "akb_vault_access_manage", "grant", "akb_grant", "akb_grant", "vault", "admin", "write",
        "Grant a user a vault role, optionally under a named access basis and revision.",
    ),
    (
        "akb_vault_access_manage", "revoke", "akb_revoke", "akb_revoke", "vault", "admin", "destructive",
        "Revoke one access basis, or all bases when `source_key` is omitted.",
    ),
    (
        "akb_vault_access_manage", "transfer_ownership", "akb_transfer_ownership", "akb_transfer_ownership", "vault", "owner", "destructive",
        "Transfer vault ownership to another username.",
    ),
    (
        "akb_vault_access_manage", "set_public", "akb_set_public", "akb_set_public", "vault", "owner", "destructive",
        "Change the vault's public access level.",
    ),
    (
        "akb_publication_manage", "publish", "akb_publish", "akb_publish", "publication", "writer", "write",
        "Publish a document, file, or table query using the existing resource-specific inputs.",
    ),
    (
        "akb_publication_manage", "snapshot", "akb_publication_snapshot", "akb_publication_snapshot", "publication_slug", "writer", "write",
        "Freeze a table-query publication into a snapshot, identified by its slug.",
    ),
    (
        "akb_publication_manage", "unpublish", "akb_unpublish", "akb_unpublish", "publication_target", "writer", "destructive",
        "Remove the publication identified by `slug`, or all publications for a document or file URI.",
    ),
    (
        "akb_vault_manage", "create", "akb_create_vault", "akb_create_vault", "none", None, "write",
        "Create a new vault. The handler checks the requested name against the caller's vault-creation scope.",
    ),
    (
        "akb_vault_manage", "archive", "akb_archive_vault", "akb_archive_vault", "vault", "owner", "destructive",
        "Archive one owned vault.",
    ),
    (
        "akb_vault_manage", "delete", "akb_delete_vault", "akb_delete_vault", "vault", "owner", "destructive",
        "Delete one owned, archived vault.",
    ),
    (
        "akb_table_schema_manage", "create", "akb_create_table", "akb_create_table", "parent", "writer", "write",
        "Create a table at a `parent` URI or under the requested `vault` and optional `collection`.",
    ),
    (
        "akb_table_schema_manage", "alter", "akb_alter_table", "akb_alter_table", "table_uri", "admin", "destructive",
        "Alter table columns, constraints, and indexes. The existing admin requirement applies.",
    ),
    (
        "akb_table_schema_manage", "drop", "akb_drop_table", "akb_drop_table", "table_uri", "admin", "destructive",
        "Drop a table. The existing admin requirement applies.",
    ),
    (
        "akb_bundle_manage", "import", "akb_import", "akb_import", "vault", "writer", "write",
        "Import a knowledge bundle into one vault using the existing reserved-path and overwrite rules.",
    ),
)

CANDIDATE_LEGACY_NAMES = frozenset(item[2] for item in _CANDIDATE_OPERATIONS) | frozenset(
    item[3] for item in _CANDIDATE_WRITES
)
CANDIDATE_REPLACED_NAMES = CANDIDATE_LEGACY_NAMES

INDEPENDENT_OPERATION_REASONS = {
    "akb_help": "Self-documentation remains an independent tool surface.",
    "akb_sql": "Arbitrary SQL keeps its existing mixed read/write scope and risk boundary.",
}

DEFERRED_OPERATION_REASONS = {
    "stdio_local_files": "Local filesystem operations remain in the stdio proxy surface.",
}

# The complete Candidate coverage contract includes local operations even though
# these tool/action pairs are implemented only by the stdio proxy and never
# appear in the backend catalog. The legacy keys name logical operations for
# coverage accounting; they are not public aliases.
DEFERRED_OPERATION_COVERAGE = {
    "akb_put_file": ("akb_file_write", "put_file"),
    "akb_get_file": ("akb_file_read", "read"),
    "akb_update_file": ("akb_file_write", "update_file"),
    "akb_delete_file": ("akb_file_write", "delete_file"),
    "akb_put_image": ("akb_file_write", "put_image"),
    "akb_discard_image": ("akb_file_write", "discard_image"),
}
DEFERRED_OPERATION_NAMES = frozenset(DEFERRED_OPERATION_COVERAGE)


def _action_schema(legacy: Tool, action: str) -> dict[str, Any]:
    schema = deepcopy(legacy.input_schema)
    properties = dict(schema.get("properties") or {})
    if "action" in properties:
        raise ValueError(f"legacy schema already owns reserved action argument: {legacy.name}")
    schema["properties"] = properties
    schema["required"] = list(schema.get("required") or [])
    schema["additionalProperties"] = False
    # The legacy grep tool also performs replacement. The read capability
    # keeps mutation-only arguments out of its action schema.
    if legacy.name == "akb_grep" and action != "grep_replace":
        for name in ("replace", "max_replacements"):
            properties.pop(name, None)
    if legacy.name == "akb_browse":
        # The old handler accepted either coordinate form and enforced the
        # relationship itself.  The candidate contract makes that requirement
        # explicit in the action branch without changing either accepted form.
        schema["anyOf"] = [{"required": ["uri"]}, {"required": ["vault"]}]
    if legacy.name == "akb_graph":
        schema["anyOf"] = [{"required": ["uri"]}, {"required": ["vault"]}]
    if legacy.name in {"akb_put", "akb_create_table"}:
        schema["anyOf"] = [{"required": ["parent"]}, {"required": ["vault"]}]
    if legacy.name == "akb_grep" and action == "grep_replace":
        schema["required"] = [*schema["required"], "replace"]
        schema["anyOf"] = [{"required": ["vault"]}]
        properties["vault"]["description"] = (
            "Required explicit vault scope. Replacement requires writer access to every listed vault."
        )
    elif legacy.name == "akb_grep":
        properties["vault"]["description"] = (
            "Limit the read-only search to one or more vaults. Omit it to search accessible vaults."
        )
    if legacy.name == "akb_publish":
        schema["anyOf"] = [
            {
                "required": ["uri"],
                "anyOf": [
                    {"not": {"required": ["resource_type"]}},
                    {"properties": {"resource_type": {"enum": ["document", "file"]}}},
                ],
            },
            {
                "properties": {"resource_type": {"const": "table_query"}},
                "required": ["resource_type", "vault", "query_sql"],
            },
        ]
    if legacy.name == "akb_unpublish":
        schema["anyOf"] = [{"required": ["slug"]}, {"required": ["uri"]}]
    schema["description"] = f"Candidate action `{action}` input contract."
    return schema


class OperationRegistry:
    """Registry for bounded candidate operations and their public schemas."""

    def __init__(self) -> None:
        self._specs: list[OperationSpec] = []
        self._by_key: dict[tuple[str, str], OperationSpec] = {}
        self._handlers: dict[tuple[str, str], Any] = {}
        self._tools: dict[str, Tool] | None = None

    def register(self, spec: OperationSpec) -> None:
        key = (spec.public_tool, spec.action)
        if key in self._by_key:
            raise ValueError(f"duplicate candidate operation: {spec.public_tool}/{spec.action}")
        if not spec.public_tool or not spec.action or not spec.handler:
            raise ValueError("candidate operation requires public_tool, action, and handler")
        if not spec.logical_audit_operation:
            raise ValueError(f"missing logical audit operation: {spec.public_tool}/{spec.action}")
        if spec.required_scope not in {READ_SCOPE, WRITE_SCOPE}:
            raise ValueError(f"invalid OAuth scope: {spec.required_scope}")
        if spec.risk not in {"read", "write", "destructive"}:
            raise ValueError(f"invalid operation risk: {spec.risk}")
        if spec.risk == "read" and spec.required_scope != READ_SCOPE:
            raise ValueError(f"read operation is not read-scoped: {spec.public_tool}/{spec.action}")
        if spec.input_schema.get("type") != "object":
            raise ValueError(f"action schema must be an object: {spec.public_tool}/{spec.action}")
        properties = spec.input_schema.get("properties")
        if not isinstance(properties, dict) or "action" in properties:
            raise ValueError(f"action schema has invalid properties: {spec.public_tool}/{spec.action}")
        if spec.vault_role not in {
            None,
            "reader",
            "writer",
            "admin",
            "owner",
            "explain_target",
        }:
            raise ValueError(f"invalid vault RBAC role: {spec.vault_role}")
        if spec.target not in {
            "none",
            "vault",
            "optional_vault",
            "many_vaults",
            "uri",
            "document_uri",
            "table_uri",
            "browse",
            "parent",
            "source",
            "publication",
            "publication_target",
            "publication_slug",
            "resource_or_vault",
        }:
            raise ValueError(f"invalid vault target rule: {spec.target}")
        self._specs.append(spec)
        self._by_key[key] = spec
        self._tools = None

    @property
    def specs(self) -> tuple[OperationSpec, ...]:
        return tuple(self._specs)

    @property
    def tools_by_name(self) -> Mapping[str, Tool]:
        self._build_tools()
        assert self._tools is not None
        return self._tools

    def has_tool(self, public_tool: str) -> bool:
        return any(spec.public_tool == public_tool for spec in self._specs)

    def actions_for(self, public_tool: str) -> tuple[str, ...]:
        return tuple(spec.action for spec in self._specs if spec.public_tool == public_tool)

    def spec_for(self, public_tool: str, action: str) -> OperationSpec | None:
        return self._by_key.get((public_tool, action))

    def validate(self, public_tool: str, arguments: Mapping[str, Any]) -> OperationSpec:
        if not isinstance(arguments, Mapping):
            raise OperationValidationError("Arguments must be an object")
        action = arguments.get("action")
        actions = self.actions_for(public_tool)
        if not isinstance(action, str) or not action:
            raise OperationValidationError(
                f"Argument 'action' is required for {public_tool}",
                available_actions=list(actions),
            )
        spec = self.spec_for(public_tool, action)
        if spec is None:
            raise OperationValidationError(
                f"Unknown action '{action}' for {public_tool}",
                available_actions=list(actions),
            )

        allowed = set(spec.input_schema.get("properties") or {})
        unknown = [key for key in arguments if key != "action" and key not in allowed]
        if unknown:
            bad = unknown[0]
            raise OperationValidationError(
                f"Unknown argument '{bad}' for action '{action}'",
                code="unknown_argument",
                action=action,
                hint=fuzzy_hint(bad, sorted(allowed), label="arguments"),
                available_arguments=sorted(allowed),
            )

        required = [str(value) for value in spec.input_schema.get("required") or []]
        missing = [key for key in required if key not in arguments]
        if missing:
            raise OperationValidationError(
                f"Missing required argument '{missing[0]}' for action '{action}'",
                action=action,
                missing_arguments=missing,
                available_arguments=sorted(allowed),
            )

        validator = Draft202012Validator(self._branch_schema(spec))
        errors = sorted(validator.iter_errors(dict(arguments)), key=lambda error: list(error.path))
        if errors:
            error = errors[0]
            raise OperationValidationError(
                f"Invalid arguments for action '{action}': {error.message}",
                action=action,
                available_arguments=sorted(allowed),
            )
        return spec

    def bind_handlers(self, handlers: Mapping[str, Any]) -> None:
        bound: dict[tuple[str, str], Any] = {}
        for spec in self._specs:
            handler = handlers.get(spec.handler)
            if handler is None:
                raise RuntimeError(
                    f"candidate operation handler is not registered: {spec.handler}"
                )
            bound[(spec.public_tool, spec.action)] = handler
        self._handlers = bound

    def replace_contract(self, replacement: OperationRegistry) -> None:
        """Replace the action schemas while retaining already-bound handlers."""
        handlers_by_name = {
            spec.handler: self._handlers[(spec.public_tool, spec.action)]
            for spec in self._specs
            if (spec.public_tool, spec.action) in self._handlers
        }
        self._specs = list(replacement._specs)
        self._by_key = dict(replacement._by_key)
        self._handlers = {
            (spec.public_tool, spec.action): handlers_by_name[spec.handler]
            for spec in self._specs
            if spec.handler in handlers_by_name
        }
        self._tools = None
        self.validate_contract()

    def handler_for(self, spec: OperationSpec) -> Any:
        try:
            return self._handlers[(spec.public_tool, spec.action)]
        except KeyError as exc:
            raise RuntimeError("candidate operation registry is not bound to handlers") from exc

    def required_scope_for(self, public_tool: str, arguments: Mapping[str, Any]) -> str:
        action = arguments.get("action") if isinstance(arguments, Mapping) else None
        spec = self.spec_for(public_tool, action) if isinstance(action, str) else None
        if spec is None:
            return WRITE_SCOPE
        return spec.required_scope

    def required_scope_for_tool(self, public_tool: str) -> str | None:
        """Return the validated common scope for a registry-owned tool."""
        specs = [spec for spec in self._specs if spec.public_tool == public_tool]
        return specs[0].required_scope if specs else None

    def logical_audit_operation_for(
        self, public_tool: str, arguments: Mapping[str, Any]
    ) -> str | None:
        action = arguments.get("action") if isinstance(arguments, Mapping) else None
        spec = self.spec_for(public_tool, action) if isinstance(action, str) else None
        return spec.logical_audit_operation if spec else None

    def vaults_for(self, spec: OperationSpec, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        """Resolve explicit target vaults for the registry-owned RBAC rule."""
        if spec.target == "none":
            return ()
        if spec.target == "vault":
            value = arguments.get("vault")
            return (value,) if isinstance(value, str) and value else ()
        if spec.target == "optional_vault":
            value = arguments.get("vault")
            return (value,) if isinstance(value, str) and value else ()
        if spec.target == "many_vaults":
            value = arguments.get("vault")
            if isinstance(value, str) and value:
                return (value,)
            if isinstance(value, list):
                return tuple(dict.fromkeys(
                    item for item in value if isinstance(item, str) and item
                ))
            return ()

        if spec.target == "uri":
            value = arguments.get("uri")
            if not isinstance(value, str):
                return ()
            from app.services.uri_service import parse_uri

            parsed = parse_uri(value)
            return (parsed.vault,) if parsed is not None else ()

        if spec.target in {"document_uri", "table_uri"}:
            value = arguments.get("uri")
            if not isinstance(value, str):
                return ()
            from app.services.uri_service import parse_uri

            parsed = parse_uri(value)
            expected = "doc" if spec.target == "document_uri" else "table"
            return (parsed.vault,) if parsed is not None and parsed.kind == expected else ()

        if spec.target == "parent":
            parent = arguments.get("parent")
            if isinstance(parent, str) and parent:
                from app.services.uri_service import split_browse_uri

                try:
                    vault, _collection = split_browse_uri(parent)
                except ValueError:
                    return ()
                return (vault,)
            value = arguments.get("vault")
            return (value,) if isinstance(value, str) and value else ()

        if spec.target == "source":
            source = arguments.get("source")
            target = arguments.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                return ()
            from app.services.uri_service import parse_uri

            source_parsed = parse_uri(source)
            target_parsed = parse_uri(target)
            if (
                source_parsed is None
                or target_parsed is None
                or source_parsed.vault != target_parsed.vault
            ):
                return ()
            return (source_parsed.vault,)

        if spec.target == "publication":
            if arguments.get("resource_type", "document") == "table_query":
                names = [arguments.get("vault"), *(arguments.get("query_vault_names") or [])]
                return tuple(dict.fromkeys(
                    value for value in names if isinstance(value, str) and value
                ))
            expected_kind = {
                "document": "doc",
                "file": "file",
            }.get(arguments.get("resource_type", "document"))
            if expected_kind is None:
                return ()
            uri = arguments.get("uri")
            if not isinstance(uri, str):
                return ()
            from app.services.uri_service import parse_uri

            parsed = parse_uri(uri)
            return (
                (parsed.vault,)
                if parsed is not None and parsed.kind == expected_kind
                else ()
            )

        if spec.target == "publication_target":
            if arguments.get("slug"):
                return ()
            uri = arguments.get("uri")
            if not isinstance(uri, str):
                return ()
            from app.services.uri_service import parse_uri

            parsed = parse_uri(uri)
            return (
                (parsed.vault,)
                if parsed is not None and parsed.kind in {"doc", "file"}
                else ()
            )

        if spec.target == "publication_slug":
            return ()

        if spec.target == "resource_or_vault":
            uri = arguments.get("uri")
            if isinstance(uri, str) and uri:
                from app.services.uri_service import parse_uri

                parsed = parse_uri(uri)
                return (parsed.vault,) if parsed is not None else ()
            value = arguments.get("vault")
            return (value,) if isinstance(value, str) and value else ()

        if spec.target == "browse":
            uri = arguments.get("uri")
            if isinstance(uri, str) and uri:
                from app.services.uri_service import split_browse_uri

                try:
                    vault, _collection = split_browse_uri(uri)
                except ValueError:
                    return ()
                return (vault,)
            value = arguments.get("vault")
            return (value,) if isinstance(value, str) and value else ()

        return ()

    def operation_coverage(self) -> dict[str, tuple[str, str]]:
        return {
            spec.logical_audit_operation: (spec.public_tool, spec.action)
            for spec in self._specs
        }

    def validate_contract(self) -> None:
        operations = [spec.logical_audit_operation for spec in self._specs]
        if len(operations) != len(set(operations)):
            raise ValueError("a logical operation is registered more than once")
        for public_tool in {spec.public_tool for spec in self._specs}:
            specs = [spec for spec in self._specs if spec.public_tool == public_tool]
            if len({spec.required_scope for spec in specs}) != 1:
                raise ValueError(f"candidate tool mixes OAuth scopes: {public_tool}")

    def _branch_schema(self, spec: OperationSpec) -> dict[str, Any]:
        properties = {"action": {"const": spec.action}}
        properties.update(deepcopy(spec.input_schema.get("properties") or {}))
        branch: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": ["action", *list(spec.input_schema.get("required") or [])],
            "additionalProperties": False,
        }
        for keyword in ("anyOf", "allOf", "oneOf", "not"):
            if keyword in spec.input_schema:
                branch[keyword] = deepcopy(spec.input_schema[keyword])
        return branch

    def _build_tools(self) -> None:
        if self._tools is not None:
            return
        self.validate_contract()
        built: dict[str, Tool] = {}
        for public_tool in dict.fromkeys(spec.public_tool for spec in self._specs):
            specs = [spec for spec in self._specs if spec.public_tool == public_tool]
            risks = {spec.risk for spec in specs}
            read_only = risks == {"read"}
            built[public_tool] = Tool(
                name=public_tool,
                description=self._tool_description(public_tool, specs),
                input_schema={
                    "type": "object",
                    "oneOf": [self._branch_schema(spec) for spec in specs],
                },
                annotations=ToolAnnotations(
                    read_only_hint=read_only,
                    destructive_hint="destructive" in risks,
                    idempotent_hint=read_only,
                    open_world_hint=False,
                ),
            )
        self._tools = built

    @staticmethod
    def _tool_description(public_tool: str, specs: list[OperationSpec]) -> str:
        if public_tool == "akb_discover":
            prefix = (
                "Read-only discovery capability. Choose exactly one `action`: "
                "use `list_vaults`/`vault_info` for vault discovery, `browse` for a "
                "tree, `search` for semantic retrieval, and `grep` for exact text or regex. "
                "No action mutates AKB."
            )
        elif public_tool == "akb_document_read":
            prefix = (
                "Read-only document capability. Choose exactly one `action`: use `get` "
                "for current or historical content, `section` for headings/sections, "
                "`activity`/`history` for change history, `diff` for a commit comparison, "
                "and `provenance` for origin metadata and visible relations. No action mutates AKB."
            )
        else:
            prefixes = {
                "akb_document_write": (
                    "Document mutation capability. Choose `put`, `update`, `edit`, `move`, or `delete` "
                    "for one document, and `grep_replace` only for an explicit scoped replacement."
                ),
                "akb_collection_manage": (
                    "Collection lifecycle capability. Choose `create` or `delete`; recursive deletion "
                    "retains its extra table permission check."
                ),
                "akb_relationship_manage": (
                    "Relationship mutation capability. Choose `link` or `unlink` for resources in one vault."
                ),
                "akb_vault_access_manage": (
                    "Vault access management capability. Choose `grant`, `revoke`, `transfer_ownership`, "
                    "or `set_public`; each action retains its own authorization rule."
                ),
                "akb_publication_manage": (
                    "Publication lifecycle capability. Choose `publish`, `snapshot`, or `unpublish` "
                    "using the corresponding resource or slug."
                ),
                "akb_vault_manage": (
                    "Vault lifecycle capability. Choose `create`, `archive`, or `delete`; create uses "
                    "the requested name's creation scope and archive/delete require ownership."
                ),
                "akb_table_schema_manage": (
                    "Table schema capability. Choose `create`, `alter`, or `drop`; create requires writer "
                    "access and alter/drop require admin access."
                ),
                "akb_bundle_manage": (
                    "Knowledge bundle capability. Choose `import` to import a bundle into one vault."
                ),
                "akb_relationships": (
                    "Read-only relationship capability. Choose `relations` to inspect one "
                    "resource's edges; choose `graph` for a full vault graph or a bounded URI subgraph."
                ),
                "akb_vault_access": (
                    "Read-only vault access capability. Choose `members` for the roster; "
                    "choose `explain` for the bases behind a user's access. A caller may explain "
                    "their own access as a reader; explaining another user requires admin access."
                ),
                "akb_identity": (
                    "Read-only identity capability. Choose `whoami` for the authenticated profile "
                    "or `search_users` to find users by the existing search fields."
                ),
                "akb_publication_read": (
                    "Read-only publication capability. Choose `list` to list or filter the "
                    "publications in one vault."
                ),
                "akb_export_read": (
                    "Read-only vault export capability. Choose `export` to retrieve the existing "
                    "inline knowledge bundle for one vault."
                ),
            }
            prefix = prefixes[public_tool]
        actions = "\n".join(f"- `{spec.action}`: {spec.description}" for spec in specs)
        return f"{prefix}\n\nActions:\n{actions}"


def build_candidate_registry(legacy_tools: Mapping[str, Tool]) -> OperationRegistry:
    registry = OperationRegistry()
    for public_tool, action, legacy_name, target, description in _CANDIDATE_OPERATIONS:
        try:
            legacy = legacy_tools[legacy_name]
        except KeyError as exc:
            raise ValueError(f"candidate coverage references missing tool: {legacy_name}") from exc
        registry.register(
            OperationSpec(
                public_tool=public_tool,
                action=action,
                input_schema=_action_schema(legacy, action),
                handler=legacy_name,
                required_scope=READ_SCOPE,
                vault_role=(
                    None
                    if target == "none"
                    else "explain_target"
                    if legacy_name == "akb_explain_access"
                    else "reader"
                ),
                target=target,
                risk="read",
                logical_audit_operation=legacy_name,
                description=description,
            )
        )
    for (
        public_tool,
        action,
        logical_operation,
        handler,
        target,
        vault_role,
        risk,
        description,
    ) in _CANDIDATE_WRITES:
        try:
            legacy = legacy_tools[handler]
        except KeyError as exc:
            raise ValueError(f"candidate coverage references missing tool: {handler}") from exc
        registry.register(
            OperationSpec(
                public_tool=public_tool,
                action=action,
                input_schema=_action_schema(legacy, action),
                handler=handler,
                required_scope=WRITE_SCOPE,
                vault_role=vault_role,
                target=target,
                risk=risk,
                logical_audit_operation=logical_operation,
                description=description,
            )
        )
    registry.validate_contract()
    return registry
