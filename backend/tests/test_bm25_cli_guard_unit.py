"""CLI ownership cannot be bypassed by a mode combination."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from scripts import backfill_bm25_vector as cli

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("mode", [[], ["--prepare"], ["--index"], ["--check"]])
async def test_modes_hold_ownership_except_read_only_check(monkeypatch, capsys, mode):
    calls = []
    sweep_options = []

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
        sweep_options.append(args[-4:])
        return 0

    async def owned(pool, schema, operation, **kwargs):
        assert kwargs["announce"] is True
        calls.append("vector locked")
        await operation()
        calls.append("vector unlocked")

    async def bulk_owned(operation):
        calls.append("main locked")
        await operation()
        calls.append("main unlocked")

    monkeypatch.setattr(cli.sys, "argv", ["backfill", *mode])
    for name in ("init_db", "close_pool", "_prepare", "_build_index"):
        monkeypatch.setattr(cli, name, nothing)
    monkeypatch.setattr(cli, "_vector_pool", pool)
    monkeypatch.setattr(cli, "_column_exists", exists)
    monkeypatch.setattr(cli, "_counts", counts)
    monkeypatch.setattr(cli, "_pass", sweep)
    monkeypatch.setattr(cli, "run_exclusive", owned)
    monkeypatch.setattr(cli, "run_bulk_exclusive", bulk_owned)
    monkeypatch.setattr(cli.sparse_encoder, "start_tokenizer_pool", lambda *_: None)
    monkeypatch.setattr(cli.sparse_encoder, "stop_tokenizer_pool", lambda: None)
    await cli.main()
    assert ("vector locked" in calls) == (mode != ["--check"])
    assert ("main locked" in calls) == (mode != ["--check"])
    if not mode:
        assert calls.index("main locked") < calls.index("vector locked")
        assert calls.index("vector locked") < calls.index("sweep")
        assert calls.index("sweep") < calls.index("vector unlocked")
        assert calls.index("vector unlocked") < calls.index("main unlocked")
        assert sweep_options == [(cli._WRITERS, cli._BATCH, 0.0, None)]
        assert "safe to flip" not in capsys.readouterr().out
    assert calls[-1] == "pool closed"


@pytest.mark.parametrize(
    "args",
    [
        ["--check", "--prepare"],
        ["--index", "--check"],
        ["--writers", "0"],
        ["--write-batch-size", "0"],
        ["--write-batch-size", "nope"],
        ["--write-pause-secs", "-0.1"],
        ["--write-pause-secs", "nan"],
        ["--write-pause-secs", "inf"],
    ],
)
async def test_invalid_modes_rejected_before_connecting(monkeypatch, args):
    async def unexpected():
        pytest.fail("invalid arguments must not initialize DB")
    monkeypatch.setattr(cli.sys, "argv", ["backfill", *args])
    monkeypatch.setattr(cli, "init_db", unexpected)
    with pytest.raises(SystemExit) as error:
        await cli.main()
    assert error.value.code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["--resume-sweep"],
        ["--since", "2020-01-01T00:00:00Z", "--sweep-id",
         "00000000-0000-0000-0000-000000000001"],
        ["--since", "2020-01-01T00:00:00Z", "--protect-since",
         "2020-01-02T00:00:00Z", "--sweep-id",
         "00000000-0000-0000-0000-000000000001", "--attempt-id",
         "00000000-0000-0000-0000-000000000002", "--source-revision", "short"],
    ],
)
async def test_incomplete_checkpoint_identity_rejected_before_connecting(
    monkeypatch, args,
):
    async def unexpected():
        pytest.fail("invalid checkpoint identity must not initialize DB")

    monkeypatch.setattr(cli.sys, "argv", ["backfill", *args])
    monkeypatch.setattr(cli, "init_db", unexpected)
    with pytest.raises(SystemExit):
        await cli.main()


async def test_checkpointed_cli_claims_under_locks_and_retains_protected_window(
    monkeypatch, capsys,
):
    calls = []
    sweep_id, attempt_id = uuid.uuid4(), uuid.uuid4()

    class Connection:
        async def fetchval(self, _query):
            return datetime(2021, 1, 1, tzinfo=timezone.utc)

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

        async def close(self):
            calls.append("pool closed")

    async def nothing(*_args):
        return None

    async def pool(*_args):
        return Pool()

    async def exists(*_args):
        return True

    async def counts(*_args):
        return 0, 0

    receipt = SimpleNamespace(cursor=uuid.UUID(int=0), seen=0, written=0)

    async def open_progress(*_args, **kwargs):
        calls.append("checkpoint claimed")
        assert kwargs["sweep_id"] == sweep_id
        assert kwargs["attempt_id"] == attempt_id
        assert kwargs["resume"] is False
        return receipt

    async def sweep(*args):
        calls.append("sweep")
        assert args[-1] is receipt
        return 1

    async def owned(_pool, _schema, operation, **_kwargs):
        calls.append("vector locked")
        await operation()
        calls.append("vector unlocked")

    async def bulk_owned(operation):
        calls.append("main locked")
        await operation()
        calls.append("main unlocked")

    monkeypatch.setattr(cli.sys, "argv", [
        "backfill", "--since", "2020-01-01T00:00:00Z",
        "--protect-since", "2020-01-02T00:00:00Z",
        "--sweep-id", str(sweep_id), "--attempt-id", str(attempt_id),
        "--source-revision", "a" * 40,
    ])
    for name in ("init_db", "close_pool"):
        monkeypatch.setattr(cli, name, nothing)
    monkeypatch.setattr(cli, "_vector_pool", pool)
    monkeypatch.setattr(cli, "_column_exists", exists)
    monkeypatch.setattr(cli, "_counts", counts)
    monkeypatch.setattr(cli, "_pass", sweep)
    monkeypatch.setattr(cli, "open_checkpoint", open_progress)
    monkeypatch.setattr(cli, "_encoding_contract", lambda: "encoder-v1")
    monkeypatch.setattr(cli, "run_exclusive", owned)
    monkeypatch.setattr(cli, "run_bulk_exclusive", bulk_owned)
    monkeypatch.setattr(cli.sparse_encoder, "start_tokenizer_pool", lambda *_: None)
    monkeypatch.setattr(cli.sparse_encoder, "stop_tokenizer_pool", lambda: None)
    await cli.main()
    assert calls.index("main locked") < calls.index("vector locked")
    assert calls.index("vector locked") < calls.index("checkpoint claimed")
    assert calls.index("checkpoint claimed") < calls.index("sweep")
    assert calls.index("sweep") < calls.index("vector unlocked")
    assert "--since '2020-01-02T00:00:00+00:00'" in capsys.readouterr().out
