"""PostgreSQL proof for the search degradation counters (akb#612).

The unit tests beside this one hold the arithmetic against a fake connection,
which is the right place for it — but they cannot tell whether the statements
are valid SQL, whether migration 110 actually created the tables the service
writes to, or whether two pods folding the same day ADD instead of overwriting.
That last one is the whole durability claim: an `ON CONFLICT` that replaced
rather than accumulated would look correct in every single-writer test and
silently discard one deployment's counts in production.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from datetime import date

import asyncpg
import pytest

from app.db import postgres as postgres_module
from app.services import search_degradation_stats as sds
from tests.helpers.pg_database import TestDatabase, temporary_database

pytestmark = [pytest.mark.asyncio, pytest.mark.live_services]

_DSN = os.environ.get("AKB_TEST_DSN", "postgresql://akb:akb@localhost:15432/akb")


@contextlib.asynccontextmanager
async def _fresh_database(application_schema_template: TestDatabase) -> AsyncIterator[asyncpg.Pool]:
    async with (
        temporary_database(_DSN, "akb_search_degradation_", template=application_schema_template) as database,
        asyncpg.create_pool(database.dsn, min_size=1, max_size=4) as pool,
    ):
        previous_pool = postgres_module._pool
        postgres_module._pool = pool
        try:
            yield pool
        finally:
            postgres_module._pool = previous_pool


def _seed(day: date, *, observed: int, degraded: int, with_results: int, causes: dict) -> None:
    delta = sds._Delta(
        observed=observed, degraded=degraded, degraded_with_results=with_results,
    )
    delta.by_cause.update(causes)
    sds._pending[day] = delta


async def test_migration_110_creates_the_tables_the_service_writes_to(application_schema_template):
    """A service whose statements name a table no migration created fails only
    on the first flush of a real deployment."""
    async with _fresh_database(application_schema_template) as pool:
        async with pool.acquire() as conn:
            present = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                " WHERE table_schema = 'public' AND table_name LIKE 'search_degradation%'"
                " ORDER BY table_name"
            )
    assert [r["table_name"] for r in present] == [
        "search_degradation_cause_daily", "search_degradation_daily",
    ]


async def test_a_second_flush_for_the_same_day_adds_rather_than_replaces(application_schema_template):
    """The claim durability rests on. Every pod folds a delta for the same day
    under the same primary key, so `ON CONFLICT` must accumulate; a plain
    overwrite passes every single-writer test and loses a whole deployment's
    counts in production."""
    sds.reset()
    day = date(2026, 9, 18)
    try:
        async with _fresh_database(application_schema_template):
            _seed(day, observed=10, degraded=3, with_results=2,
                  causes={"hydration_miss": 3})
            assert await sds.flush_once() == 10

            _seed(day, observed=4, degraded=1, with_results=0,
                  causes={"hydration_miss": 1, "sparse_encoder_degraded": 1})
            assert await sds.flush_once() == 4

            from app.db.postgres import get_pool

            pool = await get_pool()
            async with pool.acquire() as conn:
                totals = await conn.fetchrow(
                    "SELECT * FROM search_degradation_daily WHERE day = $1", day,
                )
                causes = await conn.fetch(
                    "SELECT cause, responses FROM search_degradation_cause_daily "
                    " WHERE day = $1 ORDER BY cause", day,
                )
        assert (totals["observed"], totals["degraded"], totals["degraded_with_results"]) == (14, 4, 2)
        assert [(r["cause"], r["responses"]) for r in causes] == [
            ("hydration_miss", 4), ("sparse_encoder_degraded", 1),
        ]
    finally:
        sds.reset()


async def test_the_snapshot_reads_back_what_the_flush_wrote(application_schema_template):
    """End to end through real SQL: fold, then read the section `/health`
    serves. `pending_flush` returns to zero because the delta is now durable —
    that is the number an operator uses to tell what a kill would cost."""
    sds.reset()
    try:
        async with _fresh_database(application_schema_template):
            _seed(sds._today(), observed=7, degraded=2, with_results=1,
                  causes={"hydration_miss": 2})
            await sds.flush_once()

            snap = await sds.snapshot()
        assert snap["observed"] == 7
        assert snap["degraded"] == 2
        assert snap["degraded_with_results"] == 1
        assert snap["by_cause"] == {"hydration_miss": 2}
        assert snap["pending_flush"] == 0
        assert snap["consecutive_flush_failures"] == 0
    finally:
        sds.reset()


async def test_days_stay_separate_rows(application_schema_template):
    """Comparing two days is the point of a durable aggregate, so a flush that
    spans midnight must not merge them."""
    sds.reset()
    try:
        async with _fresh_database(application_schema_template):
            _seed(date(2026, 9, 17), observed=5, degraded=0, with_results=0, causes={})
            _seed(date(2026, 9, 18), observed=9, degraded=1, with_results=1,
                  causes={"stale_arm": 1})
            assert await sds.flush_once() == 14

            from app.db.postgres import get_pool

            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT day, observed, degraded FROM search_degradation_daily "
                    " ORDER BY day",
                )
        assert [(r["day"], r["observed"], r["degraded"]) for r in rows] == [
            (date(2026, 9, 17), 5, 0), (date(2026, 9, 18), 9, 1),
        ]
    finally:
        sds.reset()
