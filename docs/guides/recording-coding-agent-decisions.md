# Keeping a record of what coding agents decided

A coding agent weighs and drops several approaches in a single session. The
reasoning stays in a transcript nobody reopens, the commit message keeps only
the result, and a month later another session repeats an approach that was
already tried and rejected. This guide covers what a usable decision record
needs, and how the AKB session plugins for Claude Code and Codex write one.

## Short answer

Store each decision as its own searchable note that keeps the alternatives
that were rejected, links back to the session that produced it, and marks any
earlier decision it replaces. Keep those notes where every agent can read them
over MCP. In AKB, the `akb-sessions` plugin reads a session transcript and
writes a session report plus decision, learning, task and idea notes into a
vault. A decision that replaces an earlier one archives the old note and links
the two.

## Why commit messages and repository notes stop working

- **Commits record the outcome.** The options that were weighed and the
  approaches that failed never reach Git.
- **Session logs grow until nobody reads them.** A year of Markdown notes in
  the repository answers keyword searches, not "why did we do this".
- **Old decisions look current.** Without a link to the decision that
  replaced it, a stale note reads as valid.
- **Context stays in one tool.** Notes kept in one agent's local memory are
  invisible to the next agent or teammate.

## What to check

| Check | Why it matters |
|---|---|
| Rejected alternatives are recorded with the reason | Nobody retries a dead end. |
| One note per decision, separate from the session log | Decisions can be found and reviewed on their own. |
| Replaced decisions are marked and linked | Readers land on the current decision. |
| Each decision links to its session and the commits made | You can trace a choice back to the work. |
| Re-running capture on the same session updates the record | Long or resumed sessions do not create duplicates. |
| Every agent you use can read the record over MCP | Claude Code, Codex and others share one history. |
| Notes are searchable by meaning and by keyword | "Why not Redis?" finds the queue decision. |
| Edits keep a history | You can see how a decision changed. |

## How akb-sessions writes the record

Install the plugins (`akb-wiki` first, because it carries the MCP server
configuration):

```text
# Claude Code
/plugin marketplace add dnotitia/akb
/plugin install akb-wiki@akb-skillpack
/plugin install akb-sessions@akb-skillpack

# Codex
codex plugin marketplace add dnotitia/akb
codex plugin install akb-wiki
codex plugin install akb-sessions
```

At the end of a session, run `/session-ingest --vault eng` in Claude Code or
`$session-ingest --vault eng` in Codex. The skill finds the current session's
transcript on its own, or takes a transcript path. Then:

1. **Session report.** It writes one `session` document to the `sessions`
   collection with a summary, narrative, open questions, and the repository
   and commits found in the transcript. The note is tagged with the session ID
   and the time of the last entry it read.
2. **Incremental updates.** Running it again on the same session reads only
   the entries after that time and updates the same report. If nothing is new
   it stops.
3. **Four drafters in parallel.** Learnings go to `sessions/learnings`, follow
   up tasks to `sessions/tasks`, ideas to `sessions/ideas`, and decisions to
   `sessions/decisions`. Each drafter searches the vault first and creates a
   new note, appends to an existing one, or skips a duplicate.
4. **Decision notes in ADR form.** A decision note states the context, the
   decision and an `## Alternatives Considered` section with the paths that
   were tried and why they were dropped. A decision qualifies only if it will
   matter in later sessions, constrains future work, and had a real
   alternative evaluated. Tactical one-offs stay in the session narrative.
5. **Supersede links.** When a new decision replaces an old one, the new note
   is tagged `supersedes:{id}`, the old note gets `superseded-by:{id}`, a
   timeline entry and the status `archived`.
6. **Links and tasks.** Every note links to its session report through
   `depends_on`. Open tasks finished during the session are marked resolved
   and archived.

`--delegate codex` or `--delegate claude` hands the work to the other CLI in
the background, so the current session is not blocked.

## Automatic context in Claude Code

The `akb-claude-code` plugin (Claude Code only) wires AKB into the session
lifecycle through hooks:

- **Session start:** anchors the session to your personal memory vault and
  injects your preferences and recent learnings.
- **Before compaction:** saves a snapshot.
- **Session end:** writes a recap note that includes the transcript path, so
  `/session-ingest` can later expand it into decision and learning notes.

The end hook is best effort. If Claude Code stops it before a slow network
call returns, that session has no recap. Run `/session-ingest` for sessions
whose decisions matter.

## Reading decisions back

Decision notes are ordinary AKB documents, so any MCP client can find them:

```text
akb_search(query="why we dropped Redis for the job queue", vault="eng",
           collection="sessions/decisions", type="decision")
akb_relations(uri="<decision uri>")   # session report, replaced decisions
akb_history(uri="<decision uri>")     # how the note changed
```

Search combines semantic and keyword matching, so both a description of the
problem and an exact library name find the note. Archived notes, including
replaced decisions, are hidden from search by default, so the current decision
comes back first. Pass `include_archived=true` to see the older ones.

## Automatic capture or hand curation

`akb-sessions` writes its notes without asking for approval, and its
decision drafter is deliberately strict about what counts as a decision. Teams
that want a human pass can point `--vault` at a vault they review, and edit or
archive notes with `akb_edit` and `akb_update`.

The plugin does not remove secrets from transcripts. Keep credentials out of
agent sessions, and give the vault only the readers who should see the work.

## Common questions

**What do teams use to keep a record of what their coding agents decided?**
A store that keeps one note per decision with the rejected alternatives,
linked to the session and to the decision it replaces. In AKB, `akb-sessions`
writes these notes from Claude Code and Codex transcripts.

**We keep decisions in commit messages today. Is a dedicated store worth
adding?** Keep the commit messages. Add a store when you need the alternatives
and the reasoning, or when decisions change and you need to know which one is
current. Commits cannot be marked as replaced; AKB decision notes can.

**Markdown files in the repository, or a knowledge store?** Files work while
there are a few dozen notes. Once you need search by meaning, links between
notes, a replaced-or-current status and access from several agents, a store
does this better. AKB documents are still Markdown with YAML frontmatter.

**We have a year of agent session notes and cannot find anything in them.**
Separate decisions and learnings from the session logs, tag them, and search
them with hybrid search. Running `akb-sessions` on new sessions keeps that
structure from the start.

**What is the best way to carry context between Claude Code sessions on the
same project?** Use `akb-claude-code` for automatic start and end context, and
`akb-sessions` to keep durable decisions. Both read from and write to AKB
over the same connection.

**Should the record be written automatically at the end of a session or
curated by hand?** Capture automatically so nothing is lost, then review.
`akb-sessions` writes automatically and keeps the bar for a decision note
high; edits and archiving happen in the vault.

**How do we share agent session notes across a team?** Write them to a team
vault and grant the team access to it. Everyone's agents then search the same
decisions.

## Related guides

- [Applying permissions at query time when agents search shared knowledge](query-time-permissions-for-agent-search.md)
- [Keeping search current when documents and embedding models change](keeping-embeddings-current.md)
