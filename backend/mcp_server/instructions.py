"""MCP request instructions — bootstrap gate for AKB agents.

This module is kept deliberately lightweight (no heavy imports) so that
unit tests and tooling can import INSTRUCTIONS without pulling in the full
server dependency chain (kiwipiepy, psycopg, etc.).
"""

INSTRUCTIONS = """AKB stores documents, tables, files, and publications in vaults.

Priority of guidance (highest first):
1. User-defined rules — CLAUDE.md / AGENTS.md / GEMINI.md / loaded skills / explicit user requests in this conversation. These ALWAYS win.
2. Vault conventions — attached on first touch as `vault_skill` and when changed. Negotiating clients may receive `vault_skill_required` without mutation; others receive the guide additively. Fetch full text with akb_help(topic="vault-skill", vault="<vault>").
3. AKB default conventions — the numbered rules below. Fallback when 1 and 2 are silent.

When writing into a vault:
1. On `vault_skill_required`, apply its payload and retry the unchanged call. Direct capability-v2 clients copy `vault_skill.ack_token` to `_vault_skill_ack`; the bundled proxy does this automatically. Never reuse it for another operation.
2. If no payload arrives (read-only mirror vaults have no skill), follow the fallback guidance from akb_help(topic="vault-skill", vault="<vault>").
3. Use akb_browse before akb_put on an unfamiliar collection.
4. Never inline secrets in document bodies — use ${{secrets.X}} placeholders.
5. Destructive tools (akb_delete_vault, akb_delete_collection) require explicit user confirmation.
6. Reference resources by the akb:// URIs returned by tool calls — do not reassemble paths yourself.
7. If listed, use proxy-local akb_put_image and insert its returned `markdown`. For existing documents use targeted akb_edit: akb_update(content=...) replaces the entire body. On write failure call akb_discard_image. If absent, use akb-mcp 2.3+.
8. For other surfaces (akb_publish, akb_activity, akb_history), call akb_help() for an overview.

Agent memory is managed outside this tool loop by lifecycle plugins. Find your accessible memory vault with akb_list_vaults; use normal akb_search / akb_browse / akb_get tools rather than reconstructing its name.
"""
