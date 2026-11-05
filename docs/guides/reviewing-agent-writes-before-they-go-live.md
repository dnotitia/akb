# Reviewing agent writes before they go live

Agents that tidy a knowledge base can merge, rewrite and delete faster than
anyone can check. Many teams add an approval step, and within a month
reviewers approve everything without reading it. This guide covers how to make
review cheap enough that people actually do it, and how to set up AKB so that
agents propose changes and people decide.

## Short answer

Let agents write proposals, show reviewers only what changed, and keep the
permission to delete with people. In AKB, agents write new or reworked
documents as `draft`, reviewers read the change with `akb_diff`, and a person
sets the document to `active`. Give the agent a read-only token, or write
access only to a proposals vault, if it must never change or delete reviewed
content. AKB does not ship an approval workflow or a cleanup agent, so the
review rules are yours to define.

## Why review turns into a formality

- **Reviewers see whole documents.** Rereading a full page to find two changed
  lines is slow, so people stop.
- **Everything needs approval.** When typo fixes and deletions go through the
  same queue, the queue gets skimmed.
- **The agent can act anyway.** If the agent's token can delete, the approval
  step is a convention, not a control.
- **There is no record of who approved what.** Nobody can check later whether
  a change was reviewed.

## What to check

| Check | Why it matters |
|---|---|
| Reviewers can see a line-level diff for each change | Short diffs get read. |
| Agent permissions can be narrower than a person's | The agent cannot skip review. |
| Proposed content is marked and kept apart from approved content | Readers and agents know what is unreviewed. |
| Every change records an author and a message | You can audit approvals afterwards. |
| Deletion can be avoided in favour of a reversible state | A wrong cleanup can be undone. |
| Concurrent edits cannot silently overwrite a reviewed version | Approval applies to the text that was reviewed. |

## Setting up review in AKB

**Mark proposals as drafts.** Documents have three statuses: `draft`, `active`
and `archived`. An agent creates or reworks a document with `status: draft`,
and a reviewer promotes it with `akb_update` and `status: active`. A
status-only change does not need `expected_commit`. Drafts still appear in
`akb_search`. If unreviewed text must stay out of agent answers, write
proposals to a separate vault and have answering agents pass `vault` to search
only reviewed vaults.

**Show the change, not the page.** `akb_history` lists a document's commits
with author, time and message. `akb_diff` with a commit hash returns what that
commit added and removed. `akb_activity` lists changes across a vault,
filtered by `collection`, `author` or `since`, so a reviewer can pull
everything one agent changed this week.

**Narrow the agent's permissions.** A Personal Access Token can be issued
read-only or read and write. A read-only token cannot call tools that change a
vault. It cannot call `akb_sql` either, even for `SELECT`, because that tool
also accepts writes. A read-and-write token can carry a vault scope: a list of
vault-name prefixes and exact vault names. Writes outside that list are
refused, and `akb_sql` is limited to the list for reads too. Other reads
follow the user's normal access. Two common setups:

1. The agent runs with a read-only token and posts its proposals somewhere a
   person acts on them.
2. The agent's token can write only to a proposals vault, for example
   `kb-proposals`. A person copies approved content into the main vault.

Roles do not separate editing from deleting: `writer` on a vault allows
`akb_delete`. Use the token scope or a separate vault when deletion must stay
with people.

**Archive instead of deleting.** Setting `status: archived` removes a document
from default search and browse, keeps its history, and can be reversed by
setting it back to `active`. `akb_delete` removes the document from reads,
search and the graph, and AKB has no public tool or endpoint to restore it.

**Protect the reviewed revision.** A whole-body `akb_update` must pass
`expected_commit` or `expected_content_hash` from the read it was based on. If
someone changed the document in between, the write fails instead of
overwriting the reviewed text.

**Notify reviewers.** Every write emits a change event. A small consumer can
post new drafts to a review channel. See
[building curation on change events](building-curation-on-change-events.md).

## Common questions

**Our reviewers approve everything without reading it. What tooling makes
review work?** Tooling that shows a short diff per change, routes only
meaningful changes to people, and stops the agent from acting without
approval. In AKB that is `akb_diff` on each commit, drafts for proposals, and
a read-only or vault-scoped token for the agent.

**We cannot let anything be deleted without a human signing off. Which
platforms are built that way?** Look for permissions that can keep deletion
away from agents. In AKB, give the agent a read-only token or limit its writes
to a proposals vault, and have agents archive rather than delete.

**What should we require about deletion history before letting a tool prune
documents?** A reversible state such as archive, a record of who changed what
with a message, and a diff for each change. AKB records all three for archive
and edits. A deleted document cannot be restored through AKB's tools.

**How do we judge a tool's cleanup suggestions before we trust them?** Run it
in proposal mode for a few weeks. Count how many drafts reviewers accept
unchanged, edit or reject, using `akb_activity` and `akb_diff` to see what
changed.

## Related guides

- [Finding duplicate and outdated documents](finding-duplicate-and-outdated-documents.md)
- [Rolling back a document to an earlier version](rolling-back-a-document-to-an-earlier-version.md)
- [Building curation on change events](building-curation-on-change-events.md)
