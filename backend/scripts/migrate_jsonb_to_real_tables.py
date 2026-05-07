"""Migrate JSONB vault_table_rows to real PostgreSQL tables.

Run inside backend pod:
  python3 scripts/migrate_jsonb_to_real_tables.py
"""

import asyncio
import json
import os
import re

import asyncpg

TYPE_MAP = {
    "text": "TEXT",
    "number": "NUMERIC",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "json": "JSONB",
}


def pg_table_name(vault_name: str, table_name: str) -> str:
    v = re.sub(r"[^a-z0-9]", "_", vault_name.lower())
    t = re.sub(r"[^a-z0-9_]", "_", table_name.lower().replace("-", "_"))
    return f"vt_{v}__{t}"


def safe_ident(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


async def migrate():
    db_url = (
        f"postgresql://{os.getenv('AKB_DB_USER', 'akbuser')}"
        f":{os.getenv('AKB_DB_PASSWORD', 'akbpass')}"
        f"@{os.getenv('AKB_DB_HOST', 'postgres')}"
        f":{os.getenv('AKB_DB_PORT', '5432')}"
        f"/{os.getenv('AKB_DB_NAME', 'akb')}"
    )
    conn = await asyncpg.connect(db_url)
    print(f"Connected to {os.getenv('AKB_DB_HOST', 'postgres')}")

    # Get all vault tables
    tables = await conn.fetch("""
        SELECT vt.id, vt.name, vt.columns, v.name as vault_name
        FROM vault_tables vt JOIN vaults v ON vt.vault_id = v.id
    """)
    print(f"Tables to migrate: {len(tables)}")

    for t in tables:
        tbl = pg_table_name(t["vault_name"], t["name"])
        raw_cols = t["columns"]
        cols_meta = json.loads(raw_cols) if isinstance(raw_cols, str) else list(raw_cols)

        # Build CREATE TABLE DDL
        col_defs = ["id UUID PRIMARY KEY DEFAULT uuid_generate_v4()"]
        for c in cols_meta:
            cn = safe_ident(c["name"])
            ct = TYPE_MAP.get(c.get("type", "text"), "TEXT")
            col_defs.append(f"{cn} {ct}")
        col_defs.append("created_by TEXT")
        col_defs.append("created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
        col_defs.append("updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")

        await conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        ddl = f"CREATE TABLE {tbl} ({', '.join(col_defs)})"
        await conn.execute(ddl)
        print(f"  Created: {tbl}")

        # Copy rows from JSONB
        rows = await conn.fetch(
            "SELECT data, created_by, created_at FROM vault_table_rows WHERE table_id = $1",
            t["id"],
        )
        migrated = 0
        errors = 0
        for row in rows:
            raw_data = row["data"]
            data = json.loads(raw_data) if isinstance(raw_data, str) else dict(raw_data)

            insert_cols = []
            insert_vals = []
            for c in cols_meta:
                cn = safe_ident(c["name"])
                # Try both original name and safe name
                val = data.get(c["name"])
                if val is None:
                    val = data.get(cn)
                if val is not None:
                    insert_cols.append(cn)
                    # Type conversion
                    ctype = c.get("type", "text")
                    if ctype == "number" and isinstance(val, str):
                        try:
                            val = float(val)
                        except ValueError:
                            pass
                    elif ctype == "boolean" and isinstance(val, str):
                        val = val.lower() in ("true", "1", "yes")
                    elif ctype == "date" and isinstance(val, str):
                        from datetime import date as _date
                        try:
                            val = _date.fromisoformat(val)
                        except ValueError:
                            val = None
                    insert_vals.append(val)

            if not insert_cols:
                continue

            insert_cols.extend(["created_by", "created_at"])
            insert_vals.extend([row["created_by"], row["created_at"]])

            placeholders = ", ".join(f"${i+1}" for i in range(len(insert_cols)))
            sql = f"INSERT INTO {tbl} ({', '.join(insert_cols)}) VALUES ({placeholders})"

            try:
                await conn.execute(sql, *insert_vals)
                migrated += 1
            except Exception as e:
                errors += 1
                print(f"    Row error: {e}")

        print(f"  Migrated: {migrated}/{len(rows)} rows ({errors} errors)")

    # Verify
    print("\n=== Verification ===")
    vtables = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE tablename LIKE 'vt_%' ORDER BY tablename"
    )
    for vt in vtables:
        count = await conn.fetchval(f"SELECT COUNT(*) FROM {vt['tablename']}")
        print(f"  {vt['tablename']}: {count} rows")

    print(f"\nTotal real tables: {len(vtables)}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(migrate())
