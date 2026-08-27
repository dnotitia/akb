"""AKB's public MCP protocol matrix."""

from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS

MCP_LEGACY_PROTOCOL_VERSIONS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
)
MCP_MODERN_PROTOCOL_VERSION = "2026-07-28"

if tuple(HANDSHAKE_PROTOCOL_VERSIONS) != MCP_LEGACY_PROTOCOL_VERSIONS:
    raise RuntimeError(
        "The pinned MCP SDK handshake versions do not match the AKB legacy allowlist"
    )
if tuple(MODERN_PROTOCOL_VERSIONS) != (MCP_MODERN_PROTOCOL_VERSION,):
    raise RuntimeError(
        "The pinned MCP SDK must expose exactly the AKB modern protocol revision"
    )
