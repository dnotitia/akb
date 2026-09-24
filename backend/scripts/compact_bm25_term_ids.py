#!/usr/bin/env python3
"""Renumber BM25 term ids densely (akb#687).

    python -m scripts.compact_bm25_term_ids            # dry run: the plan, and what forbids it
    python -m scripts.compact_bm25_term_ids --apply    # renumber, verify, commit, or roll back
    python -m scripts.compact_bm25_term_ids --revert   # put the recorded numbering back

WHY
---
`bm25_vocab` gives every term an id from `bm25_term_id_seq`. Before akb#664 and
akb#687 the encoder and the statistics recompute drew ids they then discarded,
so an installation can hold far more id space than terms: one had 955,060 terms
over ids up to 728,985,301. The `vchord` shape's index sizes its per-term arrays
by the largest id rather than by the number of terms. That installation's index
was 12.6 GB against 6.7 GB densely numbered, built in 223 s against 88 s, and
every VACUUM walked the empty range. An id of 2^30 or more also aliases another
term inside the index, because its arrays are addressed by u32 byte offsets.

A term id is a label. BM25 scores come from tf, df, N and the average document
length, so renumbering changes no score. The new id of a term is the rank of its
old one, `new_id = rank(old_id) - 1`. The mapping is monotone, so every vector
keeps its order and only its labels change.

WHAT CHANGES, IN ONE TRANSACTION
--------------------------------
- `bm25_vocab.term_id`, and `bm25_term_id_seq`, which continues after the last
  id.
- The active shape's copies of the ids:
  - `vchord`: `<schema>.chunks.sparse_bm25`, and the BM25 index built over it;
  - `posting`: `<schema>.posting.term_id`.
- `bm25_vocab_epoch`, advanced by one.
- `bm25_term_id_remap` records the mapping (term, old id, new id), and
  `bm25_term_id_remap_run` records the run. `--revert` reads both.

The vectors are rewritten with `ALTER TABLE ... ALTER COLUMN ... TYPE ...
USING`, a table rewrite that rebuilds every index on the table from the new
rows. Updating the rows and building the index in the same transaction does
not work: the build also indexes the old row versions that transaction deleted,
and it counts them. Measured: twice the documents, and a `term_id_cnt` still at
the old largest id. The rewrite copies live rows only and leaves no dead ones.
The price is that EVERY index on the table is rebuilt, including a dense HNSW
index where the table has one. That build, not the renumbering, sets the length
of the window on a large corpus. The dry run lists the indexes and their sizes.

The sequence restarts with `ALTER SEQUENCE ... RESTART`, which is
transactional. `setval` is not, and a rollback after it would leave the
sequence handing out ids the vocabulary still uses.

CONCURRENCY: THE VOCABULARY EPOCH
---------------------------------
Encoding a chunk and storing it are two transactions. Ids read before this
commit and stored after it would name other terms. Every writer therefore
stores ids under the term-id fence, `BM25_VOCAB_EPOCH_LOCK_KEY` held shared,
and refuses ids read under an epoch other than the current one; the chunk is
encoded again. This transaction:

1. holds the fence exclusively, which waits for writers in flight and turns
   new ones away (indexing hands its batch back and retries after the commit);
2. locks `bm25_vocab` and the rewritten table exclusively, which holds back
   encoders and searches until the commit;
3. advances the epoch in the same commit as the new ids.

The bulk maintenance guard (`scripts/bm25_run_lock.run_bulk_exclusive`) keeps
the statistics recompute and the `sparse_bm25` backfill, which also write
term ids, from running beside it.

This protects only processes that run the fence. EVERY backend process must run
a version that has it before `--apply`: an older one can still store ids from
before the commit. A database without `bm25_vocab_epoch` (migration 113) has
certainly not been upgraded, and the command refuses it.

VERIFICATION, BEFORE THE COMMIT
-------------------------------
- The vocabulary holds the same terms, each at the id the mapping gives it, and
  under `--apply` the ids are exactly 0 .. terms - 1.
- `vchord`: the rebuilt index counts the same documents and the same total
  length, and its `term_id_cnt` does not exceed the largest new id + 1; that
  bounds every id in every indexed vector. `posting`: the same number of rows,
  and every id within the new range.
- A baseline of queries, sampled from stored documents, ranks the same
  documents with the same scores, within 1e-6, before and after. Ties at the
  last score may reorder.
- The sequence continues after the new ids, and the epoch has moved by one.

Any failure rolls the whole transaction back and exits 1: nothing is changed.

The baseline compares against the index as it is. Its statistics must count
only live rows, or the rebuilt index would score differently for a reason
unrelated to the ids: the index counts an updated or deleted row until VACUUM
takes it out. The command refuses an index whose document count differs from
the rows that hold a vector. Pause writers and run VACUUM on the table first.

REFUSALS (exit 2, nothing changed)
----------------------------------
- A driver that keeps its vectors outside PostgreSQL (Qdrant, SeahorseDB,
  Seahorse Cloud): one transaction cannot rewrite them. Renumbering there
  needs every chunk indexed again.
- A vector index in a separate database (`vector_store_dsn`).
- The `arrays` shape, which is kept for the bench harness.
- Term ids left in a shape that is not active: `posting` rows under `vchord`,
  or `sparse_bm25` vectors under `posting`. They would keep the old numbering.
- An index whose statistics include rows VACUUM has not removed yet.
- A fence or table lock not free within `--lock-timeout` seconds.
- The bulk guard held by the statistics recompute or a backfill.

DURING THE WINDOW
-----------------
`<schema>.chunks` (or `posting`) is exclusively locked until the commit, so
searches, dense and sparse alike, wait for it. Indexing hands its batches back
and resumes after the commit. Nothing is lost: chunks written while the command
waits stay queued.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncpg

from scripts.bm25_run_lock import run_bulk_exclusive

from app.config import settings
from app.db.postgres import close_pool
from app.services.bm25_maintenance import BM25_VOCAB_EPOCH_LOCK_KEY
from app.services.vector_store import decide_sparse_shape_for_settings
from app.services.vector_store.sparse_shape_state import SparseShapeUndecidable

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2

_INDEX = "idx_vi_chunks_bm25"
_MAPPING = "bm25_term_id_remap"
_MAPPING_RUN = "bm25_term_id_remap_run"
# The extension addresses its per-term arrays by u32 byte offsets, four bytes
# per id: an id from here on wraps onto the id 2^30 below it.
_ALIASING_ID = 1 << 30
_TOLERANCE = 1e-6
_QUERIES = 24
_TOP_K = 20
_LOCK_TIMEOUT_SECS = 10.0
_MAINTENANCE_WORK_MEM = "1GB"
_SCHEMA_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_MEMORY_RE = re.compile(r"^[1-9][0-9]*(kB|MB|GB|TB)?$")


class Refused(Exception):
    """A precondition does not hold. Nothing was changed."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class VerificationFailed(Exception):
    """What the renumbering produced did not check out; it was rolled back."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


# ── What is there ─────────────────────────────────────────────────


@dataclass
class Survey:
    """The facts a renumbering starts from, and anything that forbids it."""

    schema: str
    shape: str
    terms: int = 0
    min_id: int | None = None
    max_id: int | None = None
    sequence_next: int | None = None
    epoch: int | None = None
    # Rows holding a vector under `vchord`, rows of `posting` under `posting`.
    vectors: int | None = None
    metapage: dict[str, int] | None = None
    index_bytes: int | None = None
    # What the rewrite rebuilds: (index, bytes).
    rebuilt: list[tuple[str, int]] = field(default_factory=list)
    mapping: dict[str, Any] | None = None
    queries: list[tuple[str, ...]] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    @property
    def dense(self) -> bool:
        """The ids are exactly 0 .. terms - 1, the numbering this command makes."""
        return self.terms == 0 or self.max_id == self.terms - 1

    @property
    def rewritten(self) -> str:
        return f'"{self.schema}".{"posting" if self.shape == "posting" else "chunks"}'


async def _metapage(conn, schema: str) -> dict[str, int]:
    text = await conn.fetchval(
        "SELECT bm25_catalog.bm25_page_inspect($1::regclass, 0)", f'"{schema}".{_INDEX}',
    )

    def field_(name: str) -> int:
        return int(re.search(rf"\b{name}: (\d+)", text).group(1))

    return {
        "doc_cnt": field_("doc_cnt"),
        "doc_term_cnt": field_("doc_term_cnt"),
        # The page carries it twice: the index's own, and its sealed segment's.
        "term_id_cnt": max(int(n) for n in re.findall(r"\bterm_id_cnt: (\d+)", text)),
        # The high bit of `version`: VACUUM took documents off the counts and
        # the per-term recount it owes has not finished.
        "recount_owed": int(bool(field_("version") & (1 << 31))),
    }


async def _columns(conn, schema: str, table: str) -> set[str]:
    return {
        row["column_name"]
        for row in await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = $2",
            schema, table,
        )
    }


async def _exists(conn, relation: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", relation))


async def survey(conn, *, schema: str, shape: str, queries: int = _QUERIES) -> Survey:
    """Read what a renumbering would start from. Changes nothing."""
    facts = Survey(schema=schema, shape=shape)
    refuse = facts.refusals.append

    if not await _exists(conn, "bm25_vocab_epoch"):
        refuse(
            "bm25_vocab_epoch does not exist: this database has not run migration 113. "
            "Every backend process must run a version that stores term ids under the "
            "vocabulary epoch before they can be renumbered; upgrade them all first."
        )
        return facts

    vocab = await conn.fetchrow(
        "SELECT count(*) AS terms, min(term_id) AS lo, max(term_id) AS hi FROM bm25_vocab"
    )
    facts.terms, facts.min_id, facts.max_id = int(vocab["terms"]), vocab["lo"], vocab["hi"]
    sequence = await conn.fetchrow("SELECT last_value, is_called FROM bm25_term_id_seq")
    facts.sequence_next = int(sequence["last_value"]) + (1 if sequence["is_called"] else 0)
    facts.epoch = int(await conn.fetchval("SELECT epoch FROM bm25_vocab_epoch WHERE id = 1"))
    if await _exists(conn, _MAPPING_RUN):
        facts.mapping = dict(await conn.fetchrow(f"SELECT * FROM {_MAPPING_RUN}") or {}) or None

    chunks = f'"{schema}".chunks'
    if not await _exists(conn, chunks):
        refuse(f"{chunks} does not exist; there is no vector index to renumber.")
        return facts
    columns = await _columns(conn, schema, "chunks")
    has_posting = await _exists(conn, f'"{schema}".posting')

    if shape == "arrays":
        refuse("the arrays shape is kept for the bench harness; this command does not rewrite it.")
        return facts

    if "sparse_terms" in columns and await conn.fetchval(
        f"SELECT EXISTS (SELECT 1 FROM {chunks} WHERE cardinality(sparse_terms) > 0)"
    ):
        refuse(
            f"{chunks}.sparse_terms holds term ids left by the arrays shape. They would keep "
            "the old numbering; clear the column first."
        )

    if shape == "vchord":
        if has_posting and await conn.fetchval(f'SELECT EXISTS (SELECT 1 FROM "{schema}".posting)'):
            refuse(
                f'"{schema}".posting still holds rows, kept as the way back to the posting shape. '
                "Their term ids would keep the old numbering. Empty or drop that table once "
                "the way back is no longer needed, then run this again."
            )
        valid = await conn.fetchval(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass($1)",
            f'"{schema}".{_INDEX}',
        )
        if valid is None:
            refuse(f'"{schema}".{_INDEX} does not exist; build it before renumbering.')
            return facts
        if not valid:
            refuse(f'"{schema}".{_INDEX} is INVALID; drop it and build it again first.')
            return facts
        facts.vectors = int(await conn.fetchval(
            f"SELECT count(*) FROM {chunks} WHERE sparse_bm25 IS NOT NULL"
        ))
        facts.metapage = await _metapage(conn, schema)
        facts.index_bytes = int(await conn.fetchval(
            "SELECT pg_relation_size(to_regclass($1))", f'"{schema}".{_INDEX}',
        ))
        if facts.metapage["doc_cnt"] != facts.vectors or facts.metapage["recount_owed"]:
            owed = " and owes a term recount" if facts.metapage["recount_owed"] else ""
            refuse(
                f"the index counts {facts.metapage['doc_cnt']:,} documents{owed}, and "
                f"{facts.vectors:,} rows hold a vector: its statistics still include rows "
                "VACUUM has not removed, so the baseline scores would not be the live rows'. "
                f"With writers paused, run VACUUM {chunks} and then this again."
            )
    elif shape == "posting":
        if not has_posting:
            refuse(f'"{schema}".posting does not exist; there is nothing to renumber.')
            return facts
        if "sparse_bm25" in columns and await conn.fetchval(
            f"SELECT EXISTS (SELECT 1 FROM {chunks} "
            "WHERE sparse_bm25 IS NOT NULL AND sparse_bm25::text <> '{}')"
        ):
            refuse(
                f"{chunks}.sparse_bm25 holds vectors while posting serves: a backfill toward "
                "vchord. They would keep the old numbering. Finish the move to vchord, or "
                "clear the column, then run this again."
            )
        facts.vectors = int(await conn.fetchval(f'SELECT count(*) FROM "{schema}".posting'))
    else:
        refuse(f"unknown sparse shape {shape!r}.")
        return facts

    facts.rebuilt = [
        (row["name"], int(row["bytes"]))
        for row in await conn.fetch(
            """
            SELECT c.relname AS name, pg_relation_size(c.oid) AS bytes
              FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
             WHERE i.indrelid = ANY(ARRAY[to_regclass($1), 'bm25_vocab'::regclass])
             ORDER BY pg_relation_size(c.oid) DESC, c.relname
            """,
            facts.rewritten,
        )
    ]
    facts.queries = await _sample_queries(conn, schema=schema, shape=shape, count=queries)
    return facts


async def _sample_queries(conn, *, schema: str, shape: str, count: int) -> list[tuple[str, ...]]:
    """Queries made of terms real documents hold, the same ones on every run.

    Each probe lands on the first document at or after an evenly spaced chunk
    id, and takes one, two or three of its terms. Chunk ids are random UUIDs,
    so the probes spread over the corpus, and the primary key finds each one.
    Kept as term strings: after the renumbering the vocabulary resolves them
    again, which is part of what is verified.
    """
    queries: dict[tuple[str, ...], None] = {}
    for position in range(count):
        probe = uuid.UUID(int=(position * (1 << 128)) // count)
        if shape == "vchord":
            vector = await conn.fetchval(
                f'SELECT sparse_bm25::text FROM "{schema}".chunks '
                "WHERE chunk_id >= $1 AND sparse_bm25 IS NOT NULL AND sparse_bm25::text <> '{}' "
                "ORDER BY chunk_id LIMIT 1",
                probe,
            )
            ids = [int(pair.split(":")[0]) for pair in vector.strip("{}").split(", ")] if vector else []
        else:
            ids = await conn.fetchval(
                f'SELECT array_agg(term_id ORDER BY term_id) FROM "{schema}".posting '
                f'WHERE chunk_id = (SELECT chunk_id FROM "{schema}".posting '
                "WHERE chunk_id >= $1 ORDER BY chunk_id LIMIT 1)",
                probe,
            ) or []
        if not ids:
            continue
        picked = {1: [ids[len(ids) // 2]], 2: [ids[0], ids[-1]]}.get(
            position % 3 + 1, [ids[0], ids[len(ids) // 2], ids[-1]]
        )
        terms = await conn.fetch(
            "SELECT term FROM bm25_vocab WHERE term_id = ANY($1::bigint[]) ORDER BY term",
            sorted(set(picked)),
        )
        if terms:
            queries[tuple(row["term"] for row in terms)] = None
    return list(queries)


# ── Rankings ──────────────────────────────────────────────────────


async def _ranking(conn, *, schema: str, shape: str, query: tuple[str, ...], top_k: int):
    """The exact top-k for these terms, through the ids the vocabulary gives now.

    Ascending scores, best first, for both shapes: `<&>` is a negative BM25
    score, and posting's sum is negated to match.
    """
    ids = [
        int(row["term_id"])
        for row in await conn.fetch(
            "SELECT term_id FROM bm25_vocab WHERE term = ANY($1::text[]) ORDER BY term_id",
            list(query),
        )
    ]
    if not ids:
        return []
    if shape == "vchord":
        rows = await conn.fetch(
            f"""
            SELECT id, score FROM (
              SELECT c.chunk_id::text AS id,
                     c.sparse_bm25 <&> bm25_catalog.to_bm25query(
                         '"{schema}".{_INDEX}'::regclass,
                         $1::text::bm25_catalog.bm25vector) AS score
                FROM "{schema}".chunks c
               WHERE c.sparse_bm25 IS NOT NULL
               ORDER BY score LIMIT $2) ranked
             WHERE score < 0
             ORDER BY score, id
            """,
            "{" + ", ".join(f"{term_id}:1" for term_id in ids) + "}",
            top_k,
        )
    else:
        rows = await conn.fetch(
            f"""
            SELECT p.chunk_id::text AS id, -sum(p.weight::float8) AS score
              FROM "{schema}".posting p
             WHERE p.term_id = ANY($1::bigint[])
             GROUP BY p.chunk_id
             ORDER BY score, id
             LIMIT $2
            """,
            ids,
            top_k,
        )
    return [(row["id"], float(row["score"])) for row in rows]


async def _rankings(conn, facts: Survey, top_k: int) -> dict[tuple[str, ...], list]:
    return {
        query: await _ranking(conn, schema=facts.schema, shape=facts.shape, query=query, top_k=top_k)
        for query in facts.queries
    }


def _ranking_difference(before, after) -> str | None:
    """Why two rankings of one query differ, or None when they do not.

    Every rank keeps its score. The documents scoring above the last score in
    the page are the same ones; documents tied at that score may swap in and
    out, and ties anywhere may reorder.
    """
    if len(before) != len(after):
        return f"{len(before)} results before and {len(after)} after"
    for rank, ((_, was), (_, now)) in enumerate(zip(before, after), start=1):
        if not math.isclose(was, now, rel_tol=_TOLERANCE, abs_tol=_TOLERANCE):
            return f"rank {rank} scored {was!r} before and {now!r} after"
    if not before:
        return None
    boundary = before[-1][1]
    above_before = {doc for doc, score in before if score < boundary - _TOLERANCE}
    above_after = {doc for doc, score in after if score < boundary - _TOLERANCE}
    if above_before != above_after:
        return f"documents above the last score differ: {sorted(above_before ^ above_after)[:3]}"
    scores = dict(after)
    for doc, was in before:
        if doc in scores and not math.isclose(was, scores[doc], rel_tol=_TOLERANCE, abs_tol=_TOLERANCE):
            return f"document {doc} scored {was!r} before and {scores[doc]!r} after"
    return None


# ── The window ────────────────────────────────────────────────────


async def _enter_window(conn, *, schema: str, shape: str, lock_timeout: float, maintenance_work_mem: str) -> None:
    """Session settings and locks, in the one order that cannot deadlock.

    The fence first: it waits for the transactions storing term ids that are
    in flight, and nothing that holds it shared waits on this transaction. Then
    the vocabulary, which encoders touch outside any fenced transaction. Then
    the table the rewrite replaces.
    """
    await conn.execute(f"SET LOCAL lock_timeout = '{int(lock_timeout * 1000)}ms'")
    await conn.execute(f"SET LOCAL maintenance_work_mem = '{maintenance_work_mem}'")
    # The extension's `to_bm25query` resolves its own type without a schema.
    await conn.execute('SET LOCAL search_path TO "$user", public, bm25_catalog')

    try:
        await conn.execute("SELECT pg_advisory_xact_lock($1)", BM25_VOCAB_EPOCH_LOCK_KEY)
    except asyncpg.exceptions.LockNotAvailableError:
        raise Refused([
            f"a transaction storing term ids held the fence for {lock_timeout:g}s; "
            "run this again when indexing is quiet, or with a longer --lock-timeout"
        ]) from None
    for table in ("bm25_vocab", f'"{schema}".{"posting" if shape == "posting" else "chunks"}'):
        if table != "bm25_vocab" and not await _exists(conn, table):
            continue  # the survey that follows says why
        try:
            await conn.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
        except asyncpg.exceptions.LockNotAvailableError:
            raise Refused([
                f"{table} stayed in use for {lock_timeout:g}s; run this again when it is "
                "quiet, or with a longer --lock-timeout"
            ]) from None
    if shape == "vchord" and await _exists(conn, f'"{schema}".{_INDEX}'):
        # Its settings exist once the library is loaded; -1 is the exact scan.
        await conn.fetchval("SELECT '{}'::bm25_catalog.bm25vector IS NOT NULL")
        await conn.execute("SET LOCAL bm25_catalog.bm25_limit = -1")


_RELABEL_ID = """
CREATE OR REPLACE FUNCTION pg_temp.akb_bm25_relabel_id(id bigint) RETURNS bigint
LANGUAGE plpgsql STABLE STRICT AS $fn$
DECLARE
    target bigint;
BEGIN
    SELECT m.target_id INTO target FROM pg_temp.akb_bm25_relabel_map m WHERE m.current_id = id;
    IF target IS NULL THEN
        RAISE EXCEPTION 'term id % is not in bm25_vocab', id;
    END IF;
    RETURN target;
END
$fn$
"""

# The vector's pairs in their order, each id replaced. A monotone map keeps
# them increasing, and the type's input refuses a vector that is not: a map
# that broke the order fails here instead of storing it. An id the map does not
# hold fails too, instead of dropping the term from the document.
_RELABEL_VECTOR = """
CREATE OR REPLACE FUNCTION pg_temp.akb_bm25_relabel_vector(v bm25_catalog.bm25vector)
RETURNS bm25_catalog.bm25vector
LANGUAGE plpgsql STABLE STRICT AS $fn$
DECLARE
    relabelled text;
    unknown bigint;
BEGIN
    SELECT count(*) - count(m.target_id),
           '{' || coalesce(string_agg(m.target_id::text || ':' || split_part(p.pair, ':', 2),
                                      ', ' ORDER BY p.n), '') || '}'
      INTO unknown, relabelled
      FROM unnest(string_to_array(btrim(v::text, '{}'), ', ')) WITH ORDINALITY AS p(pair, n)
      LEFT JOIN pg_temp.akb_bm25_relabel_map m
             ON m.current_id = split_part(p.pair, ':', 1)::bigint;
    IF unknown > 0 THEN
        RAISE EXCEPTION '% term id(s) in a vector are not in bm25_vocab: %', unknown, left(v::text, 200);
    END IF;
    RETURN relabelled::bm25_catalog.bm25vector;
END
$fn$
"""


async def _relabel(conn, facts: Survey, *, mapping_sql: str, mapping_args: tuple, next_id: int) -> int:
    """Apply a monotone map (current id -> target id) to every copy of the ids.

    Returns the new epoch.
    """
    await conn.execute(
        "CREATE TEMP TABLE akb_bm25_relabel_map ("
        " current_id bigint PRIMARY KEY, target_id bigint NOT NULL UNIQUE"
        ") ON COMMIT DROP"
    )
    await conn.execute(
        f"INSERT INTO pg_temp.akb_bm25_relabel_map (current_id, target_id) {mapping_sql}",
        *mapping_args,
    )
    # Planned per call inside the functions below: without statistics the map
    # is read in full for every id.
    await conn.execute("ANALYZE pg_temp.akb_bm25_relabel_map")
    await conn.execute(_RELABEL_ID)
    if facts.shape == "vchord":
        await conn.execute(_RELABEL_VECTOR)
        await conn.execute(
            f'ALTER TABLE "{facts.schema}".chunks ALTER COLUMN sparse_bm25 '
            "TYPE bm25_catalog.bm25vector USING pg_temp.akb_bm25_relabel_vector(sparse_bm25)"
        )
    else:
        await conn.execute(
            f'ALTER TABLE "{facts.schema}".posting ALTER COLUMN term_id '
            "TYPE bigint USING pg_temp.akb_bm25_relabel_id(term_id)"
        )
    # A rewrite, not two UPDATEs through negative ids: the unique index is
    # built again over the result, which also proves the map one-to-one.
    await conn.execute(
        "ALTER TABLE bm25_vocab ALTER COLUMN term_id TYPE bigint "
        "USING pg_temp.akb_bm25_relabel_id(term_id)"
    )
    await conn.execute(f"ALTER SEQUENCE bm25_term_id_seq RESTART WITH {int(next_id)}")
    # Changing a column's type discards the planner's statistics for it.
    await conn.execute(f"ANALYZE {facts.rewritten}")
    await conn.execute("ANALYZE bm25_vocab")
    return int(await conn.fetchval(
        "UPDATE bm25_vocab_epoch SET epoch = epoch + 1 WHERE id = 1 RETURNING epoch"
    ))


async def _verify(
    conn, facts: Survey, baseline: dict, *, top_k: int, epoch: int, next_id: int, dense: bool,
) -> list[str]:
    """What the committed state must satisfy, checked before the commit."""
    problems: list[str] = []
    vocab = await conn.fetchrow(
        "SELECT count(*) AS terms, min(term_id) AS lo, max(term_id) AS hi FROM bm25_vocab"
    )
    if int(vocab["terms"]) != facts.terms:
        problems.append(f"the vocabulary holds {vocab['terms']:,} terms, not {facts.terms:,}")
    unmapped = await conn.fetchval(
        """
        SELECT count(*) FROM pg_temp.akb_bm25_before b
          LEFT JOIN pg_temp.akb_bm25_relabel_map m ON m.current_id = b.term_id
          LEFT JOIN bm25_vocab v ON v.term = b.term
         WHERE v.term_id IS DISTINCT FROM m.target_id
        """
    )
    if unmapped:
        problems.append(f"{unmapped:,} terms do not hold the id the mapping gives them")
    if dense and facts.terms and (vocab["lo"] != 0 or vocab["hi"] != facts.terms - 1):
        problems.append(f"the ids run {vocab['lo']} .. {vocab['hi']}, not 0 .. {facts.terms - 1}")
    largest = int(vocab["hi"]) if vocab["hi"] is not None else -1

    if facts.shape == "vchord":
        meta = await _metapage(conn, facts.schema)
        if meta["doc_cnt"] != facts.vectors:
            problems.append(f"the rebuilt index counts {meta['doc_cnt']:,} documents, not {facts.vectors:,}")
        if meta["doc_term_cnt"] != facts.metapage["doc_term_cnt"]:
            problems.append(
                f"the rebuilt index sums {meta['doc_term_cnt']:,} term occurrences, "
                f"not {facts.metapage['doc_term_cnt']:,}"
            )
        if meta["term_id_cnt"] > largest + 1:
            problems.append(f"the rebuilt index spans {meta['term_id_cnt']:,} ids, past {largest + 1:,}")
    else:
        rows = await conn.fetchrow(
            f'SELECT count(*) AS n, min(term_id) AS lo, max(term_id) AS hi FROM "{facts.schema}".posting'
        )
        if int(rows["n"]) != facts.vectors:
            problems.append(f"posting holds {rows['n']:,} rows, not {facts.vectors:,}")
        if rows["n"] and (rows["lo"] < 0 or rows["hi"] > largest):
            problems.append(f"posting ids run {rows['lo']} .. {rows['hi']}, past {largest}")

    for query, before in baseline.items():
        after = await _ranking(conn, schema=facts.schema, shape=facts.shape, query=query, top_k=top_k)
        difference = _ranking_difference(before, after)
        if difference:
            problems.append(f"query {' '.join(query)!r}: {difference}")

    sequence = await conn.fetchrow("SELECT last_value, is_called FROM bm25_term_id_seq")
    continues_at = int(sequence["last_value"]) + (1 if sequence["is_called"] else 0)
    if continues_at != next_id:
        problems.append(f"the sequence continues at {continues_at:,}, not {next_id:,}")
    if epoch != facts.epoch + 1:
        problems.append(f"the epoch is {epoch}, not {facts.epoch + 1}")
    return problems


@dataclass
class Outcome:
    facts: Survey
    changed: bool
    epoch: int | None = None
    next_id: int | None = None
    metapage: dict[str, int] | None = None
    index_bytes: int | None = None
    timings: dict[str, float] = field(default_factory=dict)


async def _window(conn, *, schema, shape, queries, lock_timeout, maintenance_work_mem) -> Survey:
    """Enter the window, survey under its locks, and snapshot the vocabulary."""
    await _enter_window(
        conn, schema=schema, shape=shape, lock_timeout=lock_timeout,
        maintenance_work_mem=maintenance_work_mem,
    )
    facts = await survey(conn, schema=schema, shape=shape, queries=queries)
    if facts.refusals:
        raise Refused(facts.refusals)
    await conn.execute(
        "CREATE TEMP TABLE akb_bm25_before ON COMMIT DROP AS SELECT term, term_id FROM bm25_vocab"
    )
    return facts


async def apply(
    conn, *, schema: str, shape: str, queries: int = _QUERIES, top_k: int = _TOP_K,
    lock_timeout: float = _LOCK_TIMEOUT_SECS, maintenance_work_mem: str = _MAINTENANCE_WORK_MEM,
) -> Outcome:
    """Renumber densely, verify, and commit; any failure rolls it all back."""
    timings: dict[str, float] = {}
    started = time.monotonic()
    async with conn.transaction():
        facts = await _window(
            conn, schema=schema, shape=shape, queries=queries,
            lock_timeout=lock_timeout, maintenance_work_mem=maintenance_work_mem,
        )
        timings["locks and survey"] = time.monotonic() - started
        if facts.dense:
            return Outcome(facts=facts, changed=False, timings=timings)

        mark = time.monotonic()
        baseline = await _rankings(conn, facts, top_k)
        timings["baseline"] = time.monotonic() - mark

        mark = time.monotonic()
        await conn.execute(f"DROP TABLE IF EXISTS {_MAPPING_RUN}, {_MAPPING}")
        await conn.execute(
            f"""
            CREATE TABLE {_MAPPING} (
                term    text   PRIMARY KEY,
                old_id  bigint NOT NULL UNIQUE,
                new_id  bigint NOT NULL UNIQUE
            )
            """
        )
        await conn.execute(
            f"INSERT INTO {_MAPPING} (term, old_id, new_id) "
            "SELECT term, term_id, row_number() OVER (ORDER BY term_id) - 1 FROM bm25_vocab"
        )
        epoch = await _relabel(
            conn, facts,
            mapping_sql=f"SELECT old_id, new_id FROM {_MAPPING}", mapping_args=(),
            next_id=facts.terms,
        )
        await conn.execute(
            f"""
            CREATE TABLE {_MAPPING_RUN} (
                id                   smallint    PRIMARY KEY DEFAULT 1,
                shape                text        NOT NULL,
                schema_name          text        NOT NULL,
                epoch_before         bigint      NOT NULL,
                epoch_after          bigint      NOT NULL,
                terms                bigint      NOT NULL,
                max_id_before        bigint      NOT NULL,
                sequence_next_before bigint      NOT NULL,
                applied_at           timestamptz NOT NULL DEFAULT now(),
                CHECK (id = 1)
            )
            """
        )
        await conn.execute(
            f"""
            INSERT INTO {_MAPPING_RUN} (shape, schema_name, epoch_before, epoch_after, terms,
                                        max_id_before, sequence_next_before)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            shape, schema, facts.epoch, epoch, facts.terms, facts.max_id, facts.sequence_next,
        )
        timings["rewrite"] = time.monotonic() - mark

        mark = time.monotonic()
        problems = await _verify(
            conn, facts, baseline, top_k=top_k, epoch=epoch, next_id=facts.terms, dense=True,
        )
        timings["verify"] = time.monotonic() - mark
        if problems:
            raise VerificationFailed(problems)
        outcome = await _outcome(conn, facts, epoch=epoch, next_id=facts.terms, timings=timings)
    return outcome


async def revert(
    conn, *, schema: str, shape: str, queries: int = _QUERIES, top_k: int = _TOP_K,
    lock_timeout: float = _LOCK_TIMEOUT_SECS, maintenance_work_mem: str = _MAINTENANCE_WORK_MEM,
) -> Outcome:
    """Put back the numbering the recorded renumbering replaced.

    Terms registered since have no old id. They take the ids after the old
    numbering's largest, in their current order, so the map stays monotone.
    """
    timings: dict[str, float] = {}
    started = time.monotonic()
    async with conn.transaction():
        facts = await _window(
            conn, schema=schema, shape=shape, queries=queries,
            lock_timeout=lock_timeout, maintenance_work_mem=maintenance_work_mem,
        )
        run = facts.mapping
        if run is None:
            raise Refused([f"{_MAPPING_RUN} does not exist: there is no recorded renumbering to revert."])
        reasons = []
        if (run["shape"], run["schema_name"]) != (shape, schema):
            reasons.append(
                f"the recorded renumbering was of {run['shape']} in {run['schema_name']!r}, "
                f"and this database now serves {shape} in {schema!r}."
            )
        if run["epoch_after"] != facts.epoch:
            reasons.append(
                f"the vocabulary is at epoch {facts.epoch}, not the {run['epoch_after']} the "
                "recorded renumbering left: it has been renumbered since."
            )
        moved = await conn.fetchval(
            f"""
            SELECT count(*) FROM {_MAPPING} r LEFT JOIN bm25_vocab v ON v.term = r.term
             WHERE v.term_id IS DISTINCT FROM r.new_id
            """
        )
        if moved:
            reasons.append(f"{moved:,} recorded terms no longer hold the id the renumbering gave them.")
        if reasons:
            raise Refused(reasons)
        timings["locks and survey"] = time.monotonic() - started

        mark = time.monotonic()
        baseline = await _rankings(conn, facts, top_k)
        timings["baseline"] = time.monotonic() - mark

        mark = time.monotonic()
        largest_old = int(run["max_id_before"])
        next_id = max(
            int(run["sequence_next_before"]),
            largest_old + 1 + int(await conn.fetchval(
                f"SELECT count(*) FROM bm25_vocab v "
                f"WHERE NOT EXISTS (SELECT 1 FROM {_MAPPING} r WHERE r.term = v.term)"
            )),
        )
        epoch = await _relabel(
            conn, facts,
            mapping_sql=f"""
                SELECT new_id, old_id FROM {_MAPPING}
                UNION ALL
                SELECT v.term_id, $1 + row_number() OVER (ORDER BY v.term_id)
                  FROM bm25_vocab v
                 WHERE NOT EXISTS (SELECT 1 FROM {_MAPPING} r WHERE r.term = v.term)
            """,
            mapping_args=(largest_old,),
            next_id=next_id,
        )
        await conn.execute(f"DROP TABLE {_MAPPING_RUN}, {_MAPPING}")
        timings["rewrite"] = time.monotonic() - mark

        mark = time.monotonic()
        problems = await _verify(
            conn, facts, baseline, top_k=top_k, epoch=epoch, next_id=next_id, dense=False,
        )
        timings["verify"] = time.monotonic() - mark
        if problems:
            raise VerificationFailed(problems)
        outcome = await _outcome(conn, facts, epoch=epoch, next_id=next_id, timings=timings)
    return outcome


async def _outcome(conn, facts: Survey, *, epoch: int, next_id: int, timings) -> Outcome:
    outcome = Outcome(facts=facts, changed=True, epoch=epoch, next_id=next_id, timings=timings)
    if facts.shape == "vchord":
        outcome.metapage = await _metapage(conn, facts.schema)
        outcome.index_bytes = int(await conn.fetchval(
            "SELECT pg_relation_size(to_regclass($1))", f'"{facts.schema}".{_INDEX}',
        ))
    return outcome


# ── Report ────────────────────────────────────────────────────────


def _size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    raise AssertionError("unreachable")


def _describe(facts: Survey) -> list[str]:
    lines = [f"  shape         {facts.shape} (schema {facts.schema!r})"]
    if facts.epoch is None:
        return lines
    if facts.terms:
        lines.append(
            f"  vocabulary    {facts.terms:,} terms, ids {facts.min_id:,} .. {facts.max_id:,}: "
            f"{(facts.max_id + 1) / facts.terms:,.1f} ids per term"
        )
        lines.append(
            f"                the largest id is {facts.max_id / _ALIASING_ID:.1%} of 2^30, "
            "where the index starts to alias ids"
        )
    else:
        lines.append("  vocabulary    empty")
    lines.append(f"  sequence      next id {facts.sequence_next:,}; vocabulary epoch {facts.epoch}")
    if facts.vectors is not None:
        what = "rows hold a vector" if facts.shape == "vchord" else "posting rows"
        lines.append(f"  vectors       {facts.vectors:,} {what}")
    if facts.metapage is not None:
        meta = facts.metapage
        lines.append(
            f"  index         {_INDEX}: {_size(facts.index_bytes)}, {meta['doc_cnt']:,} documents, "
            f"term_id_cnt {meta['term_id_cnt']:,} (dense: {facts.terms:,})"
        )
    if facts.rebuilt:
        rebuilt = ", ".join(f"{name} {_size(size)}" for name, size in facts.rebuilt)
        lines.append(f"  rebuilt       {rebuilt}")
    if facts.queries:
        shown = "; ".join(" ".join(query) for query in facts.queries[:6])
        more = f" (and {len(facts.queries) - 6} more)" if len(facts.queries) > 6 else ""
        lines.append(f"  baseline      {len(facts.queries)} queries: {shown}{more}")
    if facts.mapping:
        lines.append(
            f"  recorded      a renumbering at {facts.mapping['applied_at']:%Y-%m-%d %H:%M} UTC, "
            f"epoch {facts.mapping['epoch_before']} -> {facts.mapping['epoch_after']} "
            f"({_MAPPING}); --revert puts it back"
        )
    return lines


def render_survey(facts: Survey) -> str:
    lines = ["BM25 term-id renumbering: dry run, nothing is changed"]
    lines += _describe(facts)
    if facts.refusals:
        lines.append("  --apply would refuse:")
        lines += [f"    - {reason}" for reason in facts.refusals]
    elif facts.dense:
        lines.append("  the ids are already dense; --apply has nothing to do")
    else:
        rewritten = "the vectors" if facts.shape == "vchord" else "posting"
        lines.append(
            f"  --apply rewrites {rewritten} and the vocabulary in one transaction, rebuilding "
            f"the indexes above, with {facts.rewritten} and bm25_vocab locked until it commits."
        )
        lines.append(
            "  every backend process must already store term ids under the vocabulary "
            "epoch (the version that ships this command)"
        )
    return "\n".join(lines)


def render_outcome(outcome: Outcome, action: str) -> str:
    facts = outcome.facts
    if not outcome.changed:
        return "BM25 term-id renumbering: the ids are already dense; nothing to do"
    lines = [f"BM25 term-id renumbering: {action}, verified and committed"]
    lines.append(
        f"  {facts.terms:,} terms, ids {facts.min_id:,} .. {facts.max_id:,} renumbered; "
        f"the sequence continues at {outcome.next_id:,}; vocabulary epoch "
        f"{facts.epoch} -> {outcome.epoch}"
    )
    if outcome.metapage and facts.metapage:
        lines.append(
            f"  {_INDEX}: term_id_cnt {facts.metapage['term_id_cnt']:,} -> "
            f"{outcome.metapage['term_id_cnt']:,}, {_size(facts.index_bytes)} -> "
            f"{_size(outcome.index_bytes)}"
        )
    lines.append(f"  {len(facts.queries)} baseline queries rank the same (tolerance {_TOLERANCE:g})")
    lines.append("  " + " · ".join(f"{name} {secs:.1f}s" for name, secs in outcome.timings.items()))
    if action == "renumbered":
        lines.append(f"  the mapping is kept in {_MAPPING} for --revert")
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────


def configuration_refusal() -> str | None:
    """Why this process's configuration cannot be renumbered in place, or None."""
    driver = settings.vector_store_driver
    if driver != "pgvector":
        return (
            f"vector_store_driver is {driver!r}: its sparse vectors live outside "
            "PostgreSQL, where no transaction can rewrite them together with the "
            "vocabulary. Renumbering there means indexing every chunk again; this "
            "command refuses."
        )
    if settings.vector_store_dsn:
        return (
            "vector_store_dsn puts the vector index in a separate database. The "
            "vocabulary and the vectors must change in one transaction, so this "
            "command needs both in the main database."
        )
    return None


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _memory(value: str) -> str:
    if not _MEMORY_RE.match(value):
        raise argparse.ArgumentTypeError("a PostgreSQL memory size, e.g. 512MB or 2GB")
    return value


def _parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Renumber BM25 term ids densely (akb#687).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true",
                      help="renumber, verify, and commit; roll back on any failure")
    mode.add_argument("--revert", action="store_true",
                      help="put back the numbering the recorded renumbering replaced")
    ap.add_argument("--queries", type=_positive_int, default=_QUERIES,
                    help=f"baseline queries sampled from stored documents (default {_QUERIES})")
    ap.add_argument("--top-k", type=_positive_int, default=_TOP_K,
                    help=f"results compared per baseline query (default {_TOP_K})")
    ap.add_argument("--lock-timeout", type=_positive_float, default=_LOCK_TIMEOUT_SECS,
                    help=f"seconds to wait for each lock before refusing (default {_LOCK_TIMEOUT_SECS:g})")
    ap.add_argument("--maintenance-work-mem", type=_memory, default=_MAINTENANCE_WORK_MEM,
                    help="maintenance_work_mem for the index rebuilds; a dense HNSW index "
                         f"builds far faster when its graph fits (default {_MAINTENANCE_WORK_MEM})")
    return ap.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    try:
        return await _run(args)
    finally:
        await close_pool()


async def _run(args: argparse.Namespace) -> int:
    refusal = configuration_refusal()
    if refusal:
        print(f"refused: {refusal}")
        return EXIT_REFUSED
    try:
        await decide_sparse_shape_for_settings()
        shape = settings.effective_sparse_shape
    except (SparseShapeUndecidable, RuntimeError) as error:
        print(f"refused: {error}")
        return EXIT_REFUSED
    schema = settings.vector_store_schema
    if not _SCHEMA_NAME_RE.match(schema):
        print(f"refused: vector_store_schema {schema!r} is not a plain SQL identifier")
        return EXIT_REFUSED

    # A connection of its own: the application pool's 30-second statement
    # timeout would cancel the rewrite partway through.
    conn = await asyncpg.connect(
        settings.asyncpg_dsn,
        command_timeout=None,
        server_settings={
            "application_name": "akb-bm25-compact-term-ids",
            "statement_timeout": "0",
            "idle_in_transaction_session_timeout": "0",
        },
    )
    try:
        if not (args.apply or args.revert):
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                facts = await survey(conn, schema=schema, shape=shape, queries=args.queries)
            print(render_survey(facts))
            return EXIT_REFUSED if facts.refusals else EXIT_OK

        operation = apply if args.apply else revert
        outcome: Outcome | None = None
        started = False

        async def guarded() -> None:
            nonlocal outcome, started
            started = True
            outcome = await operation(
                conn, schema=schema, shape=shape, queries=args.queries, top_k=args.top_k,
                lock_timeout=args.lock_timeout, maintenance_work_mem=args.maintenance_work_mem,
            )

        try:
            await run_bulk_exclusive(guarded)
        except Refused as refused:
            print("refused, nothing was changed:")
            print("\n".join(f"  - {reason}" for reason in refused.reasons))
            return EXIT_REFUSED
        except VerificationFailed as failed:
            print("verification failed; the transaction was rolled back and nothing was changed:")
            print("\n".join(f"  - {problem}" for problem in failed.problems))
            return EXIT_FAILED
        except RuntimeError as error:
            if not started:  # the bulk guard is held by the recompute or a backfill
                print(f"refused, nothing was changed: {error}")
                return EXIT_REFUSED
            print(f"stopped, the transaction was rolled back: {error}")
            return EXIT_FAILED
        except asyncpg.PostgresError as error:
            print(f"failed, the transaction was rolled back: {error}")
            return EXIT_FAILED
        assert outcome is not None
        print(render_outcome(outcome, "renumbered" if args.apply else "reverted"))
        return EXIT_OK
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
