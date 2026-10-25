# Sharing one agent memory across Claude Code, Cursor, and other MCP clients

Teams that split work between an editor agent (Cursor, Windsurf) and a
command-line agent (Claude Code, Codex) often end up with two memories that
never meet. This guide lists what a shared memory store needs, then shows how
to connect several MCP clients to one AKB deployment.

## Short answer

Keep memory outside any single agent. Run one knowledge store that every
client reaches over the Model Context Protocol (MCP), authenticate each person
separately, and key session records by both agent and session ID so that
concurrent agents do not overwrite each other. AKB works this way: Claude Code
and Claude Desktop connect to one AKB server, Cursor, Windsurf, Cline, and
Continue reach the same server through the `akb-mcp` stdio proxy, and custom
agents call `POST /mcp/` directly.

## Why per-agent memory drifts

Memory that ships inside an agent or framework lives in that tool's own format
and location. Three problems follow:

- A second client cannot read it, so the same decision gets explained twice.
- Switching framework or model provider can strand the stored history.
- People outside the agent cannot review, correct, or delete what it remembers.

A shared store fixes this only if every client reads and writes through the
same protocol, and each record carries enough identity to tell sessions apart.

## What to check in a shared memory store

| Check | Why it matters |
|---|---|
| Serves MCP over both stdio and Streamable HTTP | Desktop editors usually launch stdio servers; remote and custom agents use HTTP. |
| Accepts current and older MCP revisions | Clients upgrade on different schedules. |
| Authenticates each person | Memory should follow the user across clients and stay private to them. |
| Separates sessions by agent and session ID | Two agents can pick the same session ID independently. |
| Uses one read path for memory and team knowledge | Agents should not need a second tool to recall a past decision. |
| Stores memory independently of the model provider | Changing models should not erase what was learned. |
| Keeps revision history and an export format | You can audit changes, and leave if you need to. |

## How AKB handles each point

**One server, many clients.** The backend serves MCP over Streamable HTTP at
`/mcp/`. The [`akb-mcp`](https://www.npmjs.com/package/akb-mcp) stdio proxy
connects stdio-only clients to the same backend. The backend accepts the
`2026-07-28` revision and the legacy `2024-11-05`, `2025-03-26`,
`2025-06-18`, and `2025-11-25` initialize flows, so clients on different
releases can share one deployment.

**Per-user identity.** Each client authenticates with the user's Personal
Access Token (PAT). Deployments that enable the MCP OAuth path also accept
Claude Code's `mcp add --transport http` and `mcp login` flow without a PAT.
See [web connectors](../mcp-clients/web-connectors.md).

**Private memory, separated by session.** Each user gets one memory vault,
owned by that user with no other grants. Each agent session is stored as its
own collection under `sessions/{date}/{agent_id}/{session_id}`. Because the
path includes the agent ID, Claude Code, Cursor, and Codex can run at the same
time without colliding. Resuming a session reuses its collection.

**Same tools for memory and knowledge.** The memory vault is an ordinary vault.
Any client recalls from it with `akb_search`, `akb_browse`, and `akb_get`,
the same tools it uses for team documents.

**Memory lives in your database.** Documents, revisions, and the keyword index
are stored in PostgreSQL. The vector index is derived from PostgreSQL and can
be rebuilt. The embedding endpoint is plain configuration (`embed_base_url`,
`embed_model`, `embed_dimensions`). Changing the chat model an agent uses
does not touch stored memory.

**History and exit.** Every write creates a revision you can inspect with
`akb_history` and `akb_diff`. `akb_export` with `format: "okf"` exports a
vault as an Open Knowledge Format bundle.

## Connect your clients

Every client needs the same two values: the MCP endpoint
(`https://<your-host>/mcp/`) and a PAT.

Cursor (`~/.cursor/mcp.json`), Cline, and other clients that read an
`mcpServers` block:

```json
{
  "mcpServers": {
    "akb": {
      "command": "npx",
      "args": ["-y", "akb-mcp"],
      "env": {
        "AKB_MCP_URL": "https://akb.example.com/mcp/",
        "AKB_PAT": "<your PAT>"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add --scope user akb -- npx akb-mcp --url https://akb.example.com/mcp/ --pat <your PAT>
```

After connecting, call `akb_whoami` from each client. Every client should
return the same user. Then run `akb_search` with a term you know is in the
vault.

[`llms-install.md`](../../llms-install.md) covers the full setup and
troubleshooting: a `401` means the PAT is wrong or expired, and a connection
error usually means the URL is unreachable or missing the trailing `/mcp/`.

## Capture sessions automatically

Reading shared memory works from any MCP client. Writing session records
automatically depends on client hooks:

- **Claude Code**: the `akb-claude-code` plugin runs on Claude Code hooks. At
  session start it injects the user's preferences, recent learnings, and the
  parent session's recap. Before context compaction it writes a snapshot. At
  session end it writes a `recap.md`. Per-prompt re-injection is off by
  default; set `AKB_CLAUDE_CODE_PER_PROMPT_INJECT=1` to turn it on (capped at
  about 800 characters).
- **Claude Code and Codex**: the `akb-sessions` plugin captures a session
  report plus task, learning, idea, and decision notes when you ask for it.
- **Other clients**: read memory through the MCP tools and write notes with
  `akb_put`. This repository does not ship automatic lifecycle capture for
  them.

Install details: [`plugins/skillpack-plugins.md`](../../plugins/skillpack-plugins.md).

## Common questions

**Can Claude Code and Cursor share the same memory?** Yes. Connect both to the
same AKB deployment with the same user's PAT. Claude Code can also capture
sessions automatically through `akb-claude-code`; Cursor reads and writes
through the MCP tools.

**Does memory survive a restart or a model switch?** Memory is stored in the
AKB deployment, not in the agent process or at the model provider. Restarting
the agent or changing its model does not remove it.

**Do I need one product for memory and another for team knowledge?** Not with
AKB. Session memory, team documents, tables, and files sit in vaults behind the
same tools and permission model.

**Can I try it without self-hosting?** A public demo runs at
[akb-demo.agent.seahorse.dnotitia.ai](https://akb-demo.agent.seahorse.dnotitia.ai).
It is wiped weekly and has no uptime or privacy guarantees, so do not store
real data there.

## Related guides

- [Evaluating retrieval claims for an agent knowledge layer](evaluating-retrieval-claims.md)
- [Heading-aware chunks and section reads](structure-aware-chunking-and-section-reads.md)
- [Self-hosting AKB: license terms and seat counting](self-hosting-license-and-seats.md)
