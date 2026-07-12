"""Stable MCP projections for AKB vault access metadata."""

from __future__ import annotations

from typing import Any


def project_accessible_vault(vault: dict[str, Any]) -> dict[str, str | None]:
    """Keep the compact vault list shape without discarding access semantics."""
    role = vault.get("role")
    return {
        "name": vault["name"],
        "description": vault.get("description") or "",
        "role": role if isinstance(role, str) else None,
    }
