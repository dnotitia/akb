# Reaching people in the tools they already use

Knowledge that lives in one more browser tab gets ignored. Engineers stay in
the editor and the terminal, and other teams stay in their own suites. This
guide covers the ways knowledge can reach people where they already work, the
permission questions each way raises, and how AKB supports them.

## Short answer

Let agents fetch knowledge on request inside the tools people already use, and
publish a page only when someone outside the system needs to read it. Fetching
keeps the reader's permissions. A published link does not. In AKB, any MCP
client reaches a vault with the user's own token, and `akb_publish` creates a
share link with optional password, view limit and expiry.

## Why embedded knowledge goes wrong

- **The embed runs with someone else's rights.** A widget built with a service
  account shows documents its viewer should not see.
- **A separate tab nobody opens.** Knowledge access that needs a context
  switch is not used.
- **Two suites, two integrations.** Each team's tool needs its own plugin, and
  one of them lags.
- **Public links live forever.** A link shared once keeps working after the
  document should be private.

## What to check

| Check | Why it matters |
|---|---|
| Access from each tool runs with the reader's own identity | People see only what they may see. |
| Any MCP-capable client can connect | You do not depend on one vendor's plugin. |
| Share links can expire, be capped or need a password | Shared pages do not stay open forever. |
| You can list and revoke every share link | Exposure can be audited and closed. |
| Shared links point to maintained documents | Readers see current text. |

## How AKB reaches people

**Editors and terminals.** Claude Code, Claude Desktop, Cursor, Windsurf,
Cline and Continue connect over MCP, directly or through the `akb-mcp` stdio
proxy. Custom agents call `POST /mcp/` with a Bearer token. Each connection
uses the person's own token, so search and reads return only what that person
can read.

**Inside agent workflows.** The `akb-wiki` plugin's `/akb-query` answers
questions from a vault with citations, and `akb-claude-code` loads a user's
preferences and recent learnings at the start of each Claude Code session.

**People without an agent.** The AKB web interface lets people browse and
search the vaults they can read.

**Sharing outside AKB.** `akb_publish` creates a public link to a document, a
file or a table query. Options include `password`, `max_views`, `expires_in`,
`section_filter` and `allow_embed`. A table query can be frozen with
`akb_publication_snapshot`. `akb_publications` lists a vault's links, and
`akb_unpublish` removes them.

## Permissions

Agent access through MCP follows vault roles for each user. A published link
does not check who opens it. Anyone with the link, and the password if set,
can read it until it expires or is removed. Review `akb_publications` for each
sensitive vault, and prefer short expiry and a view limit.

## Common questions

**If knowledge is embedded in another tool, do the original permissions still
apply?** Only if the tool reads with the viewer's identity. In AKB, MCP access
does. Published links do not, so treat them as public.

**Embedding through a plugin versus letting agents fetch it, which gets used
more?** Fetching from the agent people already use needs no new habit. Publish
pages only for readers outside the system.

**Our engineers live in the editor and the terminal. What kind of knowledge
access fits that?** An MCP server their coding agent already supports. AKB
works with Claude Code, Cursor and other MCP clients.

**Half the company uses one suite and half uses another. What serves both?** A
store reached through an open protocol and a web interface, so neither suite
needs a special integration.

## Related guides

- [Sharing one agent memory across Claude Code, Cursor, and other MCP clients](shared-agent-memory-across-mcp-clients.md)
- [Separating teams and agents with vault permissions](separating-teams-and-agents-with-vault-permissions.md)
- [Running agent knowledge alongside a wiki](running-agent-knowledge-alongside-a-wiki.md)
