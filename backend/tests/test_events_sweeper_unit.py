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
    """Answers the batch read with ``rows`` and the delete with ``deleted``."""

    def __init__(self, rows: list[tuple[int, bool]], deleted: int, *, fail_delete: bool = False) -> None:
        self.rows = [{"id": event_id, "expired": expired} for event_id, expired in rows]
        self.deleted = deleted
        self.fail_delete = fail_delete
        self.deletes: list[tuple] = []

    async def fetch(self, sql: str, *args):
        return self.rows

    async def fetchval(self, sql: str, *args):
        if self.fail_delete:
            raise ConnectionResetError("lost the connection")
        self.deletes.append(args)
        return self.deleted


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquired:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
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


def _serve(monkeypatch, sweeper, conn: _Conn) -> None:
    async def get_pool():
        return _Pool(conn)

    monkeypatch.setattr(sweeper, "get_pool", get_pool)


async def test_a_full_batch_of_expired_events_goes_on_after_its_last_id(monkeypatch, sweeper):
    conn = _Conn([(4, True), (7, True), (9, True)], deleted=3)
    _serve(monkeypatch, sweeper, conn)

    assert await sweeper.sweep_once() == 3
    # The delete covers the ids after the pass's position through the last expired one.
    assert conn.deletes == [(0, 9, 7)]
    assert sweeper._position == 9


async def test_an_event_inside_the_period_ends_the_pass_and_bounds_the_delete(monkeypatch, sweeper):
    conn = _Conn([(4, True), (7, False), (9, True)], deleted=1)
    _serve(monkeypatch, sweeper, conn)
    monkeypatch.setattr(sweeper, "_position", 2)

    assert await sweeper.sweep_once() == 1
    # The expired event behind the one inside the period is not reached.
    assert conn.deletes == [(2, 4, 7)]
    assert sweeper._position == 0


async def test_a_batch_whose_rows_others_hold_ends_the_pass(monkeypatch, sweeper):
    """Another statement holds every row and is deleting them; this pass stops rather than spin."""
    conn = _Conn([(4, True), (7, True), (9, True)], deleted=0)
    _serve(monkeypatch, sweeper, conn)

    assert await sweeper.sweep_once() == 0
    assert sweeper._position == 0


async def test_a_step_that_fails_starts_the_next_pass_from_the_oldest_event(monkeypatch, sweeper):
    conn = _Conn([(4, True)], deleted=1, fail_delete=True)
    _serve(monkeypatch, sweeper, conn)
    monkeypatch.setattr(sweeper, "_position", 3)

    with pytest.raises(ConnectionResetError):
        await sweeper.sweep_once()
    assert sweeper._position == 0
