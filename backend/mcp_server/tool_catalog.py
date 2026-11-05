"""Pure composition of the public tool catalog and write acknowledgement."""

from mcp.types import Tool

from app.services.tool_resource_policy import ARG_WRITE_TRIGGERS, MCP_OPERATION_SCOPES, operation_may_write

VAULT_SKILL_ACK_ARGUMENT = "_vault_skill_ack"


def with_skill_acknowledgement(tool: Tool) -> Tool:
    if VAULT_SKILL_ACK_ARGUMENT in tool.input_schema.get("properties", {}):
        raise ValueError(f"Reserved acknowledgement argument collides in {tool.name}")
    if not operation_may_write(tool.name):
        return tool
    copied = tool.model_copy(deep=True)
    copied.input_schema.setdefault("properties", {})[VAULT_SKILL_ACK_ARGUMENT] = {
        "type": "string",
        "maxLength": 128,
        "description": (
            "Opaque acknowledgement returned as vault_skill.ack_token. "
            "After applying that guide, retry the unchanged operation with "
            "this value. The bundled proxy supplies it automatically."
        ),
    }
    return copied


def complete_catalog(tools: list[Tool]) -> list[dict]:
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate tool names in canonical catalog")
    if set(names) != set(MCP_OPERATION_SCOPES):
        raise ValueError("Canonical tools and operation scope registry differ")
    for tool in tools:
        properties = tool.input_schema.get("properties", {})
        if any(argument not in properties for argument in ARG_WRITE_TRIGGERS.get(tool.name, ())):
            raise ValueError(f"Unknown mutating argument in {tool.name}")
    return [with_skill_acknowledgement(tool).model_dump(by_alias=True, exclude_none=True) for tool in tools]
