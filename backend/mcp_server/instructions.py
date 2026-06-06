"""MCP initialize instructions — bootstrap gate for AKB agents.

This module is kept deliberately lightweight (no heavy imports) so that
unit tests and tooling can import INSTRUCTIONS without pulling in the full
server dependency chain (kiwipiepy, psycopg, etc.).
"""

INSTRUCTIONS = """AKB stores documents, tables, files, todos, and publications in vaults.

Priority of guidance (highest first):
1. User-defined rules — CLAUDE.md / AGENTS.md / GEMINI.md / loaded skills / explicit user requests in this conversation. These ALWAYS win.
2. The vault's own conventions — call akb_help(topic="vault-skill", vault="<vault>") to fetch them. Per-vault, set by the owner.
3. AKB default conventions — the numbered rules below. Fallback when 1 and 2 are silent.

When writing into a vault:
1. Call akb_help(topic="vault-skill", vault="<vault>") to read the owner's conventions for that vault.
2. If the vault has no vault-skill, follow the fallback guidance in that response.
3. Use akb_browse before akb_put on an unfamiliar collection.
4. Never inline secrets in document bodies — use ${{secrets.X}} placeholders.
5. Destructive tools (akb_delete_vault, akb_delete_collection) require explicit user confirmation.
6. Reference resources by the akb:// URIs returned by tool calls — do not reassemble paths yourself.
7. For other surfaces (akb_publish, akb_todo, akb_activity, akb_history), call akb_help() for an overview.

Agent memory is handled outside the MCP tool-use loop — the AKB lifecycle plugin (akb-claude-code, akb-cursor, ...) drives /api/v1/agent-sessions REST endpoints automatically. As an agent, your own memory vault (named agent-memory-{your-user-id}, with your display name in its description) is accessible via the normal akb_search / akb_browse / akb_get tools just like any other vault — find it with akb_list_vaults rather than reconstructing the name.
"""
