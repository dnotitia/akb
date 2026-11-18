from pathlib import Path


async def migrate(conn):
    await conn.execute(Path(__file__).with_suffix(".sql").read_text())
