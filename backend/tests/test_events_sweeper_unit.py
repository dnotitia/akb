"""Unit checks of the events sweeper's setting and of how its steps keep their place."""

from __future__ import annotations

import pytest


def test_event_retention_defaults_to_seven_days_and_refuses_less_than_one() -> None:
    from pydantic import ValidationError

    from app.config import Settings

    # Seven days is the period the publisher's sweep kept a published event.
    assert Settings(document_revision_backend="bare_git").event_retention_days == 7
    with pytest.raises(ValidationError):
        Settings(document_revision_backend="bare_git", event_retention_days=0)


class _Conn:
    """Answers the batch read with ``rows``, the delete with ``deleted``, the marks' deletes with ``orphans``."""

    def __init__(
        self,
        rows: list[tuple[int, bool]],
        deleted: int,
        *,
        orphans: list[int] | None = None,
        fail_delete: bool = False,
    ) -> None:
        self.rows = [{"id": event_id, "old": old} for event_id, old in rows]
        self.deleted = deleted
        self.orphans = list(orphans or [0])
        self.fail_delete = fail_delete
        self.deletes: list[tuple] = []
        self.statements: list[str] = []
        self.reads: list[tuple] = []
        self.mark_deletes = 0

    async def fetch(self, sql: str, *args):
        self.reads.append(args)
        return self.rows

    async def fetchval(self, sql: str, *args):
        if "event_retention_watermarks" in sql:
            self.mark_deletes += 1
            return self.orphans.pop(0) if len(self.orphans) > 1 else self.orphans[0]
        if self.fail_delete:
            raise ConnectionResetError("lost the connection")
        self.statements.append(sql)
        self.deletes.append(args)
        return self.deleted


class _Pool:
    def __init__(self, conn: _Conn, *, fail_release: bool = False) -> None:
        self.conn = conn
        self.fail_release = fail_release

    def acquire(self):
        conn = self.conn
        fail_release = self.fail_release

        class _Acquired:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                if fail_release:
                    # Resetting the connection as it goes back to the pool fails.
                    raise ConnectionResetError("the connection could not be reset")
                return False

        return _Acquired()


@pytest.fixture
def sweeper(monkeypatch):
    from app.services import events_sweeper

    monkeypatch.setattr(events_sweeper.settings, "redis_url", "")
    monkeypatch.setattr(events_sweeper.settings, "event_retention_days", 7)
    monkeypatch.setattr(events_sweeper, "BATCH_SIZE", 3)
    monkeypatch.setattr(events_sweeper, "_position", 0)
    return events_sweeper


def _serve(monkeypatch, sweeper, conn: _Conn, *, fail_release: bool = False) -> None:
    async def get_pool():
        return _Pool(conn, fail_release=fail_release)

    monkeypatch.setattr(sweeper, "get_pool", get_pool)


async def test_a_batch_of_old_events_is_deleted_through_its_last_id_and_the_pass_goes_on(monkeypatch, sweeper):
    conn = _Conn([(4, True), (7, True), (9, True)], deleted=3)
    _serve(monkeypatch, sweeper, conn)

    assert await sweeper.sweep_once() == 3
    # The delete is given the ids the batch read, not their range.
    assert conn.deletes == [([4, 7, 9], 7)]
    assert sweeper._position == 9


async def test_an_event_that_is_not_old_bounds_the_delete_and_the_next_step_ends_the_pass(monkeypatch, sweeper):
    conn = _Conn([(4, True), (7, False), (9, True)], deleted=1)
    _serve(monkeypatch, sweeper, conn)
    monkeypatch.setattr(sweeper, "_position", 2)

    assert await sweeper.sweep_once() == 1
    # The old event behind the one that is not is not reached.
    assert conn.deletes == [([4], 7)]
    assert sweeper._position == 4

    ended = _Conn([(7, False), (9, True)], deleted=0)
    _serve(monkeypatch, sweeper, ended)
    assert await sweeper.sweep_once() == 0
    assert ended.deletes == []
    assert sweeper._position == 0


async def test_old_events_the_statement_leaves_are_passed_over_and_the_pass_goes_on(monkeypatch, sweeper):
    """Rows another statement holds, or with Redis rows not yet published, stay; the pass moves past them."""
    conn = _Conn([(4, True), (7, True), (9, True)], deleted=0)
    _serve(monkeypatch, sweeper, conn)

    assert await sweeper.sweep_once() == 3
    assert sweeper._position == 9


async def test_with_redis_a_batch_is_deleted_by_publication_in_the_same_bounded_range(monkeypatch, sweeper):
    monkeypatch.setattr(sweeper.settings, "redis_url", "redis://sweep-test")
    conn = _Conn([(4, True), (7, True), (9, False)], deleted=1)
    _serve(monkeypatch, sweeper, conn)

    assert await sweeper.sweep_once() == 2
    assert conn.statements == [sweeper._DELETE_PUBLISHED]
    assert conn.deletes == [([4, 7], 7)]
    assert "FOR UPDATE SKIP LOCKED" in sweeper._DELETE_PUBLISHED
    assert sweeper._position == 7


async def test_an_empty_batch_ends_the_pass(monkeypatch, sweeper):
    conn = _Conn([], deleted=0)
    _serve(monkeypatch, sweeper, conn)
    monkeypatch.setattr(sweeper, "_position", 12)

    assert await sweeper.sweep_once() == 0
    assert conn.deletes == []
    assert sweeper._position == 0


async def test_a_step_that_fails_starts_the_next_pass_from_the_oldest_event(monkeypatch, sweeper):
    conn = _Conn([(4, True)], deleted=1, fail_delete=True)
    _serve(monkeypatch, sweeper, conn)
    monkeypatch.setattr(sweeper, "_position", 3)

    with pytest.raises(ConnectionResetError):
        await sweeper.sweep_once()
    assert sweeper._position == 0


@pytest.mark.parametrize("failing", ["get_pool", "acquire"])
async def test_a_step_that_cannot_get_a_connection_starts_the_next_pass_from_the_oldest_event(
    monkeypatch, sweeper, failing
):
    """The position went back to 0 only once the step had its connection, so a lost pool kept it."""

    class _FailingPool:
        def acquire(self):
            raise ConnectionRefusedError("the pool is closed")

    async def get_pool():
        if failing == "get_pool":
            raise ConnectionRefusedError("no database")
        return _FailingPool()

    monkeypatch.setattr(sweeper, "get_pool", get_pool)
    monkeypatch.setattr(sweeper, "_position", 42)

    with pytest.raises(ConnectionRefusedError):
        await sweeper.sweep_once()
    assert sweeper._position == 0


async def test_a_step_whose_connection_fails_to_go_back_starts_the_next_pass_from_the_oldest_event(
    monkeypatch, sweeper
):
    """The position the step reached was kept before its connection was given back.

    A step skipped an old event another statement held and deleted the one
    after it; giving its connection back then failed, and the next try read on
    from past the skipped event, which waited a further pass. The position is
    now kept only once the connection is back.
    """
    failing = _Conn([(4, True), (7, True)], deleted=1)
    _serve(monkeypatch, sweeper, failing, fail_release=True)
    monkeypatch.setattr(sweeper, "_position", 3)

    with pytest.raises(ConnectionResetError):
        await sweeper.sweep_once()
    assert failing.reads[0][0] == 3
    assert sweeper._position == 0

    retry = _Conn([(4, True), (7, True)], deleted=1)
    _serve(monkeypatch, sweeper, retry)
    assert await sweeper.sweep_once() == 2
    assert retry.reads[0][0] == 0


async def test_the_marks_of_deleted_vaults_go_only_in_the_step_that_ends_the_pass(monkeypatch, sweeper):
    """A step with old events returns at once; the step that finds none deletes the marks and returns 0."""
    busy = _Conn([(4, True)], deleted=1, orphans=[2])
    _serve(monkeypatch, sweeper, busy)
    assert await sweeper.sweep_once() == 1
    assert busy.mark_deletes == 0

    ending = _Conn([(9, False)], deleted=0, orphans=[2])
    _serve(monkeypatch, sweeper, ending)
    assert await sweeper.sweep_once() == 0
    assert ending.mark_deletes == 1

    # With Redis as well.
    monkeypatch.setattr(sweeper.settings, "redis_url", "redis://sweep-test")
    published = _Conn([], deleted=0, orphans=[1])
    _serve(monkeypatch, sweeper, published)
    assert await sweeper.sweep_once() == 0
    assert published.mark_deletes == 1


async def test_the_step_that_ends_the_pass_deletes_the_marks_a_batch_at_a_time_until_one_is_short(
    monkeypatch, sweeper
):
    monkeypatch.setattr(sweeper, "MARK_BATCH_SIZE", 2)
    conn = _Conn([], deleted=0, orphans=[2, 2, 1])
    _serve(monkeypatch, sweeper, conn)

    assert await sweeper.sweep_once() == 0
    assert conn.mark_deletes == 3
