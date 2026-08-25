"""AKB's single supported MCP protocol revision."""

from mcp_types.version import MODERN_PROTOCOL_VERSIONS

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_SUPPORTED_PROTOCOL_VERSIONS = (MCP_PROTOCOL_VERSION,)

if tuple(MODERN_PROTOCOL_VERSIONS) != MCP_SUPPORTED_PROTOCOL_VERSIONS:
    raise RuntimeError(
        "The pinned MCP SDK must expose exactly the AKB-supported modern protocol revision"
    )
