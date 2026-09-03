"""The small client seam consumed by MCP behavior scenarios."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class DriverResult:
    """A sanitized result from one MCP client operation."""

    transport: str
    operation: str
    status: str
    exit_code: int | None
    output: dict[str, Any] | None = None
    diagnostics: str | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed" and self.exit_code == 0 and self.output is not None


class McpClientDriver(Protocol):
    """Minimum MCP client behavior exposed to a product scenario."""

    transport: str

    def initialize(self) -> DriverResult:
        """Negotiate the MCP protocol and return the structured result."""

    def list_tools(self) -> DriverResult:
        """Read the server tool catalog."""

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> DriverResult:
        """Call one MCP tool with a JSON object of arguments."""
