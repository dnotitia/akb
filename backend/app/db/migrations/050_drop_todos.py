"""Migration 050: archive and drop the `todos` table.

`todos` backed the akb_todo / akb_todos / akb_todo_update MCP tools, removed
in PR #43 (`1c57350`, 2026-05-16) as "dead MCP tools — replaced by per-agent
task lists". That PR left the table and `todo_service` in place for a
"separate cleanup migration"; this is it.

The table has had no reader on any surface since: no REST router, no
frontend, no SDK/proxy, and zero importers of `todo_service`. With the tools
gone there is no writer either, so the row set is frozen.

Removing it also fixes a live bug. `access_service.delete_user_account`
wrote::

    UPDATE todos SET assignee_id = NULL WHERE assignee_id = $1
    UPDATE todos SET created_by  = NULL WHERE created_by  = $1

against columns declared NOT NULL. That block has no transaction wrapper, so
the preceding `vault_access` / `publications` updates committed, the
NotNullViolationError propagated, and the closing `DELETE FROM users` never
ran — `DELETE /api/v1/my/account` failed permanently for a user holding a
`todos` row outside their own vaults. Those writes are gone with the table.

Rows are preserved in `todos_archive`, a plain CTAS snapshot: same columns
and data, but no constraints, no foreign keys and no indexes. That is
deliberate — the archive must not re-acquire the NOT NULL / FK coupling to
`users` that caused the bug, and nothing may cascade into it. It has no
reader either; operators who no longer want the history can
``DROP TABLE todos_archive`` at any time. Operators who want the rows outside
the database should snapshot the table before upgrading.

Idempotent: re-running is a no-op once `todos` is gone. The archive and the
drop commit together, so there is no partial state to resolve — if the process
dies mid-migration the whole thing rolls back and the next boot starts over.

NOT reversible. Rolling back to an image whose `init.sql` still carries the
`CREATE TABLE todos` will recreate the table empty on the next boot, and this
migration will not run again because the ledger already records it. The
resurrected table is inert (no code reads or writes it), but removing it again
requires manual DDL.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.db.postgres import close_pool, get_pool, init_db

logger = logging.getLogger("akb.migration.050")


async def migrate(conn=None):
    if conn is None:
        pool = await get_pool()
        async with pool.acquire() as new_conn:
            await _run(new_conn)
    else:
        await _run(conn)


async def _run(conn):
    # Everything — the existence checks included — happens inside one
    # transaction under an ACCESS EXCLUSIVE lock. Two reasons:
    #
    #  * TOCTOU: checking outside the transaction lets the answer change
    #    before the archive is taken.
    #  * CTAS alone takes only ACCESS SHARE on the source, which does NOT
    #    block INSERT/UPDATE/DELETE. A row committed between the CTAS
    #    snapshot and the DROP would be destroyed without ever reaching the
    #    archive. The MCP tools are gone, but an unscoped admin's `akb_sql`
    #    runs as the connection default role and permits DML, and operators
    #    can always attach directly — so "no writer" is a statement about
    #    the product surfaces, not a guarantee about the database.
    async with conn.transaction():
        if await conn.fetchval("SELECT to_regclass('public.todos')") is None:
            logger.info("Migration 050: `todos` already absent — nothing to do.")
            return

        # LOCK blocks until it wins or `lock_timeout` (set by the caller)
        # fires; a timeout aborts this transaction and the runner retries.
        await conn.execute("LOCK TABLE public.todos IN ACCESS EXCLUSIVE MODE")

        if await conn.fetchval("SELECT to_regclass('public.todos_archive')") is not None:
            # This migration cannot produce this state: the archive and the
            # drop commit together, so it can never leave the archive behind
            # with `todos` still present. Both existing therefore means
            # something outside this code path created `todos_archive` — a
            # manual snapshot, a restored dump, or a hand-run of an earlier
            # revision. Silently keeping that archive and dropping the live
            # table could discard rows it never contained. Fail closed and
            # let an operator decide.
            raise RuntimeError(
                "Migration 050: both `todos` and `todos_archive` exist. This "
                "migration never produces that state, so `todos_archive` came "
                "from outside it and may not contain the rows currently in "
                "`todos`. Refusing to drop the source. Reconcile the two "
                "tables by hand (or rename/drop `todos_archive`), then re-run."
            )

        await conn.execute("CREATE TABLE public.todos_archive AS SELECT * FROM public.todos")
        archived = await conn.fetchval("SELECT COUNT(*) FROM public.todos_archive")
        # Plain DROP, not CASCADE: nothing in this repo depends on `todos`
        # (no inbound FK, view, or trigger — its own indexes and constraints
        # go with it either way). RESTRICT semantics mean an unexpected
        # site-local dependent fails loudly here instead of being silently
        # deleted.
        await conn.execute("DROP TABLE public.todos")

    logger.info(
        "Migration 050 dropped `todos` (%d row(s) archived to `todos_archive`). "
        "Its MCP tools went in PR #43; the account-deletion NOT NULL write in "
        "delete_user_account is removed with it.",
        archived,
    )


async def _main():
    await init_db()
    await migrate()
    await close_pool()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
