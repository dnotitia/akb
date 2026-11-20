# Rolling back a document to an earlier version

Agents and people both make bad edits. An agent rewrites a policy with the
wrong numbers, or a cleanup merges two pages that should have stayed apart.
This guide covers what a knowledge store needs for recovery, and how to
restore an earlier version of an AKB document.

## Short answer

Keep every revision, make old revisions readable, and restore by writing the
old text as a new revision. That way the bad edit and the fix both stay in the
record. In AKB, `akb_history` lists a document's revisions, `akb_get` with
`version` reads one, and `akb_update` writes it back. AKB restores single
documents. There is no vault-wide point-in-time restore, so take OKF exports
or database backups for that.

## Why recovery is often harder than expected

- **Only the latest text is kept.** Without history there is nothing to go
  back to.
- **Restoring rewrites history.** If a restore replaces the record, nobody can
  see what went wrong.
- **Deletes are final.** A deleted page cannot be brought back from the tool.
- **Concurrent edits collide.** A restore can overwrite someone else's later
  fix.

## What to check

| Check | Why it matters |
|---|---|
| Every write creates a revision with author and message | You can find the bad edit. |
| Any old revision can be read in full | You can restore exactly what was there. |
| A restore is recorded as a new revision | The audit trail stays complete. |
| There is a reversible alternative to delete | Mistaken cleanups can be undone. |
| Writes can be conditioned on the revision you read | A restore does not overwrite a newer fix. |

## Restoring a document in AKB

1. **Find the revision.** `akb_history` with the document `uri` lists commits
   with author, time and message. `akb_diff` with a commit shows what that
   commit changed.
2. **Read the old text.** `akb_get` with `version` set to the commit hash
   returns the body as it was at that revision. The title, tags and status in
   that response are the current values.
3. **Read the current revision.** `akb_get` without `version` returns
   `current_commit`.
4. **Write the old text back.** Call `akb_update` with the old `content`,
   `expected_commit` set to the current commit, and a `message` such as
   "Restore text from 3f2a…". If anyone changed the document after step 3, the
   write fails and you can check their change first.

If the bad edit also changed the title, tags or status, pass the old values in
the same `akb_update` call.

**Restoring after a move or rename.** `akb_move` keeps a document's identity
and history, and the old URI keeps resolving, so steps 1 to 4 work after a
rename.

## Deletion and retirement

- **Archive instead of delete.** `status: archived` takes a document out of
  default search and browse and can be reversed.
- **Delete cannot be undone through AKB.** `akb_delete` removes a document
  from reads, search and the graph, and there is no public tool or endpoint to
  restore it.
- **Vault deletion cannot be undone.** `akb_delete_vault` removes all
  documents, chunks, tables, files and relations. `akb_archive_vault` makes a
  vault read-only instead, and an archived vault can be unarchived.

## Restoring more than one document

For a bad batch, `akb_activity` with `author` and `since` lists every document
an agent changed, and you can restore each one with the steps above. For a
whole vault, keep regular exports:
`GET /api/v1/vaults/{vault}/export?format=okf&as=zip` returns the vault's
active and draft documents as Markdown. Archived documents, table rows and
file contents are not in the export, so database and object-storage backups
remain the full recovery path. See
[exporting a vault to the Open Knowledge Format](exporting-a-vault-to-open-knowledge-format.md).

## Common questions

**Any knowledge products that let you roll back to a past point in time?** Per
document, yes: look for full revision history with readable old versions. AKB
restores single documents from any revision. A whole-vault point in time needs
exports or database backups.

**Which knowledge platforms keep full version history of every document?**
Ones that record each write as a revision with author and message. AKB does,
and `akb_history`, `akb_diff` and `akb_get` with `version` read it.

**An agent overwrote a good page. How do we undo it without losing the later
edits?** Restore with `akb_update` conditioned on `expected_commit`. If
someone edited after the agent, the condition fails and you can merge by hand.

## Related guides

- [Reviewing agent writes before they go live](reviewing-agent-writes-before-they-go-live.md)
- [Citing the exact source behind an answer](citing-the-exact-source-behind-an-answer.md)
- [Exporting a vault to the Open Knowledge Format](exporting-a-vault-to-open-knowledge-format.md)
