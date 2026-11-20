# Moving Markdown notes into a searchable store

Coding agents produce notes quickly. After a few months a repository holds
thousands of lines of session notes, or a folder holds a year of Markdown
files, and nobody can find anything in them. This guide covers how to move an
existing pile of notes into a store where people and agents can search them,
and how to do that with AKB without retyping or reformatting every file.

## Short answer

Import the notes as they are, keep the folder structure as the first level of
organisation, mark everything as a draft, and then promote or archive notes as
you find them useful or obsolete. Use semantic search for questions and exact
search for identifiers. In AKB, a zip of Markdown files imports into a vault
in one request. Folders become collections, files without frontmatter are
still imported, and existing documents are never overwritten, so you can run
the import again safely.

## Why notes in a repository stop working

- **Search is text matching.** A question such as "why did we drop the cache
  layer" does not match notes that never use those words.
- **Files have no metadata.** Without a type, status or tags, a stale note
  looks the same as a current one.
- **One file grows forever.** Appending to a single log makes every answer a
  scroll through unrelated sessions.
- **Only one tool reads them.** Notes in one repository are invisible to
  agents and teammates working elsewhere.

## What to check before moving

| Check | Why it matters |
|---|---|
| Plain Markdown imports without conversion | You do not rewrite a year of notes. |
| Files without frontmatter are accepted | Older notes rarely have metadata. |
| Re-running an import does not duplicate or overwrite | A failed or partial import can be repeated. |
| Folder structure is kept | Existing organisation is not lost. |
| Semantic and exact search both work | Questions and identifiers need different searches. |
| Notes can be marked current, draft or retired | Stale notes stop crowding results. |

## Importing a folder into AKB

1. **Create a vault**, for example `agent-notes`, or use an existing one.
2. **Check reserved names.** The importer skips files named `index.md` and
   `log.md`, and ignores files that do not end in `.md`. Rename such notes
   before you zip the folder.
3. **Zip the folder and import it.**

   ```bash
   cd notes && zip -r ../notes.zip . && cd ..
   curl -H "Authorization: Bearer $AKB_TOKEN" \
     -F "file=@notes.zip" \
     "https://akb.example.com/api/v1/vaults/agent-notes/import?format=okf&status=draft"
   ```

   The request needs `writer` on the vault. An agent can do the same with
`akb_import`, passing a map of file paths to Markdown content.
4. **Read the result.** The response reports `created`, `skipped` and `failed`
   counts, the new URIs, the skipped paths and an `errors` list. Fix the
   failed files and run the same import again: documents that already exist
   are skipped.

**How files map to documents.** The folder path becomes the collection, and
the file name without `.md` becomes the slug. Frontmatter is used when
present: `title`, `type`, `status`, `tags`, `domain`, and `summary` or
`description`. A file without frontmatter is imported with the file name as
its title, `type: note`, and status `draft`. The `status` query parameter
overrides every imported note's status.

**Limits.** One archive can hold up to 10,000 entries and 64 MiB of
uncompressed content. Split larger collections into several archives.

## Finding things after the import

- **Questions:** `akb_search` combines semantic and keyword search, and can be
  limited by `vault`, `collection`, `type` and `tags`.
- **Exact strings:** `akb_grep` matches literal text, or a regular expression
  with `regex=true`, which suits error codes, file names and commit hashes. In
  PostgreSQL Native installations it reads the stored text, so it finds a note
  before its embeddings are ready.
- **Browsing:** `akb_browse` with `page_size` and `next_cursor` walks large
  collections page by page. See
  [browsing large vaults](../operations/large-vault-navigation.md).
- **Long notes:** `akb_drill_down` with `mode="outline"` lists a note's
  headings, so an agent can open only the section it needs.

## Triage after the move

Imported notes start as drafts. As you use them, set useful ones to `active`
and obsolete ones to `archived` with `akb_update`. A change that touches only
metadata, such as `status`, `type` or `tags`, does not need `expected_commit`.
Archived notes leave default search and browse, keep their history, and can be
restored by setting them back to `active`. `akb_grep` includes archived notes
by default, so pass `archive_scope="unarchived"` to exclude them.

## Capturing new notes in the same place

Stop adding to the old files once the import is done. The `akb-sessions`
plugin for Claude Code and Codex writes each session as a report plus task,
learning, idea and decision notes, with `--vault {name}` choosing the vault.
See
[keeping a record of what coding agents decided](recording-coding-agent-decisions.md).

## Common questions

**We keep session notes as files in the repository and nobody reads them.
Where do teams move next?** To a store with metadata and search that agents
reach over MCP. Import the existing files as drafts, then capture new sessions
directly into the store.

**I have a year of agent session notes in Markdown and cannot find anything.
What kind of store fixes that?** One that combines semantic search for
questions with exact search for identifiers, and lets you mark notes as
current or retired. Importing does not require converting the files.

**An engineer left, and everything their coding agent had worked out left with
them. What should we have been using?** A shared store that agents write to
during the work, instead of notes on one laptop. Import the notes that still
exist into a team vault, then capture new sessions there with `akb-sessions`,
so the next person's agent can search them.

**Every engineer's agent learns the same lessons separately. How do we stop
that?** Keep learnings in one vault the team can read, and have agents search
it before they start. `akb-sessions` writes each learning as its own note, so
it can be found by topic instead of by session.

**What should a shared store for agent work records give us?** Import without
converting files, metadata to mark notes current or retired, semantic and
exact search, roles per vault, and a history of every change.

## Related guides

- [Keeping a record of what coding agents decided](recording-coding-agent-decisions.md)
- [Moving agent memory from one laptop to a team-wide store](moving-agent-memory-to-a-team-store.md)
- [Exporting a vault to the Open Knowledge Format](exporting-a-vault-to-open-knowledge-format.md)
