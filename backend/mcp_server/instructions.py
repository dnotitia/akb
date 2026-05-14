"""MCP initialize instructions — bootstrap gate for AKB agents.

This module is kept deliberately lightweight (no heavy imports) so that
unit tests and tooling can import INSTRUCTIONS without pulling in the full
server dependency chain (kiwipiepy, psycopg, etc.).
"""

INSTRUCTIONS = """AKB stores documents/tables/files in vaults. Before writing into a vault:
1. Call akb_help(topic="vault-skill", vault="<vault>") to read the owner's conventions for that vault.
2. If the vault has no vault-skill, follow the fallback guidance in that response.
3. Use akb_browse before akb_put on an unfamiliar collection.
4. Never inline secrets in document bodies — use ${{secrets.X}} placeholders.
5. Destructive tools (akb_delete_vault, akb_delete_collection) require explicit user confirmation.
"""
