from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_migration_pool_does_not_inherit_request_statement_timeout(monkeypatch):
    from app.db import postgres

    observed: dict[str, object] = {}

    class Pool:
        closed = False

        async def close(self) -> None:
            self.closed = True

    pool = Pool()

    async def create_pool(**kwargs):
        observed.update(kwargs)
        return pool

    monkeypatch.setattr(postgres.asyncpg, "create_pool", create_pool)

    async with postgres._migration_pool() as opened:
        assert opened is pool
        assert pool.closed is False

    assert pool.closed is True
    assert observed["min_size"] == 1
    assert observed["max_size"] == 1
    assert observed["command_timeout"] is None
    assert observed["server_settings"] == {
        "application_name": "akb-schema-migration",
        "idle_in_transaction_session_timeout": "60000",
        "statement_timeout": "0",
    }
