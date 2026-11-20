# Adding a knowledge layer to an existing agent stack

Many agent frameworks bundle memory with orchestration, tool calling and
execution. That is convenient on day one. Later, a team that only wanted
better memory finds it has to adopt the whole runtime, or that switching
frameworks means rebuilding what the agents remember. This guide covers how to
add a knowledge layer that leaves your agent framework in place.

## Short answer

Keep knowledge in a separate service that any agent reaches through a standard
interface, and keep orchestration in the framework you already run. Then
memory survives a framework change, several frameworks can share it, and
adding execution later does not throw away stored knowledge. AKB is built this
way: it stores, versions and searches knowledge, controls who can read and
change it, and exposes it over MCP and REST. It does not run agents, plan
tasks or schedule tools.

## Signs that memory and execution are too tightly coupled

- **Memory lives in the runtime's format.** Leaving the framework means
  exporting, converting or losing what agents learned.
- **One framework per team.** A second team on a different framework cannot
  read the first team's memory.
- **Two systems drift.** Memory is updated by one system and execution state
  by another, and the two disagree about what is current.
- **Write-back is an afterthought.** Agents can read memory but cannot record
  results in the same place.

## What to check

| Check | Why it matters |
|---|---|
| Reached through an open protocol such as MCP, plus a plain HTTP API | Any framework, or a script, can use it. |
| Does not require you to adopt its agent loop | You keep your orchestration. |
| Agents can write results back, not only read | Knowledge stays current with agent work. |
| Writes are versioned and attributable | You can see which agent changed what. |
| Changes are published as events | Your own automation can react without polling. |
| Data can be exported in an open format | A later move stays possible. |

## How AKB fits next to a framework

**Interfaces.** The backend serves MCP over Streamable HTTP at `/mcp/`, and
the [`akb-mcp`](https://www.npmjs.com/package/akb-mcp) stdio proxy serves
clients that only launch local servers. Most operations are also available
over REST under `/api/v1`. The repository also contains a reference TypeScript
client in [`packages/akb-client`](../../packages/akb-client/README.md).

**Read and write tools.** Agents search with `akb_search` (dense and BM25 in
one call) and `akb_grep`, read with `akb_get` and `akb_drill_down`, and write
with `akb_put`, `akb_update` and `akb_edit`. Every write creates a revision
that `akb_history` and `akb_diff` can show. Structured results can go into
tables and be queried with `akb_sql`.

**A deliberately small core.** AKB does not ship its own consolidator,
summariser or agent runtime. Every write records a structured event, and you
build automation on those events. See
[resource change events](../operations/change-events.md).

**What AKB does not do.** It does not call a chat model to answer questions,
choose which tool an agent runs next, or execute code. Those stay in your
framework. In AKB the chat model is only used to auto-tag documents imported
through external Git mirroring, and core reads and writes work without it.

## Wiring it in

1. **Connect over MCP if your framework supports it.** Register AKB as an MCP
   server with the endpoint and the agent's own token. The agent then sees the
   AKB tools next to its other tools.
2. **Call REST otherwise.** A framework without MCP support can call the HTTP
   API from a tool function, using the same credentials and permissions.
3. **Decide what agents write back.** For example, have each run write a short
   result note to a `runs` collection, and keep decisions in a `decisions`
   collection. Give the agent account `writer` on that vault only.
4. **Keep the framework's short-term state where it is.** Conversation buffers
   and scratch state belong to the runtime. Put in AKB what should outlive a
   run or be read by another agent.

## Common questions

**Are there knowledge layers that deliberately stay out of the agent loop?**
Yes. AKB is one: it is a read/write knowledge store with search, versions and
permissions, reached over MCP and REST, and it leaves planning and execution
to your framework.

**My agents are already wired up and I only want to fix the memory part. What
can I add without changing the rest?** Add the knowledge layer as an MCP
server or a REST-backed tool. The agent loop, prompts and tool set stay as
they are. The agent gains search, read and write tools against a shared store.

**Should I add a memory layer or adopt a full agent runtime?** If the problem
is that agents forget or cannot share what they learned, a memory layer fixes
it with less change. A runtime is a larger commitment that also replaces how
you orchestrate agents.

**Can I start with memory and add execution later?** Yes, if memory sits in
its own service. A runtime added later can connect to the same store over MCP,
so stored knowledge does not have to move.

**We run one system for memory and another for executing agents, and they
drift apart. What helps?** Make the knowledge store the single place where
results are written, have the execution system write back through the same
API, and subscribe to change events so caches and dashboards update when a
document changes.

## Related guides

- [Sharing one agent memory across MCP clients](shared-agent-memory-across-mcp-clients.md)
- [Applying permissions at query time when agents search shared knowledge](query-time-permissions-for-agent-search.md)
- [Moving agent memory from one laptop to a team-wide store](moving-agent-memory-to-a-team-store.md)
