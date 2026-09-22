"""Candidate MCP operation registry.

The candidate catalog deliberately has one source of truth for the first
bounded read capabilities.  Legacy handlers remain useful as implementation
units, but their public names are not candidate operations: the registry
binds each implementation exactly once to a capability action.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator
from mcp.types import Tool, ToolAnnotations


READ_SCOPE = "akb:vault:read"
WRITE_SCOPE = "akb:vault:write"

Risk = Literal["read", "write", "destructive"]
TargetRule = Literal["none", "vault", "optional_vault", "many_vaults", "uri", "browse"]


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
    vault_role: str | None
    target: TargetRule
    risk: Risk
    logical_audit_operation: str
    description: str


_FIRST_SLICE: tuple[tuple[str, str, str, TargetRule, str], ...] = (
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
)

FIRST_SLICE_LEGACY_NAMES = frozenset(item[2] for item in _FIRST_SLICE)

# Coverage deliberately names what the next candidate slices own.  This is
# data for tests/review, not a second dispatch catalog.
DEFERRED_OPERATION_NAMES = frozenset(
    {
        "akb_create_vault",
        "akb_archive_vault",
        "akb_delete_vault",
        "akb_put",
        "akb_update",
        "akb_edit",
        "akb_move",
        "akb_delete",
        "akb_create_collection",
        "akb_delete_collection",
        "akb_relations",
        "akb_graph",
        "akb_link",
        "akb_unlink",
        "akb_create_table",
        "akb_alter_table",
        "akb_drop_table",
        "akb_publish",
        "akb_publications",
        "akb_publication_snapshot",
        "akb_unpublish",
        "akb_whoami",
        "akb_vault_members",
        "akb_explain_access",
        "akb_search_users",
        "akb_grant",
        "akb_revoke",
        "akb_transfer_ownership",
        "akb_set_public",
        "akb_export",
        "akb_import",
    }
)


def _action_schema(legacy: Tool, action: str) -> dict[str, Any]:
    schema = deepcopy(legacy.input_schema)
    properties = dict(schema.get("properties") or {})
    if "action" in properties:
        raise ValueError(f"legacy schema already owns reserved action argument: {legacy.name}")
    schema["properties"] = properties
    schema["required"] = list(schema.get("required") or [])
    schema["additionalProperties"] = False
    # The legacy grep tool also performs replacement.  The first slice keeps
    # this action read-only, so mutation-only arguments never enter its schema.
    if legacy.name == "akb_grep":
        for name in ("replace", "max_replacements"):
            properties.pop(name, None)
    if legacy.name == "akb_browse":
        # The old handler accepted either coordinate form and enforced the
        # relationship itself.  The candidate contract makes that requirement
        # explicit in the action branch without changing either accepted form.
        schema["anyOf"] = [{"required": ["uri"]}, {"required": ["vault"]}]
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
        if spec.vault_role not in {None, "reader", "writer", "admin", "owner"}:
            raise ValueError(f"invalid vault RBAC role: {spec.vault_role}")
        if spec.target not in {"none", "vault", "optional_vault", "many_vaults", "uri", "browse"}:
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
                return tuple(item for item in value if isinstance(item, str) and item)
            return ()

        if spec.target == "uri":
            value = arguments.get("uri")
            if not isinstance(value, str):
                return ()
            from app.services.uri_service import parse_uri

            parsed = parse_uri(value)
            return (parsed.vault,) if parsed is not None else ()

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

    def first_slice_coverage(self) -> dict[str, tuple[str, str]]:
        return {
            spec.handler: (spec.public_tool, spec.action)
            for spec in self._specs
        }

    def validate_contract(self) -> None:
        handlers = [spec.handler for spec in self._specs]
        if len(handlers) != len(set(handlers)):
            raise ValueError("a legacy operation is registered more than once")
        for public_tool in {spec.public_tool for spec in self._specs}:
            specs = [spec for spec in self._specs if spec.public_tool == public_tool]
            if len({spec.risk for spec in specs}) != 1:
                raise ValueError(f"candidate tool mixes risk levels: {public_tool}")
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
            risk = specs[0].risk
            built[public_tool] = Tool(
                name=public_tool,
                description=self._tool_description(public_tool, specs),
                input_schema={
                    "type": "object",
                    "oneOf": [self._branch_schema(spec) for spec in specs],
                },
                annotations=ToolAnnotations(
                    read_only_hint=risk == "read",
                    destructive_hint=risk == "destructive",
                    idempotent_hint=risk == "read",
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
        else:
            prefix = (
                "Read-only document capability. Choose exactly one `action`: use `get` "
                "for current or historical content, `section` for headings/sections, "
                "`activity`/`history` for change history, `diff` for a commit comparison, "
                "and `provenance` for origin metadata and visible relations. No action mutates AKB."
            )
        actions = "\n".join(f"- `{spec.action}`: {spec.description}" for spec in specs)
        return f"{prefix}\n\nActions:\n{actions}"


def build_candidate_registry(legacy_tools: Mapping[str, Tool]) -> OperationRegistry:
    registry = OperationRegistry()
    for public_tool, action, legacy_name, target, description in _FIRST_SLICE:
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
                vault_role="reader" if target != "none" else None,
                target=target,
                risk="read",
                logical_audit_operation=legacy_name,
                description=description,
            )
        )
    registry.validate_contract()
    return registry
