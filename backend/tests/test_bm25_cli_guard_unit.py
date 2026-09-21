"""CLI ownership cannot be bypassed by a mode combination."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from scripts import backfill_bm25_vector as cli

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("mode", [[], ["--prepare"], ["--index"], ["--check"]])
async def test_modes_hold_ownership_except_read_only_check(monkeypatch, capsys, mode):
    calls = []

    class Connection:
        async def fetchval(self, query):
            return datetime.now(timezone.utc)

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

        async def close(self):
            calls.append("pool closed")

    async def nothing(*args):
        return None

    async def pool(*args):
        return Pool()

    async def exists(*args):
        return True

    async def counts(*args):
        return 0, 0

    async def sweep(*args):
        calls.append("sweep")
        return 0

    async def owned(pool, schema, operation, **kwargs):
        assert kwargs["announce"] is True
        calls.append("locked")
        await operation()
        calls.append("unlocked")

    monkeypatch.setattr(cli.sys, "argv", ["backfill", *mode])
    for name in ("init_db", "close_pool", "_prepare", "_build_index"):
        monkeypatch.setattr(cli, name, nothing)
    monkeypatch.setattr(cli, "_vector_pool", pool)
    monkeypatch.setattr(cli, "_column_exists", exists)
    monkeypatch.setattr(cli, "_counts", counts)
    monkeypatch.setattr(cli, "_pass", sweep)
    monkeypatch.setattr(cli, "run_exclusive", owned)
    monkeypatch.setattr(cli.sparse_encoder, "start_tokenizer_pool", lambda *_: None)
    monkeypatch.setattr(cli.sparse_encoder, "stop_tokenizer_pool", lambda: None)
    await cli.main()
    assert ("locked" in calls) == (mode != ["--check"])
    if not mode:
        assert calls.index("locked") < calls.index("sweep") < calls.index("unlocked")
        assert "safe to flip" not in capsys.readouterr().out
    assert calls[-1] == "pool closed"


@pytest.mark.parametrize("args", [["--check", "--prepare"], ["--index", "--check"], ["--writers", "0"]])
async def test_invalid_modes_rejected_before_connecting(monkeypatch, args):
    async def unexpected():
        pytest.fail("invalid arguments must not initialize DB")
    monkeypatch.setattr(cli.sys, "argv", ["backfill", *args])
    monkeypatch.setattr(cli, "init_db", unexpected)
    with pytest.raises(SystemExit) as error:
        await cli.main()
    assert error.value.code == 2
