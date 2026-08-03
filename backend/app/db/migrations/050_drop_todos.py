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

Idempotent: re-running is a no-op once `todos` is gone. The archive step is
skipped if `todos_archive` already exists, so a partial run that created the
archive and died before the drop resolves correctly on retry.
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
    if await conn.fetchval("SELECT to_regclass('public.todos')") is None:
        logger.info("Migration 050: `todos` already absent — nothing to do.")
        return

    # Archive + drop as one unit so a crash between them cannot lose rows.
    # No FK points AT todos, so the drop cannot cascade-break another table;
    # CASCADE only sheds todos' own indexes.
    async with conn.transaction():
        archived = 0
        if await conn.fetchval("SELECT to_regclass('public.todos_archive')") is None:
            await conn.execute("CREATE TABLE todos_archive AS SELECT * FROM todos")
            archived = await conn.fetchval("SELECT COUNT(*) FROM todos_archive")
        else:
            logger.warning(
                "Migration 050: `todos_archive` already exists — keeping it as-is "
                "and dropping `todos` (partial earlier run)."
            )
        await conn.execute("DROP TABLE IF EXISTS todos CASCADE")

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
