"""Give existing registered tables the same default page index as new ones.

No approximate counts or per-query planner settings. The index is maintained
by PostgreSQL for every write path, including user SQL. Like other schema
migrations, creation is atomic with its receipt and may block writes while
building; large installations should schedule the first upgrade accordingly.
"""

from app.db.postgres import get_pool
from app.repositories.table_data_repo import install_default_page_index, pg_table_name


async def migrate(conn=None):
    if conn is not None:
        await _run(conn)
        return
    pool = await get_pool()
    async with pool.acquire() as connection:
        await _run(connection)


async def _run(conn):
    rows = await conn.fetch("""
        SELECT v.name AS vault_name, vt.name AS table_name
        FROM vault_tables vt JOIN vaults v ON v.id=vt.vault_id
        ORDER BY v.name, vt.name
    """)
    for row in rows:
        physical = pg_table_name(row["vault_name"], row["table_name"])
        if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", f"public.{physical}"):
            await install_default_page_index(conn, physical)
