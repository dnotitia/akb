"""Database-owned progress for an interrupted BM25 full sweep.

The row lives beside the vector chunks so a database restore cannot retain a
cursor newer than the restored data. It is a visited-prefix receipt, never a
freshness or cutover certificate. The caller still owns the migration locks and
must run its protected-window catch-up after old posting writers drain.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any


_ZERO = uuid.UUID(int=0)


def _table(schema: str) -> str:
    escaped = schema.replace('"', '""')
    return f'"{escaped}".bm25_backfill_checkpoint'


@dataclass
class SweepCheckpoint:
    pool: Any
    schema: str
    sweep_id: uuid.UUID
    attempt_id: uuid.UUID
    cursor: uuid.UUID
    sequence: int
    seen: int
    written: int

    async def advance(self, cursor: uuid.UUID, seen: int, written: int) -> None:
        """Acknowledge a contiguous writer wave, after every UPDATE committed."""
        if cursor <= self.cursor or seen <= 0 or not 0 <= written <= seen:
            raise ValueError("invalid checkpoint wave")
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""UPDATE {_table(self.schema)}
                       SET last_chunk_id=$1, seen=seen+$2, written=written+$3,
                           sequence=sequence+1, updated_at=clock_timestamp()
                     WHERE sweep_id=$4 AND attempt_id=$5 AND sequence=$6
                       AND last_chunk_id=$7 AND NOT exhausted
                 RETURNING sequence, seen, written""",
                cursor, seen, written, self.sweep_id, self.attempt_id,
                self.sequence, self.cursor,
            )
        if row is None:
            raise RuntimeError("BM25 sweep checkpoint ownership or cursor changed")
        self.cursor = cursor
        self.sequence = row["sequence"]
        self.seen = row["seen"]
        self.written = row["written"]

    async def mark_exhausted(self) -> None:
        """Mark end-of-keyspace only; concurrent-writer convergence is separate."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""UPDATE {_table(self.schema)}
                       SET exhausted=TRUE, sequence=sequence+1,
                           updated_at=clock_timestamp()
                     WHERE sweep_id=$1 AND attempt_id=$2 AND sequence=$3
                       AND last_chunk_id=$4 AND NOT exhausted
                 RETURNING sequence""",
                self.sweep_id, self.attempt_id, self.sequence, self.cursor,
            )
        if row is None:
            raise RuntimeError("BM25 sweep checkpoint ownership changed at exhaustion")
        self.sequence = row["sequence"]


async def open_checkpoint(
    pool,
    schema: str,
    *,
    sweep_id: uuid.UUID,
    attempt_id: uuid.UUID,
    since: datetime,
    protect_since: datetime,
    source_revision: str,
    encoding_contract: str,
    resume: bool,
) -> SweepCheckpoint:
    """Create or claim one immutable logical sweep under both migration locks.

    A new attempt must explicitly claim an existing row. A different source,
    window, schema or chunks relation cannot silently inherit its progress.
    """
    if since.tzinfo is None or protect_since.tzinfo is None:
        raise ValueError("BM25 sweep windows must have timezones")
    if not source_revision or not encoding_contract:
        raise ValueError("BM25 sweep source and encoding contract are required")
    table = _table(schema)
    async with pool.acquire() as conn:
        await conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {table} (
                    sweep_id UUID PRIMARY KEY,
                    attempt_id UUID NOT NULL,
                    since_at TIMESTAMPTZ NOT NULL,
                    protect_since_at TIMESTAMPTZ NOT NULL,
                    source_revision TEXT NOT NULL,
                    encoding_contract TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    chunks_oid BIGINT NOT NULL,
                    last_chunk_id UUID NOT NULL,
                    sequence BIGINT NOT NULL DEFAULT 0,
                    seen BIGINT NOT NULL DEFAULT 0,
                    written BIGINT NOT NULL DEFAULT 0,
                    exhausted BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                    CHECK (seen >= written AND written >= 0)
                )"""
        )
        chunks_oid = await conn.fetchval(
            "SELECT c.oid::bigint FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=$1 AND c.relname='chunks' AND c.relkind='r'",
            schema,
        )
        if chunks_oid is None:
            raise RuntimeError("BM25 sweep chunks table is missing")
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT * FROM {table} WHERE sweep_id=$1 FOR UPDATE", sweep_id
            )
            if resume:
                if row is None:
                    raise RuntimeError("BM25 sweep checkpoint does not exist")
                contract = (
                    ("since_at", since),
                    ("protect_since_at", protect_since),
                    ("source_revision", source_revision),
                    ("encoding_contract", encoding_contract),
                    ("schema_name", schema),
                    ("chunks_oid", chunks_oid),
                )
                if any(row[key] != expected for key, expected in contract):
                    raise RuntimeError("BM25 sweep checkpoint contract changed")
                if row["exhausted"]:
                    raise RuntimeError("BM25 sweep already exhausted; run catch-up")
                if row["attempt_id"] == attempt_id:
                    raise RuntimeError("BM25 sweep resume requires a new attempt ID")
                row = await conn.fetchrow(
                    f"""UPDATE {table}
                           SET attempt_id=$1, sequence=sequence+1,
                               updated_at=clock_timestamp()
                         WHERE sweep_id=$2 AND attempt_id=$3 AND sequence=$4
                     RETURNING *""",
                    attempt_id, sweep_id, row["attempt_id"], row["sequence"],
                )
                if row is None:
                    raise RuntimeError("BM25 sweep checkpoint claim changed")
            else:
                if row is not None:
                    raise RuntimeError("BM25 sweep ID already exists; claim explicitly")
                row = await conn.fetchrow(
                    f"""INSERT INTO {table} (
                            sweep_id, attempt_id, since_at, protect_since_at,
                            source_revision, encoding_contract, schema_name,
                            chunks_oid, last_chunk_id
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                        RETURNING *""",
                    sweep_id, attempt_id, since, protect_since, source_revision,
                    encoding_contract, schema, chunks_oid, _ZERO,
                )
    return SweepCheckpoint(
        pool=pool, schema=schema, sweep_id=sweep_id, attempt_id=attempt_id,
        cursor=row["last_chunk_id"], sequence=row["sequence"],
        seen=row["seen"], written=row["written"],
    )
