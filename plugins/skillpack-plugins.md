# AKB agent plugins

Plugins that turn [AKB](https://github.com/dnotitia/akb) into an agent-native
knowledgebase you can drive from **Claude Code** and **Codex** — ingest sources
into a vault and query across it without leaving your agent.

## Plugins

- **akb-wiki** — the foundational layer for any AKB vault. `/akb-ingest` takes
  whatever you point it at — a local file, a web URL, a GitHub PR / release /
  commit, a Confluence page, or a Jira issue — classifies it, and writes a
  structured document into your vault. `/akb-query` answers questions from the
  vault with grounded, cited synthesis (read-only). It carries the `akb` MCP
  server the other plugins reuse, so **install it first**.
- **akb-sessions** — capture a coding session as structured notes: a session
  report plus parallel-drafted TIL / task / idea / decision sub-notes, written
  straight into your vault.
- **akb-claude-code** *(Claude Code only)* — a lifecycle bridge wired through
  Claude Code hooks (no slash command): it anchors each session to your AKB
  memory vault, injecting your preferences and recent learnings at session
  start, snapshotting before context compaction, and writing a recap at the end.

## Install

Prepare your AKB credentials first (each plugin asks for these on install):

- `AKB_MCP_URL` — your AKB MCP server URL (ends in `/mcp/`)
- `AKB_PAT` — a personal access token from your AKB instance

### Claude Code

```
/plugin marketplace add dnotitia/akb
/plugin install akb-wiki@akb-skillpack
/plugin install akb-sessions@akb-skillpack
/plugin install akb-claude-code@akb-skillpack
```

### Codex

```bash
codex plugin marketplace add dnotitia/akb
codex plugin install akb-wiki
codex plugin install akb-sessions
```

Pin a revision with `codex plugin marketplace add … --ref <sha>`; refresh with
`codex plugin marketplace upgrade akb-skillpack`.

Every skill takes `--vault {name}` per invocation to choose the target vault.
