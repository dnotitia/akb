"""Cost hints for exact posting SQL; statistics never decide which rows match.

A common term can touch most chunks. In a small authorized scope, probing the
(term_id, chunk_id) index is cheaper. Rare terms should keep their term-first
scan. Missing statistics retain that general-purpose plan.
"""
from __future__ import annotations

import math
from typing import Literal

import asyncpg

ScopeStrategy = Literal["term", "scope", "point"]
MAX_POINT_CHUNKS = 10_000
MAX_POINT_PROBES = 100_000


def choose_scope_strategy(
    scope_rows: float | None, posting_rows: float | None, term_count: int,
    *, total_chunks: float,
) -> ScopeStrategy:
    if (scope_rows is None or posting_rows is None or term_count < 1
            or not all(math.isfinite(n) and n > 0 for n in (scope_rows, posting_rows, total_chunks))):
        return "term"
    probes = scope_rows * term_count
    if scope_rows <= MAX_POINT_CHUNKS and probes <= MAX_POINT_PROBES and posting_rows >= probes * 4:
        return "point"
    # A very broad scope gains little from materialization and may spill.
    if scope_rows < total_chunks * 0.8 and posting_rows >= scope_rows * 2:
        return "scope"
    return "term"


def _estimated_matches(row, values: list[str]) -> float | None:
    total = float(row["total"] or 0)
    distinct = float(row["n_distinct"] or 0)
    if total <= 0 or distinct == 0:
        return None
    distinct = -distinct * total if distinct < 0 else distinct
    common = dict(zip(row["mcv"] or [], row["freqs"] or []))
    other_frequency = max(0, 1 - sum(common.values())) / max(1, distinct - len(common))
    return min(total, total * sum(common.get(value, other_frequency) for value in set(values)))


async def estimate_scope_strategy(
    conn: asyncpg.Connection, *, schema: str, filter_col: str,
    filter_uuids, terms: list[int],
) -> ScopeStrategy:
    rows = await conn.fetch(
        """SELECT c.relname, c.reltuples::float8 AS total, s.n_distinct,
                  s.most_common_vals::text::text[] AS mcv, s.most_common_freqs AS freqs
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           JOIN pg_stats s ON s.schemaname=n.nspname AND s.tablename=c.relname
           WHERE n.nspname=$1
             AND ((c.relname='chunks' AND s.attname=$2)
               OR (c.relname='posting' AND s.attname='term_id'))""",
        schema, filter_col,
    )
    stats = {row["relname"]: row for row in rows}
    if "chunks" not in stats or "posting" not in stats:
        return "term"
    return choose_scope_strategy(
        _estimated_matches(stats["chunks"], [str(value) for value in filter_uuids]),
        _estimated_matches(stats["posting"], [str(value) for value in terms]),
        len(terms), total_chunks=float(stats["chunks"]["total"]),
    )
