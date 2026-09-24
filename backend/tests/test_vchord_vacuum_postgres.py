"""VACUUM of the BM25 index: no search waits for it, and its counts stay exact.

VACUUM reaches a vchord_bm25 index in two steps. The bulk delete marks the
documents whose rows it removes, and the cleanup recounts how many live
documents hold each term. vchord_bm25 0.3.0 held the index's metapage through
both, and a search reads the metapage for its whole scan (akb#687):

- the bulk delete held it for a pass over every document id the index has
  assigned, and wrote the counts only at the end, so a backend killed
  mid-pass left them too high for good;
- the cleanup visited every term id below the largest one indexed, one page
  write per id, and could not be cancelled.

The image deploy/postgres builds carries patches 0003 and 0004, which take the
metapage one page at a time and log each page's marks with the counts they
take off. Without them the first three tests fail.

A VACUUM stopped between the two steps, or during the recount, leaves some
statistics counting deleted documents. 0004 also records that a recount is
owed until one reaches its last page, so the next VACUUM of any kind finishes
it; upstream recounted only when that VACUUM removed documents itself. Until
then a statistic can exceed the document count, and 0005 keeps the IDF
positive, so a search for that term alone still finds its documents. Without
those two the fourth test fails.

Requires the extension-capable server (AKB_VCHORD_TEST_DSN, the image
deploy/postgres builds). Each test creates and drops its own database.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import re
import time
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest

pytestmark = pytest.mark.asyncio

_DSN = os.environ.get("AKB_VCHORD_TEST_DSN", "")
_SEARCH = """
    SELECT id FROM vacuum_probe
    ORDER BY v <&> bm25_catalog.to_bm25query('vacuum_probe_bm25'::regclass, $1::bm25_catalog.bm25vector)
    LIMIT 10
"""


@contextlib.asynccontextmanager
async def _database() -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    """A database with one table and its BM25 index, the only index VACUUM visits."""
    if not _DSN:
        pytest.skip("AKB_VCHORD_TEST_DSN is required for VectorChord coverage")
    base, _ = _DSN.rsplit("/", 1)
    name = f"akb_vchord_vacuum_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DSN)
    await admin.execute(f'CREATE DATABASE "{name}"')
    dsn = f"{base}/{name}"
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vchord_bm25")
        await conn.execute("CREATE TABLE vacuum_probe (id int, v bm25_catalog.bm25vector)")
        yield conn, dsn
    finally:
        await conn.close()
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _index(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "CREATE INDEX vacuum_probe_bm25 ON vacuum_probe USING bm25 (v bm25_catalog.bm25_ops)")


async def _fill_million(conn: asyncpg.Connection) -> None:
    """A million documents of length 10, indexed by a build, 800,000 of them deleted."""
    await conn.execute(
        "INSERT INTO vacuum_probe SELECT i, ('{1:5,' || (i % 5000 + 2) || ':5}')::bm25_catalog.bm25vector"
        " FROM generate_series(0, 999999) i")
    await _index(conn)
    await conn.execute("DELETE FROM vacuum_probe WHERE id < 800000")


async def _inspect(conn: asyncpg.Connection, blkno: int) -> str:
    return await conn.fetchval("SELECT bm25_catalog.bm25_page_inspect('vacuum_probe_bm25'::regclass, $1)", blkno)


async def _metapage(conn: asyncpg.Connection) -> dict[str, int]:
    text = await _inspect(conn, 0)
    keys = ("doc_cnt", "doc_term_cnt", "current_doc_id", "term_id_cnt", "term_stat_blkno", "delete_bitmap_blkno")
    return {key: int(re.search(rf"\b{key}: (\d+)", text).group(1)) for key in keys}


async def _data_pages(conn: asyncpg.Connection, first_blkno: int, limit: int | None = None) -> list[str]:
    """The data pages of one of the index's arrays, through its first inode page."""
    inode = await _inspect(conn, first_blkno)
    assert inode.startswith("Virtual Inode Page"), inode[:80]
    blknos = [int(b) for b in re.search(r"\[(.*)\]", inode, re.S).group(1).split(",")]
    return [await _inspect(conn, blkno) for blkno in blknos[:limit]]


async def _term_statistics(conn: asyncpg.Connection) -> list[int]:
    """Every term's stored statistic, indexed by term id."""
    meta = await _metapage(conn)
    stored: list[int] = []
    for page in await _data_pages(conn, meta["term_stat_blkno"]):
        body = re.search(r"\[(.*)\]", page, re.S).group(1).strip()
        stored.extend(int(n) for n in body.split(",") if body)
    return stored[: meta["term_id_cnt"]]


async def _deleted(conn: asyncpg.Connection) -> int:
    """How many documents the delete bitmap marks."""
    meta = await _metapage(conn)
    pages = await _data_pages(conn, meta["delete_bitmap_blkno"])
    data = "".join(re.search(r"\[(.*)\]", page, re.S).group(1) for page in pages)
    return sum(int(byte, 16).bit_count() for byte in re.findall(r"[0-9A-F]{2}", data))


async def _search(conn: asyncpg.Connection, query: str) -> float:
    started = time.monotonic()
    async with conn.transaction():
        await conn.execute('SET LOCAL search_path TO "$user", public, bm25_catalog')
        await conn.execute("SET LOCAL bm25_catalog.bm25_limit = 10")
        await conn.fetch(_SEARCH, query)
    return time.monotonic() - started


async def _phase(conn: asyncpg.Connection, pid: int) -> str | None:
    return await conn.fetchval("SELECT phase FROM pg_stat_progress_vacuum WHERE pid = $1", pid)


async def _vacuum_while_searching(dsn: str, *, cost_delay_ms: int) -> tuple[float, dict[str, float]]:
    """Run VACUUM while one session searches back to back.

    Returns the VACUUM's duration and, per VACUUM phase, the longest search that
    started in it. A search blocked behind the metapage takes as long as the
    step that holds it.
    """
    vac, probe, watch = [await asyncpg.connect(dsn) for _ in range(3)]
    try:
        pid = await vac.fetchval("SELECT pg_backend_pid()")
        await vac.execute(f"SET vacuum_cost_delay = '{cost_delay_ms}ms'")
        await _search(probe, "{1:1}")  # loads the library in the probe session
        started = time.monotonic()
        task = asyncio.create_task(vac.execute("VACUUM vacuum_probe"))
        longest: dict[str, float] = {}
        while not task.done():
            phase = await _phase(watch, pid)
            took = await _search(probe, "{3:1}")  # 200 documents, quick by itself
            if phase:
                longest[phase] = max(longest.get(phase, 0.0), took)
        await task
        return time.monotonic() - started, longest
    finally:
        for conn in (vac, probe, watch):
            await conn.close()


async def test_a_cancelled_bulk_delete_stops_at_once_and_the_next_vacuum_counts_exactly():
    """A cancel lands inside the bulk delete, and the counts follow the marks.

    0.3.0 held the metapage for the whole pass, which also held off the cancel
    until the pass was over. The pass now takes the metapage one delete bitmap
    page at a time and logs each page's marks with the counts they take off,
    so a cancel stops it between pages and leaves the counts matching the
    marks. The next VACUUM then marks the rest and takes each document off
    once. Autovacuum's cost delay keeps the pass long enough to cancel.
    """
    async with _database() as (conn, dsn):
        await _fill_million(conn)
        vac = await asyncpg.connect(dsn)
        try:
            pid = await vac.fetchval("SELECT pg_backend_pid()")
            await vac.execute("SET vacuum_cost_delay = '2ms'")
            task = asyncio.create_task(vac.execute("VACUUM vacuum_probe"))
            while await _phase(conn, pid) != "vacuuming indexes":
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.1)
            cancelled = time.monotonic()
            await conn.execute("SELECT pg_cancel_backend($1)", pid)
            with pytest.raises(asyncpg.QueryCanceledError):
                await task
            stopped = time.monotonic() - cancelled
        finally:
            await vac.close()
        after_cancel = await _metapage(conn)
        marked = await _deleted(conn)
        await conn.execute("VACUUM vacuum_probe")
        after = await _metapage(conn)

    assert stopped < 1.0, f"the cancel took {stopped:.2f} s to stop the bulk delete"
    assert after_cancel["doc_cnt"] == after_cancel["current_doc_id"] - marked, (after_cancel, marked)
    assert after["doc_cnt"] == 200_000
    assert after["doc_term_cnt"] == 2_000_000  # length 10, stored as is


async def test_no_search_waits_for_the_bulk_delete():
    """A search during the bulk delete takes as long as a search at any other time.

    0.3.0 held the metapage for the whole pass, 800,000 deletions over a
    million documents here, and a search that started meanwhile waited until
    it ended: 3 s without cost delay, longer with it.
    """
    async with _database() as (conn, dsn):
        await _fill_million(conn)
        took, longest = await _vacuum_while_searching(dsn, cost_delay_ms=2)
        after = await _metapage(conn)

    assert max(longest.values()) < 0.5, f"a search waited for VACUUM: {longest}, VACUUM {took:.1f} s"
    assert "vacuuming indexes" in longest, longest  # and searches did run during the pass
    assert after["doc_cnt"] == 200_000


async def test_the_cleanup_over_a_sparse_term_id_space_is_quick_and_exact():
    """The cleanup's work follows the terms, and every term's statistic matches the rows.

    One document holds term 20,000,000, so term ids run to 20,000,001 while
    only a few hundred exist. 0.3.0 recounted and rewrote every one of those
    ids with the metapage held: about 3.5 us and 52 bytes of WAL per id, over
    a minute here, with every search waiting. The cleanup now writes only the
    statistic pages whose counts changed and holds no lock between pages.
    Rows reach the index by the build and by insert, and deletions fall in
    both segments.
    """
    rnd = random.Random(687)
    rows = {i: sorted({int(400 * rnd.random() ** 2) + 1 for _ in range(rnd.randint(1, 8))}) for i in range(24_000)}
    rows[0] = [1, 20_000_000]

    def vector(terms: list[int]) -> str:
        return "{" + ",".join(f"{t}:1" for t in terms) + "}"

    async with _database() as (conn, dsn):
        insert = "INSERT INTO vacuum_probe VALUES ($1, $2::bm25_catalog.bm25vector)"
        await conn.executemany(insert, [(i, vector(t)) for i, t in rows.items() if i < 20_000])
        await _index(conn)
        await conn.executemany(insert, [(i, vector(t)) for i, t in rows.items() if i >= 20_000])  # growing
        await conn.execute("DELETE FROM vacuum_probe WHERE id % 4 = 1")
        took, longest = await _vacuum_while_searching(dsn, cost_delay_ms=0)
        meta = await _metapage(conn)
        first_page = (await _data_pages(conn, meta["term_stat_blkno"], limit=1))[0]

    live = [terms for i, terms in rows.items() if i % 4 != 1]
    truth: dict[int, int] = {}
    for terms in live:
        for term in terms:
            truth[term] = truth.get(term, 0) + 1
    stored = [int(n) for n in re.search(r"\[(.*)\]", first_page, re.S).group(1).split(",")]
    wrong = {term: (count, truth.get(term, 0)) for term, count in enumerate(stored) if count != truth.get(term, 0)}

    assert meta["term_id_cnt"] == 20_000_001
    assert took < 10, f"VACUUM took {took:.1f} s over {meta['term_id_cnt']:,} term ids"
    assert max(longest.values()) < 1.0, f"a search waited for VACUUM: {longest}"
    assert meta["doc_cnt"] == len(live)
    assert wrong == {}, f"{len(wrong)} term statistics differ from the rows: {dict(list(wrong.items())[:10])}"


async def _vacuum_cancelled_in(dsn: str, phase: str) -> str:
    """Run VACUUM slowed by a cost delay, and cancel it once it is in `phase`.

    Returns the phase the cancel landed in, or "finished" when VACUUM ended
    before the cancel reached it.
    """
    vac, watch = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
    try:
        pid = await vac.fetchval("SELECT pg_backend_pid()")
        await vac.execute("SET vacuum_cost_delay = '2ms'")
        await vac.execute("SET vacuum_cost_limit = 10")
        task = asyncio.create_task(vac.execute("VACUUM vacuum_probe"))
        while not task.done() and await _phase(watch, pid) != phase:
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)
        landed = await _phase(watch, pid)
        await watch.execute("SELECT pg_cancel_backend($1)", pid)
        try:
            await task
        except asyncpg.QueryCanceledError:
            return landed or "finished"
        return "finished"
    finally:
        await vac.close()
        await watch.close()


async def _rows_found(conn: asyncpg.Connection, query: str) -> int:
    async with conn.transaction():
        await conn.execute('SET LOCAL search_path TO "$user", public, bm25_catalog')
        await conn.execute("SET LOCAL bm25_catalog.bm25_limit = 10")
        return len(await conn.fetch(_SEARCH, query))


async def test_a_vacuum_that_removes_nothing_finishes_a_recount_an_interrupted_one_owed():
    """Statistics left counting deleted documents are recounted by the next VACUUM, whatever it removes.

    Term 1 is in every document, terms 2 to 5,001 in one document of every
    5,000, and each document has a rare term of its own, so the recount has
    hundreds of statistic pages to write. 40,000 of the 100,000 documents are
    deleted, and three VACUUMs follow:

    - the first is cancelled after its bulk delete, while it vacuums the heap.
      Every statistic still counts the deleted documents, and term 1's is
      100,000 against 60,000 documents. Without 0005 its IDF is negative and a
      search for term 1 alone finds nothing;
    - the second finds every dead document already marked, so it removes
      nothing from the index. Upstream therefore skipped the recount; with
      0004 it runs, and is cancelled part way;
    - the third has nothing left to remove at all. With 0004 it finishes the
      recount, and every statistic matches the rows.
    """
    rare = 600_000
    async with _database() as (conn, dsn):
        await conn.execute("ALTER TABLE vacuum_probe SET (autovacuum_enabled = off)")
        await conn.execute(
            "INSERT INTO vacuum_probe SELECT i, ('{1:1,' || (i % 5000 + 2) || ':1,'"
            f" || (5002 + (i::bigint * 7919) % {rare}) || ':1}}')::bm25_catalog.bm25vector"
            " FROM generate_series(0, 99999) i")
        await _index(conn)
        await conn.execute("DELETE FROM vacuum_probe WHERE id % 10 < 4")

        first = await _vacuum_cancelled_in(dsn, "vacuuming heap")
        stale = await _term_statistics(conn)
        found = await _rows_found(conn, "{1:1}")
        second = await _vacuum_cancelled_in(dsn, "cleaning up indexes")
        stale_after_second = await _term_statistics(conn)
        await conn.execute("VACUUM vacuum_probe")
        stored = await _term_statistics(conn)
        meta = await _metapage(conn)

    truth: dict[int, int] = {}
    for i in range(100_000):
        if i % 10 >= 4:
            for term in (1, i % 5000 + 2, 5002 + (i * 7919) % rare):
                truth[term] = truth.get(term, 0) + 1

    def wrong(statistics: list[int]) -> int:
        return sum(count != truth.get(term, 0) for term, count in enumerate(statistics))

    assert first == "vacuuming heap", first
    assert stale[1] == 100_000 and meta["doc_cnt"] == 60_000, (stale[1], meta)
    observed = {
        "rows found for term 1 alone": found,
        "second VACUUM cancelled in": second,
        "recount stopped part way": 0 < wrong(stale_after_second) < wrong(stale),
        "statistics wrong after the third": wrong(stored),
    }
    assert observed == {
        "rows found for term 1 alone": 10,
        "second VACUUM cancelled in": "cleaning up indexes",
        "recount stopped part way": True,
        "statistics wrong after the third": 0,
    }, (observed, {"wrong after the first": wrong(stale), "after the second": wrong(stale_after_second)})
