---
status: accepted
stage: applied
created: 2026-08-03
updated: 2026-08-03
---

# Drop the `todos` Stack

## Decision

Complete the cleanup PR #43 (`1c57350`, 2026-05-16) declared and deferred:
archive the `todos` rows, drop the table, delete `todo_service`, and remove
every remaining reference — including the agent-facing prose that still
advertised the deleted tools.

Option **C′** of three the PM weighed:

| | Content | Historic rows | Dead code | PRs |
|---|---|---|---|---|
| B | Drop the `NOT NULL` constraints only | kept | kept | 2 |
| C | Finish the cleanup, no archive | destroyed | removed | 1 |
| **C′** | **Finish the cleanup, archive first** | **kept** | **removed** | **1** |

C′ chosen 2026-08-03. B fixes the symptom while leaving three blocks of
unreachable code permanently; C′ completes the plan PR #43 stated and offsets
the only real risk (data destruction) with the archive.

## Why the table has no future

`todos` recorded person-to-person task assignments hanging off a document
(`ref_doc_id`). PR #43 removed `akb_todo` / `akb_todos` / `akb_todo_update`,
judging that per-agent task lists had superseded it, and left the table and
service "for a separate cleanup migration".

That removal took the *only* entrypoint with it. Measured at `main` `2f49f89`:

| Surface | Result |
|---|---|
| REST router | none — absent from the 14 `include_router` calls in `main.py` |
| Frontend | no references |
| Generated SDK / MCP proxy | no references |
| `todo_service` importers | **0** — all four functions unreachable |

With the tools gone there is no writer either, so any existing rows are a
frozen set that cannot grow. Deployment-specific measurements that confirmed
this are recorded in the internal `gnu-weekly` vault, not here.

## The live bug this removes

`access_service.delete_user_account` detached residual references before
deleting the user row:

```python
await conn.execute("UPDATE vault_access SET granted_by = NULL WHERE granted_by = $1", uid)
await conn.execute("UPDATE publications SET created_by = NULL WHERE created_by = $1", uid)
await conn.execute("UPDATE todos SET assignee_id = NULL WHERE assignee_id = $1", uid)  # NOT NULL
await conn.execute("UPDATE todos SET created_by  = NULL WHERE created_by  = $1", uid)  # NOT NULL
await conn.execute("DELETE FROM users WHERE id = $1", uid)
```

`todos.assignee_id` and `.created_by` are `NOT NULL` (`init.sql:444-445`,
confirmed on the live schema; no migration ever altered this table). The block
has no transaction wrapper, so:

1. the two updates above commit,
2. the third raises `NotNullViolationError`, which propagates — there is no
   `try/except` in this block,
3. `DELETE FROM users` never executes.

`DELETE /api/v1/my/account` (`access.py:216`) returns 500, the user row
survives, a partial cleanup is committed, and every retry fails identically —
**the account cannot be deleted**.

Trigger: the user holds a `todos` row in a vault they do not own (rows in
their own vaults are already cleared by `delete_vault`), or a row with
`vault_id IS NULL`, which no cascade touches.

The bug predates PR #43 — `git blame` puts both lines in `d6f1b56`, the first
OSS snapshot — and stayed latent because self-deletion is rare and the e2e
suites invoke the endpoint only as teardown with output discarded.

## Design of the removal

**Archive, then drop (migration 050).** `CREATE TABLE todos_archive AS SELECT
* FROM todos` then `DROP TABLE todos CASCADE`, both inside one transaction so
a crash between them cannot lose rows. CTAS deliberately copies data without
constraints, foreign keys, or indexes: the archive must not re-acquire the
`NOT NULL` / FK coupling to `users` that caused the bug, and nothing may
cascade into it. No inbound FK points at `todos`, so the drop cannot break
another table. Idempotent — the archive step is skipped
if `todos_archive` already exists, so a partial run resolves on retry.

**`init.sql` must lose the `CREATE` too.** `init_db()` runs `init.sql` on
every boot *before* applying migrations. Leaving `CREATE TABLE IF NOT EXISTS
todos` would recreate an empty table on the next boot while 050 stays recorded
in `schema_migrations` — a permanent resurrection. A tombstone comment holds
the place, matching migration 031's precedent.

**The archive has no reader and no retention machinery.** It exists so the
history is recoverable; operators may `DROP TABLE todos_archive` whenever they
are satisfied. Deliberately not wired into any cascade.

## Prose closure guards

Three surfaces still advertised the removed tools a year later:

- `mcp_server/instructions.py:8,:22` — sent to every MCP client at
  `initialize`. This session's own instructions block named `akb_todo` while
  the tool list did not contain it.
- `mcp_server/help.py:33` — a `todos` row in the root category table pointing
  at a drill-down topic that **never existed as a `HELP` key**, so
  `akb_help(topic="todos")` failed.
- `plugins/{claude,codex}/akb-sessions/skills/session-ingest/SKILL.md` —
  declared all three tools in `allowed-tools` and called them in §4-3. These
  ship to users via `.claude-plugin/marketplace.json`.

None of these are generated from `TOOLS`, which is why a tool removal could
miss them. `tests/test_mcp_prose_tool_closure_unit.py` now closes the loop by
AST: every `akb_*` name and every drill-down topic in instructions, help, and
the distributed plugin skills must resolve to a real tool or `HELP` key. The
proxy-only trio (`akb_put_file` / `akb_get_file` / `akb_delete_file`) is
allowlisted — the Node stdio proxy owns those and the backend never defines
them.

This is a first structural answer to cross-cutting theme 6 ("hand-authored
artifacts never checked against code") from the 2026-07-27 architecture
synthesis.

## Verification

Reproduced against a throwaway PostgreSQL on the pre-change schema — the
earlier investigation inferred the failure from schema, code, and data but
never executed it:

1. seeded a user with both blocking row shapes → the old statement sequence
   raised `NotNullViolationError`, the users row survived, and the retry hit
   the same wall;
2. migration 050 dropped the table and preserved every row in `todos_archive`
   with nullable user columns and zero FKs;
3. the post-change sequence deleted the account cleanly;
4. re-running 050 was a no-op;
5. re-applying the new `init.sql` did **not** resurrect `todos`.

Backend suite on Python 3.14.5 (CI parity): 1579 passed, 0 failed.

## Residual risk

- Rows survive only in `todos_archive`, inside the same database, so the
  archive is exactly as durable as the database itself and no more. Operators
  who need an independent copy should snapshot the table before upgrading.
- The archive retains `assignee_id` / `created_by` UUIDs of users who may
  later be deleted. The previous *intent* was to null those on account
  deletion; a frozen snapshot does not. Open question below.

## Open questions

- Retention: drop `todos_archive` after a fixed window, or leave it for the
  operator to remove? No mechanism is included.
- The `no_op` error code disappears from the catalogue with its last emitter
  (`todo_service`). Nothing in the frontend, SDK, or docs branches on it, and
  the repo's own orphan-constant test requires the removal, but it was a
  publicly documented code in the 0.x error table.
